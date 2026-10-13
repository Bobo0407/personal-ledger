"""交易明細（P2 Step 4A）與修改條件的入口。

只讀，不寫入。明細要能讀到已刪除的交易（規格 5.4：交易的「已刪除」檢視與還原），
所以這裡用 id 直接讀一筆交易；這不是帳務加總，不經過 active_postings()。

分錄顯示不用「借／貸」（規格 1.5），也不給畫面帶正負號的金額：
每筆分錄依科目的正常餘額方向（ledger_queries.NORMAL_SIGN）換算成「增加／減少 + 金額」，
例如「郵局 減少 120」「聯邦信用卡 增加 120」（欠款增加）。template 不判斷正負或科目類別。

修改條件（edit guard）：表單不能完整表示的交易不開放修改，免得儲存時默默丟掉看不到的內容。
判斷與預填值在 services/txn_edit.py（還原 → builder 重建 → 完整比對）：
4A 只開放快速記帳格式的支出；5B 加入收入、轉帳、繳卡費；Step 6 支出改用同一個做法，
開放手續費、分期利息、不計入預算（外幣仍不開放）。
帳務規則（V08、V10–V12、REFUND_LOCKED……）仍全部由 P1 負責。
"""

from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy.orm import Session

from app.models import Merchant, PaymentMethod, Transaction
from app.models.enums import LineRole, TxnType
from app.repositories import accounts as account_repo
from app.repositories import ledger as ledger_repo
from app.services import txn_edit
from app.services.entry_options import VOUCHER_LABELS
from app.services.errors import NotFound, RuleViolation
from app.services.ledger_queries import NORMAL_SIGN
from app.services.search import TXN_TYPE_LABELS

NOT_EDITABLE = "NOT_EDITABLE"

ROLE_LABELS = {LineRole.FEE: "手續費", LineRole.INTEREST: "分期利息"}


@dataclass(frozen=True)
class PostingLine:
    account_name: str
    effect: str  # 「增加」或「減少」（依科目正常餘額方向）
    amount: int  # 絕對值
    role_label: str | None  # 手續費、分期利息；MAIN 與一般分錄不另外標示


@dataclass(frozen=True)
class TxnDetail:
    id: int
    txn_date: date
    txn_type: str
    type_label: str
    is_deleted: bool
    deleted_at: datetime | None
    merchant_name: str | None
    payment_method_name: str | None
    description: str | None
    voucher_label: str | None
    invoice_no: str | None
    magnitude: int  # 交易規模：正數分錄加總（與列表相同定義）
    lines: tuple[PostingLine, ...]
    refunded_amount: int  # EXPENSE：有效退款合計；其他類型為 0
    related_txn_id: int | None  # REFUND：原交易
    edit_blockers: tuple[str, ...]  # 空 = 可以用表單修改
    delete_blockers: tuple[str, ...]  # 空 = 可以刪除（已刪除的交易不適用）
    exclude_from_budget: bool = False  # 不計入預算（表頭旗標；P2 Step 6 在明細顯示）


def _get(db: Session, txn_id: int) -> Transaction:
    txn = db.get(Transaction, txn_id)
    if txn is None:
        raise NotFound("交易", txn_id)
    return txn


def _line(db: Session, posting) -> PostingLine:
    account = account_repo.get(db, posting.account_id)
    signed = posting.amount * NORMAL_SIGN[account.account_class]
    return PostingLine(
        account_name=account.name,
        effect="增加" if signed > 0 else "減少",
        amount=abs(posting.amount),
        role_label=ROLE_LABELS.get(posting.line_role),
    )


def edit_blockers(db: Session, txn: Transaction) -> tuple[str, ...]:
    """不能用表單修改的原因；空 tuple 代表可以修改。判斷在 services/txn_edit.py
    （還原 → builder 重建 → 完整比對；Step 6 起支出也用同一個做法）。"""
    return txn_edit.edit_blockers(db, txn)


def require_editable(db: Session, txn_id: int) -> Transaction:
    """修改前的伺服器端檢查：找不到 → NotFound；不能用表單修改 → RuleViolation（NOT_EDITABLE）。"""
    txn = _get(db, txn_id)
    reasons = edit_blockers(db, txn)
    if reasons:
        raise RuleViolation(NOT_EDITABLE, reasons[0])
    return txn


@dataclass(frozen=True)
class EditState:
    """修改頁需要的狀態：原交易類型（決定用哪張表單）、不能修改的原因、預填值。"""

    txn_type: str
    blocker: str | None  # None = 可以修改
    values: dict[str, str] | None  # 可以修改時的預填值（也是「目前值」的來源）


def edit_state(db: Session, txn_id: int) -> EditState:
    """找不到 → NotFound。不能修改時 values 為 None。"""
    txn = _get(db, txn_id)
    reasons = edit_blockers(db, txn)
    if reasons:
        return EditState(txn.txn_type, reasons[0], None)
    return EditState(txn.txn_type, None, txn_edit.edit_values(db, txn))


def get_detail(db: Session, txn_id: int) -> TxnDetail:
    txn = _get(db, txn_id)
    merchant = db.get(Merchant, txn.merchant_id) if txn.merchant_id is not None else None
    pm = db.get(PaymentMethod, txn.payment_method_id) if txn.payment_method_id else None
    refunded = (
        ledger_repo.active_refunded_amount(db, txn.id) if txn.txn_type == TxnType.EXPENSE else 0
    )
    is_deleted = txn.deleted_at is not None
    delete_blockers: tuple[str, ...] = ()
    if not is_deleted and refunded > 0:
        delete_blockers = (f"這筆支出已退 {refunded:,} 元，請先刪除退款",)
    return TxnDetail(
        id=txn.id,
        txn_date=date.fromisoformat(txn.txn_date),
        txn_type=txn.txn_type,
        type_label=TXN_TYPE_LABELS.get(txn.txn_type, txn.txn_type),
        is_deleted=is_deleted,
        deleted_at=datetime.fromisoformat(txn.deleted_at) if is_deleted else None,
        merchant_name=merchant.name if merchant is not None else None,
        payment_method_name=pm.name if pm is not None else None,
        description=txn.description,
        voucher_label=VOUCHER_LABELS.get(txn.voucher_type) if txn.voucher_type else None,
        invoice_no=txn.invoice_no,
        magnitude=sum(p.amount for p in txn.postings if p.amount > 0),
        lines=tuple(_line(db, p) for p in txn.postings),
        refunded_amount=refunded,
        related_txn_id=txn.related_txn_id,
        edit_blockers=edit_blockers(db, txn),
        exclude_from_budget=txn.exclude_from_budget,
        delete_blockers=delete_blockers,
    )


def edit_values(db: Session, txn_id: int) -> dict[str, str]:
    """修改表單的預填值；不能修改時丟 RuleViolation（NOT_EDITABLE）。"""
    return txn_edit.edit_values(db, require_editable(db, txn_id))
