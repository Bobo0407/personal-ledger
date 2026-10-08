"""settings：系統設定（規格 6.10）。

初始值（large_amount_threshold = 10000、budget_alert_pct = 80）由 Step 2 的 seed 寫入。
預設付款方式不放這裡，只存在 payment_methods.is_default（決策 D4）。
"""

from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class Setting(TimestampMixin, Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(primary_key=True)
    value: Mapped[str]

    def __repr__(self) -> str:
        return f"<Setting {self.key}={self.value!r}>"
