"""最小帳務查詢（P1 決策 D15、Q5）：餘額、期間合計、大類合計、淨資產。

- 全部建立在 repositories.ledger.active_postings() 上，只計算有效分錄。
- balance()、period_total()、class_total()、net_worth() 一律回傳「正常餘額方向」：
  資產、費用 = +SUM；負債、權益、收入 = −SUM（規格 8.1：負債顯示為欠款）。
  呼叫端（reports、budgets）直接使用，不要再 abs() 或自行翻號；
  退款讓費用淨額變成負數時就是負數（T06）。原始正負號只在 raw_balance() 與 repositories 內部使用。
- 截止日的預設是「截至今天（台北日期）」，不包含未來日期的交易；
  如果日後需要包含未來交易的帳面餘額，另設名稱明確的查詢，不改變這裡的預設語意。
- 群組科目的餘額包含所有子孫科目。
- 完整的月報、年報在 P3 的 reports.py，會沿用這裡的查詢。
"""

from datetime import date

from sqlalchemy.orm import Session

from app import clock
from app.models.enums import AccountClass
from app.repositories import accounts as account_repo
from app.repositories import ledger as ledger_repo
from app.services.errors import NotFound

NORMAL_SIGN: dict[str, int] = {
    AccountClass.ASSET: 1,
    AccountClass.EXPENSE: 1,
    AccountClass.LIABILITY: -1,
    AccountClass.EQUITY: -1,
    AccountClass.INCOME: -1,
}


def subtree_ids(db: Session, account_id: int) -> list[int]:
    """科目本身加上所有子孫科目的 id。"""
    children: dict[int | None, list[int]] = {}
    for account in account_repo.list_all(db):
        children.setdefault(account.parent_id, []).append(account.id)
    ids, stack = [], [account_id]
    while stack:
        current = stack.pop()
        ids.append(current)
        stack.extend(children.get(current, []))
    return ids


def raw_balance(db: Session, account_id: int, as_of: date | None = None) -> int:
    """原始正負號（借正貸負）的餘額，截至 as_of（含當天），None = 截至今天。"""
    as_of = as_of or clock.today()
    return ledger_repo.active_sum_for_accounts(db, subtree_ids(db, account_id), end=as_of)


def balance(db: Session, account_id: int, as_of: date | None = None) -> int:
    """正常餘額方向的餘額，截至 as_of（含當天）；as_of=None 代表截至今天。"""
    account = account_repo.get(db, account_id)
    if account is None:
        raise NotFound("科目", account_id)
    return NORMAL_SIGN[account.account_class] * raw_balance(db, account_id, as_of)


def period_total(db: Session, account_id: int, start: date, end: date) -> int:
    """期間內（起訖日都包含）的發生額，正常餘額方向，包含子孫科目。"""
    account = account_repo.get(db, account_id)
    if account is None:
        raise NotFound("科目", account_id)
    raw = ledger_repo.active_sum_for_accounts(db, subtree_ids(db, account_id), start, end)
    return NORMAL_SIGN[account.account_class] * raw


def class_total(db: Session, account_class: str, start: date, end: date) -> int:
    """某一大類在期間內的發生額，正常餘額方向；例如本月支出 = class_total(EXPENSE, 月初, 月底)。"""
    raw = ledger_repo.active_sum_for_class(db, account_class, start, end)
    return NORMAL_SIGN[account_class] * raw


def net_worth(db: Session, as_of: date | None = None) -> int:
    """淨資產 = 資產合計 − 負債合計，截至 as_of（含當天）；None = 截至今天。"""
    as_of = as_of or clock.today()
    assets = ledger_repo.active_sum_for_class(db, AccountClass.ASSET, end=as_of)
    liabilities = -ledger_repo.active_sum_for_class(db, AccountClass.LIABILITY, end=as_of)
    return assets - liabilities
