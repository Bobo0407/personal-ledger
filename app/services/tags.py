"""標籤的讀取（P2 Step 8A）。

8A 只提供讀取；建立標籤（含名稱正規化）、停用、管理頁都不在 8A（8B／P3 再決定）。
寫入 transaction_tags 只在 LedgerService（ADR-007）。
"""

from sqlalchemy.orm import Session

from app.models import Tag
from app.repositories import tags as tag_repo


def tags_for_transaction(db: Session, txn_id: int) -> list[Tag]:
    """交易目前的標籤（依名稱排序），含已停用的標籤；已刪除的交易也讀得到。"""
    return tag_repo.tags_for_transaction(db, txn_id)
