"""scripts/backup.py 與 scripts/restore_check.py 的結束碼（P3-5A）。

0 成功；1 備份失敗（已盡力寫 FAILED）；2 無法保證有紀錄（來源 DB 不存在、或 backup_runs 寫入失敗）。
在 tmp_path 內執行（chdir），避免讀到開發機專案根目錄的 .env。
"""

import importlib.util
import sqlite3
from pathlib import Path

import pytest

from tests.backup_helpers import make_age_keys
from tests.ledger_helpers import freeze_today

ROOT = Path(__file__).resolve().parents[2]


def load_script(name: str):
    spec = importlib.util.spec_from_file_location(f"script_{name}", ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    freeze_today(monkeypatch)
    monkeypatch.chdir(tmp_path)
    for key in ("BACKUP_DIR", "BACKUP_AGE_RECIPIENT", "BACKUP_AGE_BIN", "DATABASE_URL"):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def keys(tmp_path):
    return make_age_keys(tmp_path)


def configure(monkeypatch, db_url, backup_dir, keys, recipient="__keys__"):
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("BACKUP_DIR", str(backup_dir))
    monkeypatch.setenv("BACKUP_AGE_BIN", keys.age_bin)
    monkeypatch.setenv(
        "BACKUP_AGE_RECIPIENT", keys.recipient if recipient == "__keys__" else recipient
    )


def run_rows(db_url):
    path = db_url.removeprefix("sqlite:///")
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT status, file_name FROM backup_runs ORDER BY id").fetchall()
    finally:
        conn.close()


def test_success_exits_0_and_prints_the_file_name(
    migrated_engine, db_url, tmp_path, keys, monkeypatch, capsys
):
    configure(monkeypatch, db_url, tmp_path / "bk", keys)
    assert load_script("backup").main() == 0
    out = capsys.readouterr()
    assert "backup ok: ledger-20261015-120000.db.gz.age" in out.out
    assert keys.recipient not in out.out + out.err
    assert run_rows(db_url) == [("SUCCESS", "ledger-20261015-120000.db.gz.age")]


def test_pipeline_failure_exits_1_with_a_failed_row(
    migrated_engine, db_url, tmp_path, keys, monkeypatch
):
    configure(monkeypatch, db_url, tmp_path / "bk", keys, recipient="")
    assert load_script("backup").main() == 1
    assert run_rows(db_url) == [("FAILED", None)]


def test_missing_source_database_exits_2_and_creates_nothing(tmp_path, keys, monkeypatch):
    missing = tmp_path / "nope" / "ledger.db"
    configure(monkeypatch, f"sqlite:///{missing.as_posix()}", tmp_path / "bk", keys)
    assert load_script("backup").main() == 2
    assert not missing.exists() and not missing.parent.exists()  # 沒有建立空 DB 或 metadata
    assert not (tmp_path / "bk").exists()


def test_unwritable_backup_runs_exits_2_but_keeps_the_valid_backup(
    migrated_engine, db_url, tmp_path, keys, monkeypatch
):
    from app.repositories import backup_runs as run_repo

    monkeypatch.setattr(run_repo, "add", lambda db, run: (_ for _ in ()).throw(OSError("full")))
    configure(monkeypatch, db_url, tmp_path / "bk", keys)
    assert load_script("backup").main() == 2
    assert [p.name for p in (tmp_path / "bk").iterdir()] == ["ledger-20261015-120000.db.gz.age"]


def test_backup_script_needs_no_secret_key(migrated_engine, db_url, tmp_path, keys, monkeypatch):
    monkeypatch.delenv("SECRET_KEY", raising=False)
    configure(monkeypatch, db_url, tmp_path / "bk", keys)
    assert load_script("backup").main() == 0


def test_restore_check_passes_then_fails_with_wrong_identity(
    migrated_engine, db_url, tmp_path, keys, monkeypatch, capsys
):
    configure(monkeypatch, db_url, tmp_path / "bk", keys)
    assert load_script("backup").main() == 0
    backup_file = next((tmp_path / "bk").iterdir())
    capsys.readouterr()

    restore = load_script("restore_check")
    args = ["--backup", str(backup_file), "--identity", str(keys.identity)]
    assert restore.main(args) == 0
    out = capsys.readouterr().out
    assert "integrity_check: ok" in out and "alembic_version: 0005" in out
    assert str(keys.identity) not in out

    other = make_age_keys(tmp_path, "other.txt")
    assert restore.main(["--backup", str(backup_file), "--identity", str(other.identity)]) == 1
    assert "restore check FAILED" in capsys.readouterr().err
