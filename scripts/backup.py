"""執行一次加密備份：本機密文 ＋ 上傳雲端（規格 F29；P3-5A／5B）。

由 systemd timer 或手動呼叫（P3-6 再接排程）。

用法（在專案根目錄）：
    python scripts/backup.py              # 正式用法：本機加密 + rclone 上傳 + 驗證
    python scripts/backup.py --local-only # 明確只做本機（開發／CI）；SUCCESS + 黃色「僅本機」警告

環境變數（或 .env）：DATABASE_URL、BACKUP_DIR、BACKUP_AGE_RECIPIENT（age 公鑰）、BACKUP_AGE_BIN、
RCLONE_REMOTE、RCLONE_PATH、RCLONE_BIN、RCLONE_CONFIG。不需要 SECRET_KEY。
log 寫到 stderr，不含 recipient、rclone 設定或任何 token。
未設定 RCLONE_REMOTE 又沒有 --local-only → 視為設定錯誤（FAILED、exit 1），不會默默降級。
P3-6 的正式排程不得使用 --local-only。

結束碼：0 備份成功；1 備份失敗（含雲端上傳失敗、設定錯誤），已盡力寫入 FAILED 紀錄；
        2 無法保證有紀錄（來源資料庫不存在／無法寫入，或 backup_runs 寫入失敗）。
"""

import argparse
import logging
import sys
from pathlib import Path

# 直接以 `python scripts/backup.py` 執行時，讓 app 套件可以被 import
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import BackupSettings  # noqa: E402
from app.db import make_engine, make_sessionmaker  # noqa: E402
from app.services import backup  # noqa: E402

logger = logging.getLogger("app.backup")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="執行一次加密備份（本機 + 雲端）")
    parser.add_argument(
        "--local-only",
        action="store_true",
        help="明確只做本機備份（開發／CI）；正式排程不得使用",
    )
    args = parser.parse_args([] if argv is None else argv)
    logging.basicConfig(
        stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    try:
        config = backup.config_from_settings(BackupSettings(), local_only=args.local_only)
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
    sys.exit(main(sys.argv[1:]))
