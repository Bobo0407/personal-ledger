"""建立備份執行紀錄：backup_runs（規格 6、F29；P3-5A）

- backup_runs：一次備份結束時寫一筆。status 只有 SUCCESS／FAILED（沒有 RUNNING）。
- SUCCESS 必須有 file_name、size_bytes；FAILED 必須有 error。
- 不儲存備份內容，也不與帳務資料表有任何外鍵。
- downgrade 只刪 backup_runs（備份紀錄會遺失），備份檔與帳務資料不受影響。

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-15

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATUS_CHECK = "status IN ('SUCCESS', 'FAILED')"
SHAPE_CHECK = (
    "(status = 'SUCCESS' AND file_name IS NOT NULL AND size_bytes IS NOT NULL"
    " AND size_bytes >= 0) OR (status = 'FAILED' AND error IS NOT NULL)"
)


def upgrade() -> None:
    op.create_table(
        "backup_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.String(), nullable=False),
        sa.Column("finished_at", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("file_name", sa.String(), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("error", sa.String(), nullable=True),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.CheckConstraint(STATUS_CHECK, name=op.f("ck_backup_runs_status_valid")),
        sa.CheckConstraint(SHAPE_CHECK, name=op.f("ck_backup_runs_status_shape")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_backup_runs")),
    )


def downgrade() -> None:
    op.drop_table("backup_runs")
