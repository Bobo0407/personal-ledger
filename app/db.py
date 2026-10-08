"""資料庫連線：Engine、Session 與 SQLite PRAGMA（規格 5.3）。

Engine 採延遲建立，import 這個模組不會讀設定或碰到檔案系統，
測試可以直接用 `make_engine()` 對暫存檔建立獨立的 Engine。
"""

from collections.abc import Iterator
from functools import lru_cache
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_database_settings


def _set_sqlite_pragmas(dbapi_connection, _connection_record) -> None:
    # SQLite 預設不檢查外鍵，而且這個設定是「每條連線」各自生效，
    # 所以必須掛在 connect 事件上，不能只在啟動時執行一次。
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys = ON")
    cursor.close()


def _ensure_sqlite_dir(database_url: str) -> None:
    database = make_url(database_url).database
    if database and database != ":memory:":
        Path(database).parent.mkdir(parents=True, exist_ok=True)


def make_engine(database_url: str) -> Engine:
    url = make_url(database_url)
    if url.get_backend_name() != "sqlite":
        raise ValueError(f"只支援 SQLite，收到：{url.get_backend_name()}")

    _ensure_sqlite_dir(database_url)
    engine = create_engine(
        database_url,
        # FastAPI 會在執行緒池中處理同步路由，連線可能跨執行緒使用
        connect_args={"check_same_thread": False},
    )
    event.listen(engine, "connect", _set_sqlite_pragmas)
    return engine


def make_sessionmaker(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@lru_cache
def get_engine() -> Engine:
    return make_engine(get_database_settings().database_url)


@lru_cache
def get_sessionmaker() -> sessionmaker[Session]:
    return make_sessionmaker(get_engine())


def get_db() -> Iterator[Session]:
    """FastAPI 依賴：每個請求一個 Session，結束時關閉。"""
    session = get_sessionmaker()()
    try:
        yield session
    finally:
        session.close()
