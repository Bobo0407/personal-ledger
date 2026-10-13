"""tags、transaction_tags：標籤與交易的多對多關係（規格 6.8、F19；P2 Step 8A、ADR-007）。

- 標籤掛在交易，不掛在分錄（規格「標籤層級」決策）：不影響借貸，也不影響任何帳務加總。
- tags.name 只照規格做精確 UNIQUE；名稱的大小寫、空白正規化在 service 層（P2 Step 8B）。
- is_active 是軟停用；沒有 deleted_at。外鍵都不 cascade：交易只軟刪除，
  有交易引用的標籤也不能硬刪除。
- 只有 LedgerService（services/ledger.py）可以寫入 transaction_tags，
  而且和交易、分錄、稽核在同一個資料庫交易內提交（規格 7.4）。
- Transaction 刻意不建 relationship：快照、驗證一律用查詢讀「資料庫目前的狀態」，
  避免 ORM 集合快取過期（P2 Step 8A 決策）。
"""

from sqlalchemy import CheckConstraint, ForeignKey, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class Tag(TimestampMixin, Base):
    __tablename__ = "tags"
    __table_args__ = (CheckConstraint("is_active IN (0, 1)", name="is_active_bool"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(unique=True)
    is_active: Mapped[bool] = mapped_column(default=True, server_default=text("1"))

    def __repr__(self) -> str:
        return f"<Tag id={self.id} {self.name}>"


class TransactionTag(TimestampMixin, Base):
    __tablename__ = "transaction_tags"

    transaction_id: Mapped[int] = mapped_column(ForeignKey("transactions.id"), primary_key=True)
    tag_id: Mapped[int] = mapped_column(ForeignKey("tags.id"), primary_key=True, index=True)

    def __repr__(self) -> str:
        return f"<TransactionTag txn={self.transaction_id} tag={self.tag_id}>"
