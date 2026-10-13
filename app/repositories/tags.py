"""標籤的唯讀查詢（P2 Step 8A）。寫入 transaction_tags 只在 services/ledger.py。

交易的標籤一律用查詢讀「資料庫目前的狀態」，不靠 ORM 集合（Transaction 沒有標籤 relationship）。
這裡不看交易是否已刪除：明細、稽核快照、驗證都需要讀到已刪除交易的標籤。
"""

from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Tag, TransactionTag


def get_many(db: Session, tag_ids: Iterable[int]) -> dict[int, Tag]:
    """{id: Tag}；找不到的 id 不會出現在結果裡。"""
    ids = set(tag_ids)
    if not ids:
        return {}
    return {t.id: t for t in db.scalars(select(Tag).where(Tag.id.in_(ids)))}


def ids_for_transaction(db: Session, txn_id: int) -> tuple[int, ...]:
    """這筆交易目前的標籤 id（排序後）。"""
    stmt = (
        select(TransactionTag.tag_id)
        .where(TransactionTag.transaction_id == txn_id)
        .order_by(TransactionTag.tag_id)
    )
    return tuple(db.scalars(stmt))


def links_for_transaction(db: Session, txn_id: int) -> list[TransactionTag]:
    """給 LedgerService 整組替換時刪除用（ORM 物件，刪除時 identity map 一致）。"""
    stmt = select(TransactionTag).where(TransactionTag.transaction_id == txn_id)
    return list(db.scalars(stmt))


def tags_for_transaction(db: Session, txn_id: int) -> list[Tag]:
    """這筆交易的標籤（依名稱排序），含已停用的標籤。"""
    stmt = (
        select(Tag)
        .join(TransactionTag, TransactionTag.tag_id == Tag.id)
        .where(TransactionTag.transaction_id == txn_id)
        .order_by(Tag.name, Tag.id)
    )
    return list(db.scalars(stmt))
