"""postings：分錄（規格 6.7）。

- amount 單一欄位帶正負號：借方為正、貸方為負，單位為新台幣元；每筆交易加總必須為 0。
- 「加總為 0」「科目必須是最末層、啟用中」無法用單列 CHECK 表達，
  由 LedgerService 驗證，並由每日帳平檢查（integrity）兜底。
- 只有 LedgerService 可以寫入這張表。
"""

from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin
from app.models.enums import LineRole, sql_in

if TYPE_CHECKING:
    from app.models.account import Account
    from app.models.transaction import Transaction


class Posting(TimestampMixin, Base):
    __tablename__ = "postings"
    __table_args__ = (
        CheckConstraint("amount <> 0", name="amount_nonzero"),
        CheckConstraint("line_no >= 1", name="line_no_positive"),
        CheckConstraint(f"line_role IS NULL OR {sql_in('line_role', LineRole)}", name="line_role"),
        # 原幣別與原幣金額必須同時為空或同時有值（V09）
        CheckConstraint(
            "(original_currency IS NULL) = (original_amount_minor IS NULL)",
            name="original_currency_pair",
        ),
        UniqueConstraint("transaction_id", "line_no", name="uq_postings_transaction_id_line_no"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    transaction_id: Mapped[int] = mapped_column(
        ForeignKey("transactions.id", ondelete="CASCADE"), index=True
    )
    line_no: Mapped[int]  # 同一筆交易內的順序，從 1 開始
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    amount: Mapped[int]
    line_role: Mapped[str | None]
    original_currency: Mapped[str | None] = mapped_column(ForeignKey("currencies.code"))
    original_amount_minor: Mapped[int | None]  # 原幣最小單位，依 currencies.minor_unit 換算
    memo: Mapped[str | None]

    transaction: Mapped["Transaction"] = relationship(back_populates="postings")
    account: Mapped["Account"] = relationship()

    def __repr__(self) -> str:
        return (
            f"<Posting id={self.id} txn={self.transaction_id} line={self.line_no} "
            f"account={self.account_id} amount={self.amount}>"
        )
