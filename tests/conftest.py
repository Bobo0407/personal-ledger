"""共用 fixture：每個測試使用獨立的暫存 SQLite（規格第 11 節）。"""

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from app.db import make_engine, make_sessionmaker
from app.logging_config import reset_logging

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _close_log_handlers():
    """每個測試結束後關閉日誌檔，Windows 才能刪除暫存目錄，也避免 handler 累積。"""
    yield
    reset_logging()


@pytest.fixture
def db_url(tmp_path) -> str:
    return f"sqlite:///{(tmp_path / 'test.db').as_posix()}"


@pytest.fixture
def alembic_cfg(db_url) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    cfg.attributes["configure_logger"] = False
    return cfg


@pytest.fixture
def migrated_engine(alembic_cfg, db_url):
    """已跑完 `alembic upgrade head` 的資料庫，用的是真正的 migration，不是 create_all。"""
    command.upgrade(alembic_cfg, "head")
    engine = make_engine(db_url)
    yield engine
    engine.dispose()


TEST_CHART = PROJECT_ROOT / "tests" / "fixtures" / "chart_of_accounts_test.yaml"


@pytest.fixture
def ledger_db(migrated_engine):
    """已升級到最新 migration 的空資料庫 Session。"""
    session = make_sessionmaker(migrated_engine)()
    yield session
    session.rollback()
    session.close()


@pytest.fixture
def seeded_db(ledger_db):
    """載入測試用科目表（tests/fixtures/chart_of_accounts_test.yaml）後的 Session。"""
    from app.services.master_data_seed import seed_master_data

    seed_master_data(ledger_db, TEST_CHART)
    return ledger_db
