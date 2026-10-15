"""執行一次本機加密備份（規格 F29；P3-5A）。由 systemd timer 或手動呼叫（P3-6 再接排程）。

用法（在專案根目錄）：
    python scripts/backup.py

環境變數（或 .env）：DATABASE_URL、BACKUP_DIR、BACKUP_AGE_RECIPIENT（age 公鑰）、BACKUP_AGE_BIN。
不需要 SECRET_KEY。log 寫到 stderr，不含 recipient 或任何金鑰。

結束碼：0 備份成功；1 備份失敗，已盡力寫入 FAILED 紀錄；
        2 無法保證有紀錄（來源資料庫不存在／無法寫入，或 backup_runs 寫入失敗）。
"""

import logging
import sys
from pathlib import Path

# 直接以 `python scripts/backup.py` 執行時，讓 app 套件可以被 import
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import BackupSettings  # noqa: E402
from app.db import make_engine, make_sessionmaker  # noqa: E402
from app.services import backup  # noqa: E402

logger = logging.getLogger("app.backup")


def main() -> int:
    logging.basicConfig(
        stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    try:
        config = backup.config_from_settings(BackupSettings())
    except Exception:
        logger.exception("backup: invalid settings")
        return 2
    if not config.database_path.is_file():
        # 不建立空資料庫、不另外記錄 metadata：只有 log 與 exit code
        logger.error("backup: source database not found; cannot record this run")
        return 2

    engine = make_engine(f"sqlite:///{config.database_path.resolve().as_posix()}")
    session = make_sessionmaker(engine)()
    try:
        run = backup.run_backup(session, config)
    except backup.BackupRecordError:
        return 2
    except backup.BackupFailed:
        return 1
    finally:
        session.close()
        engine.dispose()
    print(f"backup ok: {run.file_name} ({run.size_bytes} bytes)")
    if run.error:
        print(f"warning: {run.error}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
