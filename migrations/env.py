"""Alembic 執行環境。

- 資料庫網址：優先使用 Config 上的 sqlalchemy.url（測試用），否則讀 app.config 的 DATABASE_URL。
- 連線一律透過 app.db.make_engine 建立，確保 migration 期間也開啟外鍵檢查。
- render_as_batch=True：SQLite 不支援大部分 ALTER TABLE，改欄位時由 Alembic 重建資料表。
"""

from logging.config import fileConfig

from alembic import context

import app.models  # noqa: F401  匯入所有模型，讓 metadata 完整
from app.db import make_engine
from app.models.base import Base

config = context.config

# 測試時設定 configure_logger=False，避免 fileConfig 改掉 pytest 的日誌設定
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def get_url() -> str:
    url = config.get_main_option("sqlalchemy.url")
    if url:
        return url
    # 只讀 DATABASE_URL，migration 不需要 SECRET_KEY 等應用程式設定
    from app.config import get_database_settings

    return get_database_settings().database_url


def run_migrations_offline() -> None:
    context.configure(
        url=get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = make_engine(get_url())
    try:
        with engine.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                render_as_batch=True,
            )
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
