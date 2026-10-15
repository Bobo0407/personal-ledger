"""驗證一份加密備份可以還原（P3-5A restore smoke test；規格 F29「沒還原過的備份不算備份」）。

用法：
    python scripts/restore_check.py --backup <備份檔.age> --identity <age 私鑰檔>

解密 → gunzip → 以記憶體 SQLite 開啟 → integrity_check → 顯示 alembic 版本與主要表筆數。
明文只存在記憶體，不寫入磁碟；私鑰路徑與內容不會被印出。
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
    parser.add_argument("--backup", required=True, type=Path)
    parser.add_argument("--identity", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        report = backup.verify_backup(args.backup, args.identity, BackupSettings().backup_age_bin)
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
