"""users：登入帳號、密碼雜湊、登入失敗鎖定狀態（規格 6.10）。"""

from typing import TYPE_CHECKING

from sqlalchemy import text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.user_session import UserSession


class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(unique=True)
    password_hash: Mapped[str]  # Argon2 雜湊字串，永遠不存明文
    failed_login_count: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    locked_until: Mapped[str | None]

    sessions: Mapped[list["UserSession"]] = relationship(back_populates="user")

    def __repr__(self) -> str:  # 不輸出 password_hash
        return f"<User id={self.id} username={self.username!r}>"
