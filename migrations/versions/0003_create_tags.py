"""建立標籤：tags、transaction_tags（規格 6.8、F19；P2 Step 8A、ADR-007）

- tags：name 精確 UNIQUE、is_active 軟停用（沒有 deleted_at）。
- transaction_tags：(transaction_id, tag_id) 複合主鍵；兩個外鍵都不 cascade。
  主鍵已涵蓋以 transaction_id 開頭的查詢；ix_transaction_tags_tag_id 給「依標籤找交易」。
- 規格 6：所有資料表都有 created_at、updated_at；布林以 CHECK IN (0, 1) 限制。
- downgrade 只刪這兩張表（標籤資料會遺失），交易、分錄不受影響。

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-13

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "tags",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("1"), nullable=False),
        *_timestamps(),
        sa.CheckConstraint("is_active IN (0, 1)", name=op.f("ck_tags_is_active_bool")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tags")),
        sa.UniqueConstraint("name", name=op.f("uq_tags_name")),
    )
    op.create_table(
        "transaction_tags",
        sa.Column("transaction_id", sa.Integer(), nullable=False),
        sa.Column("tag_id", sa.Integer(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["transaction_id"],
            ["transactions.id"],
            name=op.f("fk_transaction_tags_transaction_id_transactions"),
        ),
        sa.ForeignKeyConstraint(
            ["tag_id"], ["tags.id"], name=op.f("fk_transaction_tags_tag_id_tags")
        ),
        sa.PrimaryKeyConstraint("transaction_id", "tag_id", name=op.f("pk_transaction_tags")),
    )
    op.create_index(
        op.f("ix_transaction_tags_tag_id"), "transaction_tags", ["tag_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_transaction_tags_tag_id"), table_name="transaction_tags")
    op.drop_table("transaction_tags")
    op.drop_table("tags")
