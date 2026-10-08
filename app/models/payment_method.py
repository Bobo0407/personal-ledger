"""payment_methods：付款管道，對應實際扣款的帳戶（規格 6.3）。

LINE Pay、台灣Pay、簽帳卡都是管道，實際扣的是郵局帳戶。
預設付款方式只存在 is_default（決策 D4），由部分唯一索引保證全系統最多一個。
「account_id 必須是資產或負債」由服務層檢查。
"""

from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, ForeignKey, Index, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.account import Account


class PaymentMethod(TimestampMixin, Base):
    __tablename__ = "payment_methods"
    __table_args__ = (
        CheckConstraint("is_default IN (0, 1)", name="is_default_bool"),
        CheckConstraint("is_active IN (0, 1)", name="is_active_bool"),
        Index(
            "uq_payment_methods_single_default",
            "is_default",
            unique=True,
            sqlite_where=text("is_default = 1"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(unique=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    is_default: Mapped[bool] = mapped_column(default=False, server_default=text("0"))
    is_active: Mapped[bool] = mapped_column(default=True, server_default=text("1"))
    sort_order: Mapped[int] = mapped_column(default=0, server_default=text("0"))

    account: Mapped["Account"] = relationship()

    def __repr__(self) -> str:
        return f"<PaymentMethod id={self.id} name={self.name!r}>"
