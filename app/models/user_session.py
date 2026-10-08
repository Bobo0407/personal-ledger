"""user_sessions：登入 Session（規格 6.10）。

Cookie 只放原始權杖；資料庫只存它的雜湊（token_hash），
資料庫外洩時也無法直接拿來登入。
"""

from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.user import User


class UserSession(TimestampMixin, Base):
    __tablename__ = "user_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    token_hash: Mapped[str] = mapped_column(unique=True)
    expires_at: Mapped[str]  # 建立時 + 30 天，固定不延長
    last_seen_at: Mapped[str | None]
    revoked_at: Mapped[str | None]
    user_agent: Mapped[str | None]

    user: Mapped["User"] = relationship(back_populates="sessions")

    def __repr__(self) -> str:  # 不輸出 token_hash
        return f"<UserSession id={self.id} user_id={self.user_id}>"
