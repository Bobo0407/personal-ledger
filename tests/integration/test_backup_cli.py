"""scripts/backup.py 與 scripts/restore_check.py 的結束碼（P3-5A／5B）。

0 成功；1 備份失敗（已盡力寫 FAILED）；2 無法保證有紀錄（來源 DB 不存在、或 backup_runs 寫入失敗）。
在 tmp_path 內執行（chdir），避免讀到開發機專案根目錄的 .env。
"""

import importlib.util
import sqlite3
from pathlib import Path

import pytest

from tests.backup_helpers import make_age_keys, make_local_remote
from tests.ledger_helpers import freeze_today

ROOT = Path(__file__).resolve().parents[2]
LOCAL = ["--local-only"]  # P3-5A 的既有測試只驗本機流程；預設行為（要求 remote）另有測試


def load_script(name: str):
    spec = importlib.util.spec_from_file_location(f"script_{name}", ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    freeze_today(monkeypatch)
    monkeypatch.chdir(tmp_path)
    for key in (
        "BACKUP_DIR",
        "BACKUP_AGE_RECIPIENT",
        "BACKUP_AGE_BIN",
        "DATABASE_URL",
        "RCLONE_REMOTE",
        "RCLONE_PATH",
        "RCLONE_BIN",
        "RCLONE_CONFIG",
    ):
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
    assert load_script("backup").main(LOCAL) == 0
    out = capsys.readouterr()
    assert "backup ok: ledger-20261015-120000.db.gz.age" in out.out
    assert keys.recipient not in out.out + out.err
    assert run_rows(db_url) == [("SUCCESS", "ledger-20261015-120000.db.gz.age")]


def test_pipeline_failure_exits_1_with_a_failed_row(
    migrated_engine, db_url, tmp_path, keys, monkeypatch
):
    configure(monkeypatch, db_url, tmp_path / "bk", keys, recipient="")
    assert load_script("backup").main(LOCAL) == 1
    assert run_rows(db_url) == [("FAILED", None)]


def test_missing_source_database_exits_2_and_creates_nothing(tmp_path, keys, monkeypatch):
    missing = tmp_path / "nope" / "ledger.db"
    configure(monkeypatch, f"sqlite:///{missing.as_posix()}", tmp_path / "bk", keys)
    assert load_script("backup").main(LOCAL) == 2
    assert not missing.exists() and not missing.parent.exists()  # 沒有建立空 DB 或 metadata
    assert not (tmp_path / "bk").exists()


def test_unwritable_backup_runs_exits_2_but_keeps_the_valid_backup(
    migrated_engine, db_url, tmp_path, keys, monkeypatch
):
    from app.repositories import backup_runs as run_repo

    monkeypatch.setattr(run_repo, "add", lambda db, run: (_ for _ in ()).throw(OSError("full")))
    configure(monkeypatch, db_url, tmp_path / "bk", keys)
    assert load_script("backup").main(LOCAL) == 2
    assert [p.name for p in (tmp_path / "bk").iterdir()] == ["ledger-20261015-120000.db.gz.age"]


def test_backup_script_needs_no_secret_key(migrated_engine, db_url, tmp_path, keys, monkeypatch):
    monkeypatch.delenv("SECRET_KEY", raising=False)
    configure(monkeypatch, db_url, tmp_path / "bk", keys)
    assert load_script("backup").main(LOCAL) == 0


def test_restore_check_passes_then_fails_with_wrong_identity(
    migrated_engine, db_url, tmp_path, keys, monkeypatch, capsys
):
    configure(monkeypatch, db_url, tmp_path / "bk", keys)
    assert load_script("backup").main(LOCAL) == 0
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


# --- P3-5B：雲端（remote）--------------------------------------------------------------------


def configure_remote(monkeypatch, remote):
    monkeypatch.setenv("RCLONE_REMOTE", remote.config.remote)
    monkeypatch.setenv("RCLONE_PATH", remote.config.path)
    monkeypatch.setenv("RCLONE_BIN", remote.config.rclone_bin)
    monkeypatch.setenv("RCLONE_CONFIG", str(remote.config.config_file))


def test_default_run_without_remote_is_a_configuration_failure_not_a_silent_downgrade(
    migrated_engine, db_url, tmp_path, keys, monkeypatch, capsys
):
    configure(monkeypatch, db_url, tmp_path / "bk", keys)
    assert load_script("backup").main() == 1  # 沒有 --local-only
    assert run_rows(db_url) == [("FAILED", None)]
    assert not (tmp_path / "bk").exists()  # 沒有產生任何本機檔
    captured = capsys.readouterr()
    assert "backup ok" not in captured.out


def test_local_only_flag_is_the_only_way_to_get_a_local_only_success(
    migrated_engine, db_url, tmp_path, keys, monkeypatch, capsys
):
    configure(monkeypatch, db_url, tmp_path / "bk", keys)
    assert load_script("backup").main(["--local-only"]) == 0
    captured = capsys.readouterr()
    assert "backup ok" in captured.out and "僅本機備份" in captured.err
    assert run_rows(db_url) == [("SUCCESS", "ledger-20261015-120000.db.gz.age")]


def test_configured_remote_uploads_without_any_flag(
    migrated_engine, db_url, tmp_path, keys, monkeypatch, capsys
):
    remote = make_local_remote(tmp_path)
    configure(monkeypatch, db_url, tmp_path / "bk", keys)
    configure_remote(monkeypatch, remote)
    assert load_script("backup").main() == 0
    assert [p.name for p in remote.directory.iterdir()] == ["ledger-20261015-120000.db.gz.age"]
    captured = capsys.readouterr()
    assert "warning" not in captured.err  # 完整成功：沒有任何警告
    assert str(remote.config.config_file) not in captured.out + captured.err


def test_upload_failure_exits_1_and_keeps_the_local_file(
    migrated_engine, db_url, tmp_path, keys, monkeypatch
):
    remote = make_local_remote(tmp_path)
    configure(monkeypatch, db_url, tmp_path / "bk", keys)
    configure_remote(monkeypatch, remote)
    monkeypatch.setenv("RCLONE_REMOTE", "nosuch")
    assert load_script("backup").main() == 1
    assert run_rows(db_url) == [("FAILED", "ledger-20261015-120000.db.gz.age")]
    assert [p.name for p in (tmp_path / "bk").iterdir()] == ["ledger-20261015-120000.db.gz.age"]


def test_blank_rclone_remote_in_env_counts_as_unset(
    migrated_engine, db_url, tmp_path, keys, monkeypatch
):
    configure(monkeypatch, db_url, tmp_path / "bk", keys)
    monkeypatch.setenv("RCLONE_REMOTE", "   ")
    assert load_script("backup").main() == 1
    assert run_rows(db_url) == [("FAILED", None)]


def test_restore_check_remote_latest_round_trip_and_wrong_identity(
    migrated_engine, db_url, tmp_path, keys, monkeypatch, capsys
):
    remote = make_local_remote(tmp_path)
    configure(monkeypatch, db_url, tmp_path / "bk", keys)
    configure_remote(monkeypatch, remote)
    assert load_script("backup").main() == 0
    (remote.directory / "notes.txt").write_bytes(b"unrelated")
    capsys.readouterr()

    restore = load_script("restore_check")
    assert restore.main(["--remote", "latest", "--identity", str(keys.identity)]) == 0
    out = capsys.readouterr().out
    assert "backup: ledger-20261015-120000.db.gz.age" in out
    assert "integrity_check: ok" in out and "alembic_version: 0005" in out
    assert str(keys.identity) not in out and str(remote.config.config_file) not in out

    other = make_age_keys(tmp_path, "other.txt")
    assert restore.main(["--remote", "latest", "--identity", str(other.identity)]) == 1
    assert "restore check FAILED" in capsys.readouterr().err
    # 不認得的檔名一律拒絕，不會下載遠端的其他檔案
    assert restore.main(["--remote", "notes.txt", "--identity", str(keys.identity)]) == 1


def test_restore_check_remote_without_config_fails_clearly(tmp_path, keys, capsys):
    restore = load_script("restore_check")
    assert restore.main(["--remote", "latest", "--identity", str(keys.identity)]) == 1
    assert "RCLONE_REMOTE" in capsys.readouterr().err


def test_restore_check_needs_exactly_one_source(tmp_path, keys):
    restore = load_script("restore_check")
    with pytest.raises(SystemExit):
        restore.main(["--identity", str(keys.identity)])
    with pytest.raises(SystemExit):
        restore.main(["--backup", "a", "--remote", "latest", "--identity", str(keys.identity)])
