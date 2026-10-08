"""ORM 基底：命名慣例與共用時間欄位。"""

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app import clock

# 固定約束名稱：SQLite 的 ALTER TABLE 能力有限，Alembic 以 batch 模式重建資料表時
# 需要靠名稱找到約束，所以所有 PK／FK／UNIQUE 都必須有可預期的名稱。
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def _now_iso() -> str:
    # 執行時才查 clock.now_iso，測試可以替換掉時間來源
    return clock.now_iso()


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class TimestampMixin:
    """規格第 6 節：所有資料表都有 created_at、updated_at（TEXT，ISO 8601，台北時區）。"""

    created_at: Mapped[str] = mapped_column(default=_now_iso)
    updated_at: Mapped[str] = mapped_column(default=_now_iso, onupdate=_now_iso)
