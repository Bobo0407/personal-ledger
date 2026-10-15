"""本機加密備份 service（P3-5A；規格 F29、7.2）。使用真的 age 執行檔。

時鐘固定在 2026-10-15 12:00（台北）。
FAILED 紀錄的保證範圍：來源 DB 仍可寫、流程失敗 → 嘗試寫 FAILED；
backup_runs 本身寫不進去 → BackupRecordError，不保證有紀錄（見 backup.py 模組說明）。
"""

import hashlib
import logging
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.models import Account, BackupRun
from app.repositories import backup_runs as run_repo
from app.services import backup
from tests.backup_helpers import add_run, db_path_of, make_age_keys, make_config
from tests.ledger_helpers import freeze_today

FIRST_NAME = "ledger-20261015-120000.db.gz.age"
BAD_RECIPIENT = "age1" + "q" * 58  # 格式像公鑰，但 bech32 檢查碼不對 → age 會拒絕


@pytest.fixture(autouse=True)
def _today(monkeypatch):
    freeze_today(monkeypatch)


@pytest.fixture
def keys(tmp_path):
    return make_age_keys(tmp_path)


@pytest.fixture
def backup_dir(tmp_path) -> Path:
    return tmp_path / "backups"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def runs(db):
    db.expire_all()
    return list(db.scalars(select(BackupRun).order_by(BackupRun.id)))


def leftovers(directory: Path):
    return sorted(p.name for p in directory.iterdir()) if directory.exists() else []


# --- 成功、往返 ------------------------------------------------------------------------------


def test_success_writes_file_and_success_row(seeded_db, keys, backup_dir):
    run = backup.run_backup(seeded_db, make_config(seeded_db, backup_dir, keys))
    assert run.status == "SUCCESS" and run.error == backup.LOCAL_ONLY_WARNING
    assert run.file_name == FIRST_NAME
    assert run.size_bytes == (backup_dir / FIRST_NAME).stat().st_size > 0
    assert run.started_at == "2026-10-15T12:00:00+08:00"
    assert run.finished_at == "2026-10-15T12:00:00+08:00"
    assert [r.id for r in runs(seeded_db)] == [run.id]
    assert leftovers(backup_dir) == [FIRST_NAME]  # 沒有 .part 或其他暫存檔


def test_backup_file_is_age_ciphertext_not_gzip_or_sqlite(seeded_db, keys, backup_dir):
    backup.run_backup(seeded_db, make_config(seeded_db, backup_dir, keys))
    content = (backup_dir / FIRST_NAME).read_bytes()
    assert content.startswith(b"age-encryption.org/v1")
    assert not content.startswith(b"\x1f\x8b")
    assert b"SQLite format 3" not in content


def test_backup_decrypts_and_sqlite_is_readable_and_matches_source(seeded_db, keys, backup_dir):
    source_accounts = seeded_db.scalar(select(text("count(*)")).select_from(Account))
    assert source_accounts > 0
    backup.run_backup(seeded_db, make_config(seeded_db, backup_dir, keys))
    report = backup.verify_backup(backup_dir / FIRST_NAME, keys.identity, keys.age_bin)
    assert report.integrity_ok
    assert report.alembic_version == "0005"
    assert report.row_counts["accounts"] == source_accounts
    assert report.row_counts["postings"] == 0


def test_round_trip_data_equals_source_rows(seeded_db, keys, backup_dir):
    """用 age 命令列解密、解壓，逐列比對 accounts 與來源完全相同。"""
    import gzip
    import subprocess

    backup.run_backup(seeded_db, make_config(seeded_db, backup_dir, keys))
    plain = subprocess.run(
        [keys.age_bin, "-d", "-i", str(keys.identity), str(backup_dir / FIRST_NAME)],
        check=True,
        capture_output=True,
    ).stdout
    conn = sqlite3.connect(":memory:")
    conn.deserialize(gzip.decompress(plain))
    restored = conn.execute("SELECT id, code, name FROM accounts ORDER BY id").fetchall()
    conn.close()
    source = seeded_db.execute(text("SELECT id, code, name FROM accounts ORDER BY id")).fetchall()
    assert restored == [tuple(r) for r in source]


def test_snapshot_does_not_modify_source_database(seeded_db, keys, backup_dir):
    path = db_path_of(seeded_db)
    before, mtime = sha256(path), path.stat().st_mtime_ns
    config = make_config(seeded_db, backup_dir, keys)
    backup._create_backup(config, backup.clock.now())  # 只跑 pipeline，不寫 backup_runs
    assert sha256(path) == before and path.stat().st_mtime_ns == mtime


