import logging
import re
from logging.handlers import TimedRotatingFileHandler

import pytest

from app.config import Settings
from app.logging_config import APP_LOGGER, LOG_FILE_NAME, reset_logging, setup_logging


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(_env_file=None, secret_key="test-secret", log_dir=tmp_path / "logs")


def owned_handlers() -> list[logging.Handler]:
    return [h for h in logging.getLogger(APP_LOGGER).handlers if getattr(h, "_ledger_owned", False)]


def flush() -> None:
    for h in owned_handlers():
        h.flush()


def test_writes_to_log_file_with_taipei_time(settings):
    setup_logging(settings)
    logging.getLogger("app.test").info("hello log")
    flush()

    text = (settings.log_dir / LOG_FILE_NAME).read_text(encoding="utf-8")
    assert re.search(
        r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+08:00 INFO \[app\.test\] hello log$",
        text,
        re.MULTILINE,
    )


def test_rotates_daily_and_keeps_14_days(settings):
    setup_logging(settings)
    file_handlers = [h for h in owned_handlers() if isinstance(h, TimedRotatingFileHandler)]
    assert len(file_handlers) == 1
    assert file_handlers[0].when == "MIDNIGHT"
    assert file_handlers[0].backupCount == 14


def test_setup_twice_does_not_duplicate_handlers(settings):
    setup_logging(settings)
    setup_logging(settings)
    assert len(owned_handlers()) == 2  # 一個檔案、一個主控台


def test_reset_removes_handlers(settings):
    setup_logging(settings)
    reset_logging()
    assert owned_handlers() == []


def test_respects_log_level(tmp_path):
    s = Settings(_env_file=None, secret_key="x", log_dir=tmp_path, log_level="WARNING")
    setup_logging(s)
    logging.getLogger("app.test").info("should not appear")
    flush()
    assert "should not appear" not in (tmp_path / LOG_FILE_NAME).read_text(encoding="utf-8")
