"""應用程式日誌（規格 9.4）。

- 寫入 LOG_DIR/app.log，每天午夜輪替，保留 14 份舊檔。
- 時間戳記一律是台北時區 ISO 8601。
- 只設定 `app` 這個 logger（程式內用 logging.getLogger(__name__)），不動 root 與 uvicorn。
- 日誌不得包含密碼、Session 權杖、交易金額；只記 ID 與原因代碼。
"""

import logging
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler

from app.clock import APP_TIMEZONE
from app.config import Settings

APP_LOGGER = "app"
LOG_FILE_NAME = "app.log"
BACKUP_DAYS = 14
_OWNED = "_ledger_owned"  # 標記由這個模組加上的 handler，重複設定時才知道要移除哪些

LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"


class TaipeiFormatter(logging.Formatter):
    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:  # noqa: N802
        return datetime.fromtimestamp(record.created, APP_TIMEZONE).isoformat(timespec="seconds")


def reset_logging() -> None:
    """移除並關閉之前加上的 handler（重複呼叫 create_app 或測試結束時使用）。"""
    logger = logging.getLogger(APP_LOGGER)
    for handler in list(logger.handlers):
        if getattr(handler, _OWNED, False):
            logger.removeHandler(handler)
            handler.close()


def setup_logging(settings: Settings) -> None:
    reset_logging()
    settings.log_dir.mkdir(parents=True, exist_ok=True)

    file_handler = TimedRotatingFileHandler(
        settings.log_dir / LOG_FILE_NAME,
        when="midnight",
        backupCount=BACKUP_DAYS,
        encoding="utf-8",
    )
    console_handler = logging.StreamHandler()

    formatter = TaipeiFormatter(LOG_FORMAT)
    logger = logging.getLogger(APP_LOGGER)
    for handler in (file_handler, console_handler):
        handler.setFormatter(formatter)
        setattr(handler, _OWNED, True)
        logger.addHandler(handler)

    logger.setLevel(settings.log_level)
    logger.propagate = False
