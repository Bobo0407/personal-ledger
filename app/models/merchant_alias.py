"""merchant_aliases：商家別名（規格 6.4）。

alias_normalized 是去除空白、全形轉半形、轉小寫後的比對用字串，全系統唯一（M06）。
正規化規則在 P2 的 merchants 服務實作。
"""

from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.merchant import Merchant


class MerchantAlias(TimestampMixin, Base):
    __tablename__ = "merchant_aliases"

    id: Mapped[int] = mapped_column(primary_key=True)
    merchant_id: Mapped[int] = mapped_column(ForeignKey("merchants.id"), index=True)
    alias: Mapped[str]  # 例如「統一超商股份有限公司」
    alias_normalized: Mapped[str] = mapped_column(unique=True)

    merchant: Mapped["Merchant"] = relationship(back_populates="aliases")

    def __repr__(self) -> str:
        return f"<MerchantAlias id={self.id} alias={self.alias!r}>"