def test_no_plaintext_database_is_left_anywhere(seeded_db, keys, backup_dir, tmp_path):
    backup.run_backup(seeded_db, make_config(seeded_db, backup_dir, keys))
    source = db_path_of(seeded_db)
    for path in tmp_path.rglob("*"):
        if path.is_file() and path != source and not path.name.startswith(source.name):
            head = path.read_bytes()[:16]
            assert head != b"SQLite format 3\x00", path
            assert not head.startswith(b"\x1f\x8b"), path


def test_same_second_runs_get_numbered_names(seeded_db, keys, backup_dir):
    config = make_config(seeded_db, backup_dir, keys)
    names = [backup.run_backup(seeded_db, config).file_name for _ in range(3)]
    assert names == [
        FIRST_NAME,
        "ledger-20261015-120000-2.db.gz.age",
        "ledger-20261015-120000-3.db.gz.age",
    ]


def test_backup_dir_is_created_when_missing(seeded_db, keys, tmp_path):
    nested = tmp_path / "a" / "b" / "backups"
    backup.run_backup(seeded_db, make_config(seeded_db, nested, keys))
    assert leftovers(nested) == [FIRST_NAME]


# --- 失敗：盡力寫 FAILED，然後丟 BackupFailed -----------------------------------------------------


def assert_failed(db, backup_dir, expected_in_error, *, secrets=()):
    rows = runs(db)
    assert [r.status for r in rows] == ["FAILED"]
    assert rows[0].file_name is None and rows[0].size_bytes is None
    assert expected_in_error in rows[0].error
    assert len(rows[0].error) <= backup.ERROR_MAX
    for secret in secrets:
        assert secret not in rows[0].error
    assert [n for n in leftovers(backup_dir) if n.endswith(".part")] == []  # 沒有半成品


def test_missing_recipient_records_failed(seeded_db, keys, backup_dir):
    config = make_config(seeded_db, backup_dir, keys, recipient=None)
    with pytest.raises(backup.BackupFailed) as exc_info:
        backup.run_backup(seeded_db, config)
    assert isinstance(exc_info.value.__cause__, backup.BackupStepError)  # 原例外沒有被吞掉
    assert_failed(seeded_db, backup_dir, "BACKUP_AGE_RECIPIENT")


@pytest.mark.parametrize(
    "value", ["AGE-SECRET-KEY-1QQQQQQQQQQQQQQQQQQQQQQQQQQ", "not-a-key", "age1"]
)
def test_malformed_recipient_is_rejected_without_echoing_it(seeded_db, keys, backup_dir, value):
    config = make_config(seeded_db, backup_dir, keys, recipient=value)
    with pytest.raises(backup.BackupFailed):
        backup.run_backup(seeded_db, config)
    assert_failed(seeded_db, backup_dir, "格式不正確", secrets=[value])


def test_age_rejecting_the_recipient_records_failed_without_leaking_it(seeded_db, keys, backup_dir):
    config = make_config(seeded_db, backup_dir, keys, recipient=BAD_RECIPIENT)
    with pytest.raises(backup.BackupFailed):
        backup.run_backup(seeded_db, config)
    assert_failed(seeded_db, backup_dir, "age 失敗", secrets=[BAD_RECIPIENT])


def test_missing_age_binary_records_failed(seeded_db, keys, backup_dir):
    config = make_config(seeded_db, backup_dir, keys, age_bin="no-such-age-binary-xyz")
    with pytest.raises(backup.BackupFailed):
        backup.run_backup(seeded_db, config)
    assert_failed(seeded_db, backup_dir, "找不到 age")


