"""budgets 資料存取（P3-3A）。只讀寫預算列，不放商業規則、不 commit。

已用金額的加總不在這裡：一律走 repositories/ledger.py 的 active_sum_for_accounts()。
"""

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from app.models import Budget


def get(db: Session, account_id: int, effective_from: str) -> Budget | None:
    stmt = select(Budget).where(
        Budget.account_id == account_id, Budget.effective_from == effective_from
    )
    return db.scalar(stmt)


def in_force_candidates(db: Session, month: str, account_id: int | None = None) -> list[Budget]:
    """effective_from ≤ month 的預算列，依 (account_id, effective_from) 由舊到新。

    每個科目的最後一筆就是 month 當月採用的版本（由 service 挑選）。
    """
    stmt = select(Budget).where(Budget.effective_from <= month)
    if account_id is not None:
        stmt = stmt.where(Budget.account_id == account_id)
    return list(db.scalars(stmt.order_by(Budget.account_id, Budget.effective_from)))


def later_versions(db: Session, month: str) -> list[Budget]:
    """effective_from > month 的預算列（已排定、尚未生效），依 (account_id, effective_from)。"""
    stmt = select(Budget).where(Budget.effective_from > month)
    return list(db.scalars(stmt.order_by(Budget.account_id, Budget.effective_from)))


def has_budgets(db: Session, account_id: int) -> bool:
    return bool(db.scalar(select(exists().where(Budget.account_id == account_id))))
