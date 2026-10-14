"""budgets：第一層費用科目的月預算（規格 6.9、F20、M05；P3-3A）。

- 只存「預算金額」與「從哪個月起生效」；已用金額、使用率一律由有效分錄即時計算，不儲存。
- 調整預算 = 新增一筆較新的 effective_from；指定月份採用 effective_from ≤ 該月的最新一筆。
- M05（只能設在第一層費用科目）需要查 accounts，由 services/budgets.py 檢查，資料庫不做。
- account_id 外鍵不 cascade：有預算的科目不能被刪除（services/accounts.py 先給友善訊息）。
"""

from sqlalchemy import CheckConstraint, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin

MONTH_GLOB = "[0-9][0-9][0-9][0-9]-[0-1][0-9]"
EFFECTIVE_FROM_CHECK = (
    f"effective_from GLOB '{MONTH_GLOB}' AND substr(effective_from, 6, 2) BETWEEN '01' AND '12'"
)


class Budget(TimestampMixin, Base):
    __tablename__ = "budgets"
    __table_args__ = (
        UniqueConstraint(
            "account_id", "effective_from", name="uq_budgets_account_id_effective_from"
        ),
        CheckConstraint("monthly_amount > 0", name="monthly_amount_positive"),
        CheckConstraint(EFFECTIVE_FROM_CHECK, name="effective_from_format"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    monthly_amount: Mapped[int]
    effective_from: Mapped[str]  # YYYY-MM

    def __repr__(self) -> str:
        return (
            f"<Budget account={self.account_id} from={self.effective_from} {self.monthly_amount}>"
        )