def test_unwritable_backup_dir_records_failed(seeded_db, keys, tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a directory")
    config = make_config(seeded_db, blocker / "backups", keys)
    with pytest.raises(backup.BackupFailed):
        backup.run_backup(seeded_db, config)
    assert [r.status for r in runs(seeded_db)] == ["FAILED"]


def test_missing_source_database_records_failed_in_a_writable_db(seeded_db, keys, backup_dir):
    """寫 backup_runs 的 DB 仍可用（這裡是另一個連線），來源檔案不存在 → FAILED，不建立空 DB。"""
    missing = backup_dir.parent / "nope.db"
    config = make_config(seeded_db, backup_dir, keys, database_path=missing)
    with pytest.raises(backup.BackupFailed):
        backup.run_backup(seeded_db, config)
    assert_failed(seeded_db, backup_dir, "來源資料庫不存在")
    assert not missing.exists()


def test_corrupt_source_database_records_failed(seeded_db, keys, backup_dir, tmp_path):
    corrupt = tmp_path / "corrupt.db"
    corrupt.write_bytes(b"this is not a sqlite database" * 100)
    config = make_config(seeded_db, backup_dir, keys, database_path=corrupt)
    with pytest.raises(backup.BackupFailed):
        backup.run_backup(seeded_db, config)
    assert [r.status for r in runs(seeded_db)] == ["FAILED"]
    assert leftovers(backup_dir) == []


def test_failed_run_is_followed_by_a_clean_success(seeded_db, keys, backup_dir):
    with pytest.raises(backup.BackupFailed):
        backup.run_backup(seeded_db, make_config(seeded_db, backup_dir, keys, recipient=None))
    backup.run_backup(seeded_db, make_config(seeded_db, backup_dir, keys))
    assert [r.status for r in runs(seeded_db)] == ["FAILED", "SUCCESS"]


# --- 無法寫 backup_runs：不保證有紀錄 ------------------------------------------------------------


def test_record_failure_on_success_keeps_the_valid_backup_file(
    seeded_db, keys, backup_dir, monkeypatch, caplog
):
    def boom(db, run):
        raise RuntimeError("disk I/O error")

    monkeypatch.setattr(run_repo, "add", boom)
    with caplog.at_level(logging.ERROR), pytest.raises(backup.BackupRecordError):
        backup.run_backup(seeded_db, make_config(seeded_db, backup_dir, keys))
    assert "could not write backup_runs" in caplog.text  # 完整例外寫進 log
    assert leftovers(backup_dir) == [FIRST_NAME]  # 有效的備份檔保留
    assert runs(seeded_db) == []  # 沒有半筆紀錄


def test_record_failure_while_recording_failed_raises_record_error(
    seeded_db, keys, backup_dir, monkeypatch
):
    monkeypatch.setattr(run_repo, "add", lambda db, run: (_ for _ in ()).throw(OSError("full")))
    with pytest.raises(backup.BackupRecordError) as exc_info:
        backup.run_backup(seeded_db, make_config(seeded_db, backup_dir, keys, recipient=None))
    assert isinstance(exc_info.value.__cause__, OSError)
    assert runs(seeded_db) == []


# --- 保留與清理 ------------------------------------------------------------------------------


def test_success_cleans_expired_files_and_keeps_others(seeded_db, keys, backup_dir):
    backup_dir.mkdir()
    keep_month_first = "ledger-20260801-030000.db.gz.age"
    expired = "ledger-20260802-030000.db.gz.age"
    recent = "ledger-20261014-030000.db.gz.age"
    for file in (keep_month_first, expired, recent, "notes.txt"):
        (backup_dir / file).write_bytes(b"x")
    run = backup.run_backup(seeded_db, make_config(seeded_db, backup_dir, keys))
    assert run.error == backup.LOCAL_ONLY_WARNING
    assert leftovers(backup_dir) == sorted([keep_month_first, recent, "notes.txt", FIRST_NAME])


def test_cleanup_failure_keeps_success_with_short_warning(
    seeded_db, keys, backup_dir, monkeypatch, caplog
):
    def failing_cleanup(*args, **kwargs):
        raise backup.BackupStepError("3 個舊備份檔無法刪除 C:\\secret\\path")

    monkeypatch.setattr(backup, "cleanup", failing_cleanup)
    with caplog.at_level(logging.ERROR):
        run = backup.run_backup(seeded_db, make_config(seeded_db, backup_dir, keys))
    assert run.status == "SUCCESS"
    # 簡短警告，不含原始錯誤或路徑（本機模式另有固定的「僅本機」警告）
    assert run.error == backup.WARNING_SEPARATOR.join(
        [backup.LOCAL_ONLY_WARNING, backup.CLEANUP_WARNING]
    )
    assert "secret" not in run.error
    assert (backup_dir / FIRST_NAME).exists()
    assert "backup cleanup failed" in caplog.text


def test_next_clean_run_has_no_warning(seeded_db, keys, backup_dir, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(backup, "cleanup", lambda *a, **k: (_ for _ in ()).throw(OSError("x")))
        backup.run_backup(seeded_db, make_config(seeded_db, backup_dir, keys))
    second = backup.run_backup(seeded_db, make_config(seeded_db, backup_dir, keys))
    assert [r.error for r in runs(seeded_db)] == [
        backup.WARNING_SEPARATOR.join([backup.LOCAL_ONLY_WARNING, backup.CLEANUP_WARNING]),
        backup.LOCAL_ONLY_WARNING,
    ]
    assert second.error == backup.LOCAL_ONLY_WARNING


# --- 不洩漏 ----------------------------------------------------------------------------------


def test_logs_and_rows_never_contain_recipient_or_identity(
    seeded_db, keys, backup_dir, caplog, tmp_path
):
    secret = keys.identity.read_text(encoding="utf-8")
    private = [line for line in secret.splitlines() if line.startswith("AGE-SECRET-KEY-")][0]
    with caplog.at_level(logging.DEBUG):
        backup.run_backup(seeded_db, make_config(seeded_db, backup_dir, keys))
        with pytest.raises(backup.BackupFailed):
            backup.run_backup(
                seeded_db, make_config(seeded_db, backup_dir, keys, recipient=BAD_RECIPIENT)
            )
        backup.verify_backup(backup_dir / FIRST_NAME, keys.identity, keys.age_bin)
    everything = caplog.text + " ".join(r.error or "" for r in runs(seeded_db))
    for forbidden in (keys.recipient, BAD_RECIPIENT, private, str(keys.identity)):
        assert forbidden not in everything


# --- 還原驗證 --------------------------------------------------------------------------------


def test_verify_with_wrong_identity_fails(seeded_db, keys, backup_dir, tmp_path):
    backup.run_backup(seeded_db, make_config(seeded_db, backup_dir, keys))
    other = make_age_keys(tmp_path, "other-key.txt")
    with pytest.raises(backup.BackupStepError):
        backup.verify_backup(backup_dir / FIRST_NAME, other.identity, keys.age_bin)


def test_verify_corrupted_backup_fails(seeded_db, keys, backup_dir):
    backup.run_backup(seeded_db, make_config(seeded_db, backup_dir, keys))
    target = backup_dir / FIRST_NAME
    data = bytearray(target.read_bytes())
    data[-10] ^= 0xFF
    target.write_bytes(bytes(data))
    with pytest.raises(backup.BackupStepError):
        backup.verify_backup(target, keys.identity, keys.age_bin)


def test_verify_non_sqlite_payload_fails(keys, tmp_path):
    import gzip
    import subprocess

    bogus = tmp_path / "bogus.db.gz.age"
    payload = gzip.compress(b"definitely not a database" * 50)
    subprocess.run(
        [keys.age_bin, "-r", keys.recipient, "-o", str(bogus)],
        input=payload,
        check=True,
        capture_output=True,
    )
    with pytest.raises(backup.BackupStepError):
        backup.verify_backup(bogus, keys.identity, keys.age_bin)


def test_verify_leaves_no_files_behind(seeded_db, keys, backup_dir, tmp_path):
    backup.run_backup(seeded_db, make_config(seeded_db, backup_dir, keys))
    before = sorted(str(p) for p in tmp_path.rglob("*"))
    backup.verify_backup(backup_dir / FIRST_NAME, keys.identity, keys.age_bin)
    assert sorted(str(p) for p in tmp_path.rglob("*")) == before


# --- backup_runs 約束 ------------------------------------------------------------------------


def test_valid_rows_are_accepted(ledger_db):
    add_run(ledger_db, "SUCCESS")
    add_run(ledger_db, "FAILED", error="x")
    add_run(ledger_db, "SUCCESS", error=backup.CLEANUP_WARNING)
    add_run(ledger_db, "SUCCESS", size_bytes=0)
    assert ledger_db.scalar(select(text("count(*)")).select_from(BackupRun)) == 4


@pytest.mark.parametrize(
    "fields",
    [
        {"status": "RUNNING", "error": "x"},
        {"status": "success", "file_name": "a", "size_bytes": 1},
        {"status": "SUCCESS", "file_name": None, "size_bytes": 1},
        {"status": "SUCCESS", "file_name": "a", "size_bytes": None},
        {"status": "SUCCESS", "file_name": "a", "size_bytes": -1},
        {"status": "FAILED", "error": None},
    ],
    ids=["running", "lowercase", "no-file", "no-size", "negative-size", "failed-no-error"],
)
def test_invalid_rows_are_rejected(ledger_db, fields):
    base = {"started_at": "2026-10-15T03:00:00+08:00", "finished_at": "2026-10-15T03:00:00+08:00"}
    ledger_db.add(BackupRun(**base, **fields))
    with pytest.raises(IntegrityError):
        ledger_db.commit()
    ledger_db.rollback()


def test_timestamps_are_required(ledger_db):
    ledger_db.add(BackupRun(started_at="x", status="FAILED", error="x", finished_at=None))
    with pytest.raises(IntegrityError):
        ledger_db.commit()
    ledger_db.rollback()
