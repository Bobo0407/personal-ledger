"""載入主資料初始值：科目表、幣別、付款方式、系統設定（P1 Step 2）。

用法（在專案根目錄）：
    alembic upgrade head
    python scripts/seed_master_data.py --dry-run   # 先看會新增什麼，不寫入
    python scripts/seed_master_data.py

- 預設讀 seeds/chart_of_accounts.yaml，可用 --file 指定其他檔案。
- 可以重複執行：只新增缺少的資料，不覆寫、不刪除你已維護的主資料。
- 和資料庫既有資料有結構性衝突（例如同一個科目代碼的五大類不同）時，整批中止，不寫入任何資料。
- 資料庫不在最新的 migration 版本時拒絕執行。
"""

import argparse
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory

from app.config import get_database_settings
from app.db import make_engine, make_sessionmaker
from app.services.master_data_seed import (
    DEFAULT_SEED_FILE,
    SeedConflict,
    SeedFileError,
    seed_master_data,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def migration_heads() -> set[str]:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    return set(ScriptDirectory.from_config(cfg).get_heads())


def main(
    argv: Sequence[str] | None = None,
    database_url: str | None = None,
    out: Callable[[str], None] = print,
) -> int:
    parser = argparse.ArgumentParser(description="載入主資料初始值")
    parser.add_argument("--dry-run", action="store_true", help="只顯示會新增什麼，不寫入")
    parser.add_argument("--file", type=Path, default=DEFAULT_SEED_FILE, help="初始資料 YAML 檔")
    args = parser.parse_args(argv)

    engine = make_engine(database_url or get_database_settings().database_url)
    try:
        with engine.connect() as conn:
            current = set(MigrationContext.configure(conn).get_current_heads())
        if current != migration_heads():
            out("資料庫不是最新的結構，請先執行：alembic upgrade head")
            return 1

        with make_sessionmaker(engine)() as db:
            try:
                report = seed_master_data(db, args.file, dry_run=args.dry_run)
            except (SeedFileError, SeedConflict) as exc:
                out(str(exc))
                return 1

        out("【試算，未寫入】" if args.dry_run else "【完成】")
        for line in report.lines():
            out(line)
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
