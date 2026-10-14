"""建立預算：budgets（規格 6.9、F20；P3-3A）

- budgets：第一層費用科目的月預算。effective_from 是 YYYY-MM，調整金額時新增一筆，
  指定月份採用 effective_from ≤ 該月的最新一筆。
- UNIQUE (account_id, effective_from) 已涵蓋「依科目查」，不另外建索引。
- 不儲存已用金額或使用率：一律由有效分錄（active_postings()）即時計算。
- M05（只能設在第一層費用科目）由 services/budgets.py 檢查。
- downgrade 只刪 budgets（預算資料會遺失），帳戶、交易、分錄不受影響。

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-14

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MONTH_GLOB = "[0-9][0-9][0-9][0-9]-[0-1][0-9]"
EFFECTIVE_FROM_CHECK = (
    f"effective_from GLOB '{MONTH_GLOB}' AND substr(effective_from, 6, 2) BETWEEN '01' AND '12'"
)


def upgrade() -> None:
    op.create_table(
        "budgets",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("monthly_amount", sa.Integer(), nullable=False),
        sa.Column("effective_from", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.CheckConstraint("monthly_amount > 0", name=op.f("ck_budgets_monthly_amount_positive")),
        sa.CheckConstraint(
            EFFECTIVE_FROM_CHECK, name=op.f("ck_budgets_effective_from_format")
        ),
        sa.ForeignKeyConstraint(
            ["account_id"], ["accounts.id"], name=op.f("fk_budgets_account_id_accounts")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_budgets")),
        sa.UniqueConstraint(
            "account_id", "effective_from", name=op.f("uq_budgets_account_id_effective_from")
        ),
    )


def downgrade() -> None:
    op.drop_table("budgets")
