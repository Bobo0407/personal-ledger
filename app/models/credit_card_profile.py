"""credit_card_profiles：信用卡結帳日、繳款日、扣繳帳戶（規格 6.2）。

和 accounts 一對一，主鍵就是信用卡的 account_id。
P1 只建表；「信用卡帳戶必須有這筆設定」（M07）與「必須是 CREDIT_CARD 子類型」由服務層檢查，
帳單週期計算留到 P3（決策 D5）。
"""

from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.account import Account


class CreditCardProfile(TimestampMixin, Base):
    __tablename__ = "credit_card_profiles"
    __table_args__ = (
        CheckConstraint("closing_day BETWEEN 1 AND 31", name="closing_day_range"),
        CheckConstraint("due_day BETWEEN 1 AND 31", name="due_day_range"),
        CheckConstraint("credit_limit IS NULL OR credit_limit > 0", name="credit_limit_positive"),
    )

    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), primary_key=True)
    closing_day: Mapped[int]  # 結帳日；該月沒有這天時取月底
    due_day: Mapped[int]  # 繳款截止日
    autopay_account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"))
    credit_limit: Mapped[int | None]

    account: Mapped["Account"] = relationship(foreign_keys=[account_id])
    autopay_account: Mapped["Account | None"] = relationship(foreign_keys=[autopay_account_id])

    def __repr__(self) -> str:
        return f"<CreditCardProfile account_id={self.account_id}>"
