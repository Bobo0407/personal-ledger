"""audit_logs：稽核紀錄（規格 6.10）。

P1 起由 LedgerService 在同一個資料庫交易內寫入（決策 D6）；檢視介面在 P2。
user_id 可為空：排程工作等系統動作沒有登入使用者。
"""

from sqlalchemy import CheckConstraint, ForeignKey, Index
from sqlalchemy.orm import Mapped, mapped_column

from app import clock
from app.models.base import Base, TimestampMixin
from app.models.enums import AuditAction, sql_in


def _now_iso() -> str:
    return clock.now_iso()


class AuditLog(TimestampMixin, Base):
    __tablename__ = "audit_logs"
    __table_args__ = (
        CheckConstraint(sql_in("action", AuditAction), name="action"),
        Index("ix_audit_logs_entity_entity_id", "entity", "entity_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    occurred_at: Mapped[str] = mapped_column(default=_now_iso)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    entity: Mapped[str]  # 例如 "transaction"
    entity_id: Mapped[int]
    action: Mapped[str]
    before_json: Mapped[str | None]
    after_json: Mapped[str | None]

    def __repr__(self) -> str:  # 不輸出 before／after 內容
        return f"<AuditLog id={self.id} {self.action} {self.entity}#{self.entity_id}>"
