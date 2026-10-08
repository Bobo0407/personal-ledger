"""transactions：交易表頭（規格 6.6）。

一筆交易的金額都在 postings；表頭只記錄日期、類型、狀態與附帶資訊。
帳務計算只能使用有效分錄：deleted_at IS NULL AND status = 'POSTED'（規格 5.4），
這個條件只寫在 repositories 的 active_postings()，不要在別處自行撰寫。
"""

from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, ForeignKey, Index, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin
from app.models.enums import TxnSource, TxnStatus, TxnType, VoucherType, sql_in

if TYPE_CHECKING:
    from app.models.posting import Posting

# SQLite 的 GLOB 會比對整個字串，而且區分大小寫
DATE_GLOB = "[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]"
INVOICE_NO_GLOB = "[A-Z][A-Z]" + "[0-9]" * 8


class Transaction(TimestampMixin, Base):
    __tablename__ = "transactions"
    __table_args__ = (
        CheckConstraint(sql_in("txn_type", TxnType), name="txn_type"),
        CheckConstraint(sql_in("status", TxnStatus), name="status"),
        CheckConstraint(sql_in("source", TxnSource), name="source"),
        CheckConstraint(
            f"voucher_type IS NULL OR {sql_in('voucher_type', VoucherType)}",
            name="voucher_type",
        ),
        CheckConstraint(f"txn_date GLOB '{DATE_GLOB}'", name="txn_date_format"),
        CheckConstraint(
            f"invoice_no IS NULL OR invoice_no GLOB '{INVOICE_NO_GLOB}'",
            name="invoice_no_format",
        ),
        CheckConstraint(
            "related_txn_id IS NULL OR related_txn_id <> id", name="not_related_to_self"
        ),
        CheckConstraint("exclude_from_budget IN (0, 1)", name="exclude_from_budget_bool"),
        # 防止週期性交易、匯入重複產生；source_ref 為 NULL 時不受限制
        UniqueConstraint("source", "source_ref", name="uq_transactions_source_source_ref"),
        # 發票號碼只在「未刪除」的交易中唯一（V08），軟刪除後可以重新使用
        Index(
            "uq_transactions_invoice_no_active",
            "invoice_no",
            unique=True,
            sqlite_where=text("deleted_at IS NULL AND invoice_no IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    txn_date: Mapped[str]  # YYYY-MM-DD
    txn_type: Mapped[str]
    status: Mapped[str] = mapped_column(
        default=TxnStatus.POSTED.value, server_default=text("'POSTED'")
    )
    description: Mapped[str | None]
    merchant_id: Mapped[int | None] = mapped_column(ForeignKey("merchants.id"))
    payment_method_id: Mapped[int | None] = mapped_column(ForeignKey("payment_methods.id"))
    voucher_type: Mapped[str | None]
    invoice_no: Mapped[str | None]
    exclude_from_budget: Mapped[bool] = mapped_column(default=False, server_default=text("0"))
    related_txn_id: Mapped[int | None] = mapped_column(ForeignKey("transactions.id"))
    source: Mapped[str] = mapped_column(
        default=TxnSource.MANUAL.value, server_default=text("'MANUAL'")
    )
    source_ref: Mapped[str | None]
    deleted_at: Mapped[str | None]  # 軟刪除時間

    postings: Mapped[list["Posting"]] = relationship(
        back_populates="transaction",
        order_by="Posting.line_no",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    related_txn: Mapped["Transaction | None"] = relationship(remote_side=lambda: [Transaction.id])

    def __repr__(self) -> str:
        return f"<Transaction id={self.id} {self.txn_date} {self.txn_type} {self.status}>"
