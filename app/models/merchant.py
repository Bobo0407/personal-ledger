"""merchants：商家（規格 6.4）。

P1 只建表（決策 D5），讓 transactions.merchant_id 一次定型；商家服務與自動完成在 P2。
"""

from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, ForeignKey, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.merchant_alias import MerchantAlias


class Merchant(TimestampMixin, Base):
    __tablename__ = "merchants"
    __table_args__ = (CheckConstraint("is_active IN (0, 1)", name="is_active_bool"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(unique=True)  # 標準名稱，例如「7-11」
    default_account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"))  # V1.5
    default_payment_method_id: Mapped[int | None] = mapped_column(
        ForeignKey("payment_methods.id")
    )  # V1.5
    is_active: Mapped[bool] = mapped_column(default=True, server_default=text("1"))

    aliases: Mapped[list["MerchantAlias"]] = relationship(back_populates="merchant")

    def __repr__(self) -> str:
        return f"<Merchant id={self.id} name={self.name!r}>"
