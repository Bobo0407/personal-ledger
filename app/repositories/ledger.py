"""分錄查詢（規格 5.4）。只查詢，不 commit，不放商業規則。

有效分錄 = postings JOIN transactions，且 transactions.deleted_at IS NULL AND status = 'POSTED'。
這個排除條件全專案只寫在 active_postings() 這一處；所有帳務加總都從這裡開始，
呼叫端只能再加條件或改成加總欄位，例如：

    active_postings().with_only_columns(func.sum(Posting.amount)).where(...)

例外（規格 5.4）：帳平檢查要看全部交易（含已刪除與草稿），使用名稱明確寫著「全部」的
all_transaction_sums() 與 all_postings_on_non_leaf_accounts()，不得用於餘額或報表。
"""

from collections.abc import Iterable
from datetime import date

from sqlalchemy import Select, exists, func, select
from sqlalchemy.orm import Session

from app.models import Account, Posting, Transaction
from app.models.enums import TxnStatus, TxnType


def active_postings() -> Select:
    """有效分錄的唯一查詢入口。"""
    return (
        select(Posting)
        .join(Transaction, Posting.transaction_id == Transaction.id)
        .where(Transaction.deleted_at.is_(None), Transaction.status == TxnStatus.POSTED.value)
    )


def _date_range(stmt: Select, start: date | None, end: date | None) -> Select:
    # txn_date 是 YYYY-MM-DD 字串，字串比較等同日期比較
    if start is not None:
        stmt = stmt.where(Transaction.txn_date >= start.isoformat())
    if end is not None:
        stmt = stmt.where(Transaction.txn_date <= end.isoformat())
    return stmt


def active_sum_for_accounts(
    db: Session,
    account_ids: Iterable[int],
    start: date | None = None,
    end: date | None = None,
    txn_type: str | None = None,
) -> int:
    """指定科目在期間內（起訖日都包含）的有效分錄加總，原始正負號；可限定交易類型。"""
    ids = list(account_ids)
    if not ids:
        return 0
    stmt = active_postings().with_only_columns(func.coalesce(func.sum(Posting.amount), 0))
    stmt = stmt.where(Posting.account_id.in_(ids))
    if txn_type is not None:
        stmt = stmt.where(Transaction.txn_type == txn_type)
    return int(db.scalar(_date_range(stmt, start, end)) or 0)


def accounts_with_active_opening(db: Session, account_ids: Iterable[int]) -> set[int]:
    """這些科目中，已經有有效 OPENING 分錄的科目 id（決策 D12）。"""
    ids = list(account_ids)
    if not ids:
        return set()
    stmt = (
        active_postings()
        .with_only_columns(Posting.account_id)
        .where(Transaction.txn_type == TxnType.OPENING.value, Posting.account_id.in_(ids))
        .distinct()
    )
    return set(db.scalars(stmt))


def active_sum_for_class(
    db: Session, account_class: str, start: date | None = None, end: date | None = None
) -> int:
    """某一大類在期間內的有效分錄加總，原始正負號。"""
    stmt = (
        active_postings()
        .join(Account, Posting.account_id == Account.id)
        .with_only_columns(func.coalesce(func.sum(Posting.amount), 0))
        .where(Account.account_class == account_class)
    )
    return int(db.scalar(_date_range(stmt, start, end)) or 0)


# --- 帳平檢查專用：刻意包含已刪除與草稿交易 -------------------------------------


def all_transaction_sums(db: Session) -> list[tuple[int, str, str | None, int, int]]:
    """全部交易（含已刪除、草稿）的 (id, status, deleted_at, 分錄筆數, 分錄加總)。"""
    stmt = (
        select(
            Transaction.id,
            Transaction.status,
            Transaction.deleted_at,
            func.count(Posting.id),
            func.coalesce(func.sum(Posting.amount), 0),
        )
        .outerjoin(Posting, Posting.transaction_id == Transaction.id)
        .group_by(Transaction.id)
        .order_by(Transaction.id)
    )
    return [tuple(row) for row in db.execute(stmt)]


def all_postings_on_non_leaf_accounts(db: Session) -> list[tuple[int, int, int]]:
    """全部交易中記在非末層科目上的分錄：(transaction_id, posting_id, account_id)。"""
    child = Account.__table__.alias("child")
    stmt = (
        select(Posting.transaction_id, Posting.id, Posting.account_id)
        .where(exists().where(child.c.parent_id == Posting.account_id))
        .order_by(Posting.transaction_id, Posting.id)
    )
    return [tuple(row) for row in db.execute(stmt)]
