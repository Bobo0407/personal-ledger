"""currencies：幣別與小數位數（規格 6.5）。

外幣只作為分錄的原幣資訊；記帳金額一律是新台幣元。
"""

from sqlalchemy import CheckConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class Currency(TimestampMixin, Base):
    __tablename__ = "currencies"
    __table_args__ = (CheckConstraint("minor_unit >= 0", name="minor_unit_non_negative"),)

    code: Mapped[str] = mapped_column(primary_key=True)  # ISO 4217，例如 USD
    name: Mapped[str]
    minor_unit: Mapped[int]  # 小數位數：USD 2、JPY 0

    def __repr__(self) -> str:
        return f"<Currency {self.code}>"
