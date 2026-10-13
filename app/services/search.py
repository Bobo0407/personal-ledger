"""交易列表與搜尋（規格 F22、F23、UF10、7.1 search.py；P2 Step 3）。

- 只列有效交易（active_postings() 語意，見 repositories/ledger.py）。
- 排序固定為 txn_date DESC、id DESC；每頁 PAGE_SIZE 筆（offset 分頁）。
- 每列的 amount 是交易規模 magnitude（所有正數分錄加總，一律為正數），所有 txn_type 同一個定義；
  它不是支出也不是收入，畫面必須搭配交易類型標籤。
- 摘要涵蓋「全部符合條件的結果」，不是目前這一頁：
  - expense_net  = SUM(EXPENSE 科目分錄 amount)，不取絕對值；只有退款時可以是負數。
  - income_total = -SUM(INCOME 科目分錄 amount)。
  轉帳、繳卡費沒有費用分錄，自然不計入支出；轉帳手續費是費用分錄，計入支出。不依 txn_type 特判。
- 商家條件：services/merchants.resolve()（別名與標準名稱，正規化後完全相同）。
  搜尋只讀，找不到商家時回傳 0 筆，不建立商家。
- 標籤條件（P2 Step 8B）：單一標籤，services/tags.resolve()（正規化後完全相同，含已停用）；
  找不到時回傳 0 筆，不建立標籤。
- 退款的分類（P2 Step 8B）：商家、標籤這兩個「分類維度」，退款跟著原交易「目前」的值
  （repositories/ledger.py 的 _classification_owner）；日期、金額、排序仍然看退款自己。
  所以「原交易 1,000 + 退款 300」用原交易的商家或標籤搜尋時，支出淨額是 700。
  退款自己存的 merchant_id 是建立當下的紀錄，不拿來分類。
"""

import calendar
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy.orm import Session

from app.models.enums import AccountClass, TxnType
from app.repositories import ledger as ledger_repo
from app.services import merchants, tags

PAGE_SIZE = 50

TXN_TYPE_LABELS = {
    TxnType.EXPENSE: "支出",
    TxnType.INCOME: "收入",
    TxnType.TRANSFER: "轉帳",
    TxnType.CARD_PAYMENT: "繳卡費",
    TxnType.REFUND: "退款",
    TxnType.PAYROLL: "薪資",
    TxnType.OPENING: "期初",
    TxnType.ADJUSTMENT: "調整",
    TxnType.MANUAL: "多分錄",
}


@dataclass(frozen=True)
class SearchFilters:
    date_from: date | None = None
    date_to: date | None = None
    merchant_name: str | None = None
    min_amount: int | None = None
    max_amount: int | None = None
    tag_name: str | None = None  # P2 Step 8B：單一標籤（正規化後完全相符）


@dataclass(frozen=True)
class TxnRow:
    id: int
    txn_date: date
    txn_type: str
    type_label: str
    merchant_name: str | None
    description: str | None
    amount: int  # magnitude，一律為正數


@dataclass(frozen=True)
class Page:
    rows: tuple[TxnRow, ...]
    total_count: int
    page: int
    page_count: int
    expense_net: int
    income_total: int
    merchant_not_found: bool = False
    tag_not_found: bool = False


def month_range(today: date) -> tuple[date, date]:
    """today 所在月份的第一天與最後一天。"""
    last_day = calendar.monthrange(today.year, today.month)[1]
    return today.replace(day=1), today.replace(day=last_day)


def search(db: Session, filters: SearchFilters, page: int = 1) -> Page:
    merchant_id = None
    if filters.merchant_name is not None:
        merchant = merchants.resolve(db, filters.merchant_name)
        if merchant is None:
            return Page((), 0, 1, 1, 0, 0, merchant_not_found=True)
        merchant_id = merchant.id
    tag_id = None
    if filters.tag_name is not None:
        tag = tags.resolve(db, filters.tag_name)  # 搜尋只讀，不建立標籤；停用的也可以搜
        if tag is None:
            return Page((), 0, 1, 1, 0, 0, tag_not_found=True)
        tag_id = tag.id

    criteria = {
        "date_from": filters.date_from,
        "date_to": filters.date_to,
        "merchant_id": merchant_id,
        "min_amount": filters.min_amount,
        "max_amount": filters.max_amount,
        "tag_id": tag_id,
    }
    total = ledger_repo.count_active_transactions(db, **criteria)
    page_count = max(1, -(-total // PAGE_SIZE))
    page = min(max(page, 1), page_count)
    found = ledger_repo.search_active_transactions(
        db, limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE, **criteria
    )
    sums = ledger_repo.active_class_sums_for_search(db, **criteria)
    rows = tuple(
        TxnRow(
            id=txn.id,
            txn_date=date.fromisoformat(txn.txn_date),
            txn_type=txn.txn_type,
            type_label=TXN_TYPE_LABELS.get(txn.txn_type, txn.txn_type),
            merchant_name=merchant_name,
            description=txn.description,
            amount=magnitude,
        )
        for txn, merchant_name, magnitude in found
    )
    return Page(
        rows=rows,
        total_count=total,
        page=page,
        page_count=page_count,
        expense_net=sums.get(AccountClass.EXPENSE.value, 0),
        income_total=-sums.get(AccountClass.INCOME.value, 0),
    )


# --- 已刪除交易列表（P2 Step 4B）-----------------------------------------------------------
# 與上面的有效交易搜尋完全分開：不經過 active_postings()，也沒有任何金額合計。


@dataclass(frozen=True)
class DeletedRow:
    row: TxnRow  # amount 是該交易自己的 magnitude，僅供顯示
    deleted_at: datetime


@dataclass(frozen=True)
class DeletedPage:
    rows: tuple[DeletedRow, ...]
    total_count: int
    page: int
    page_count: int


def deleted_page(db: Session, page: int = 1) -> DeletedPage:
    """已刪除的交易，最近刪除的在前（deleted_at DESC, id DESC），每頁 PAGE_SIZE 筆。"""
    total = ledger_repo.count_deleted_transactions(db)
    page_count = max(1, -(-total // PAGE_SIZE))
    page = min(max(page, 1), page_count)
    found = ledger_repo.deleted_transactions(db, limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE)
    rows = tuple(
        DeletedRow(
            row=TxnRow(
                id=txn.id,
                txn_date=date.fromisoformat(txn.txn_date),
                txn_type=txn.txn_type,
                type_label=TXN_TYPE_LABELS.get(txn.txn_type, txn.txn_type),
                merchant_name=merchant_name,
                description=txn.description,
                amount=magnitude,
            ),
            deleted_at=datetime.fromisoformat(txn.deleted_at),
        )
        for txn, merchant_name, magnitude in found
    )
    return DeletedPage(rows=rows, total_count=total, page=page, page_count=page_count)
