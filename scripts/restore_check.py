"""驗證一份加密備份可以還原（restore smoke test；規格 F29「沒還原過的備份不算備份」）。

用法：
    python scripts/restore_check.py --backup <備份檔.age> --identity <age 私鑰檔>
    python scripts/restore_check.py --remote latest|<檔名> --identity <age 私鑰檔>   # P3-5B

--remote：用 .env／環境變數的 RCLONE_* 設定，先從遠端把密文下載到暫存目錄（結束就刪），
latest 只會從「檔名符合備份格式」的檔案中選最新的，遠端資料夾裡的其他檔案一律忽略。
解密 → gunzip → 以記憶體 SQLite 開啟 → integrity_check → 顯示 alembic 版本與主要表筆數。
明文只存在記憶體，不寫入磁碟；私鑰路徑與內容不會被印出；不會碰任何正式資料庫。
「Docker 啟動、各帳戶餘額與正式環境一致」是 P3-7 launch gate 的人工步驟，不在這裡。

結束碼：0 通過；1 失敗。
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import BackupSettings  # noqa: E402
from app.services import backup  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="驗證加密備份可以解密並開啟")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--backup", type=Path, help="本機備份檔")
    source.add_argument("--remote", metavar="latest|FILENAME", help="從遠端下載後驗證")
    parser.add_argument("--identity", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        settings = BackupSettings()
        if args.remote is not None:
            remote = backup.config_from_settings(settings).remote
            if remote is None:
                raise backup.BackupStepError("未設定 RCLONE_REMOTE，無法從遠端驗證")
            name, report = backup.verify_remote_backup(
                remote, args.remote, args.identity, settings.backup_age_bin
            )
            print(f"backup: {name}")
        else:
            report = backup.verify_backup(args.backup, args.identity, settings.backup_age_bin)
    except backup.BackupStepError as exc:
        print(f"restore check FAILED: {exc}", file=sys.stderr)
        return 1
    print(f"integrity_check: {'ok' if report.integrity_ok else 'FAILED'}")
    print(f"alembic_version: {report.alembic_version}")
    for table, count in report.row_counts.items():
        print(f"{table}: {count}")
    return 0 if report.integrity_ok else 1


if __name__ == "__main__":
    sys.exit(main())
