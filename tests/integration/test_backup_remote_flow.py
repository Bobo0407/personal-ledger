"""雲端備份整條流程（P3-5B）：本機密文 → 上傳 → listing 驗證 → 保留 → backup_runs → 還原驗證。

真 age ＋ 真 rclone（local backend）。時間凍結在 2026-10-15 12:00（台北）。
"""

import hashlib
import logging
import subprocess
import sys

import pytest
from sqlalchemy import select

from app.models import BackupRun
from app.services import backup, backup_remote
from app.services.backup_remote import RemoteConfig, RemoteError
from tests.backup_helpers import db_path_of, make_age_keys, make_config, make_local_remote
from tests.ledger_helpers import freeze_today

NAME = "ledger-20261015-120000.db.gz.age"


@pytest.fixture(autouse=True)
def _today(monkeypatch):
    freeze_today(monkeypatch)


@pytest.fixture
def keys(tmp_path):
    return make_age_keys(tmp_path)


@pytest.fixture
def remote(tmp_path):
    return make_local_remote(tmp_path)


@pytest.fixture
def backup_dir(tmp_path):
    return tmp_path / "backups"


def runs(db):
    db.expire_all()
    return list(db.scalars(select(BackupRun).order_by(BackupRun.id)))


def names(directory):
    return sorted(p.name for p in directory.iterdir())


def full_config(db, backup_dir, keys, remote, **overrides):
    return make_config(db, backup_dir, keys, remote=remote.config, **overrides)


# --- 成功 ------------------------------------------------------------------------------------


def test_success_uploads_and_verifies_and_leaves_no_warning(seeded_db, keys, backup_dir, remote):
    run = backup.run_backup(seeded_db, full_config(seeded_db, backup_dir, keys, remote))
    assert run.status == "SUCCESS" and run.error is None
    assert run.file_name == NAME
    assert (backup_dir / NAME).exists()
    assert (remote.directory / NAME).read_bytes() == (backup_dir / NAME).read_bytes()
    assert run.size_bytes == (remote.directory / NAME).stat().st_size
    assert [(r.status, r.error) for r in runs(seeded_db)] == [("SUCCESS", None)]


def test_only_age_ciphertext_reaches_the_remote(seeded_db, keys, backup_dir, remote):
    backup.run_backup(seeded_db, full_config(seeded_db, backup_dir, keys, remote))
    assert names(remote.directory) == [NAME]
    head = (remote.directory / NAME).read_bytes()[:40]
    assert head.startswith(b"age-encryption.org/v1")
    assert b"SQLite format 3" not in (remote.directory / NAME).read_bytes()
    assert not (remote.directory / NAME).read_bytes().startswith(b"\x1f\x8b")  # 不是 gzip


def test_two_runs_upload_two_distinct_files(seeded_db, keys, backup_dir, remote, monkeypatch):
    config = full_config(seeded_db, backup_dir, keys, remote)
    backup.run_backup(seeded_db, config)
    backup.run_backup(seeded_db, config)  # 同一秒 → 本機與遠端都是 -2
    assert names(remote.directory) == sorted([NAME, NAME.replace(".db", "-2.db")])
    assert [r.error for r in runs(seeded_db)] == [None, None]


def test_the_remote_copy_decrypts_and_matches_the_source(seeded_db, keys, backup_dir, remote):
    backup.run_backup(seeded_db, full_config(seeded_db, backup_dir, keys, remote))
    report = backup.verify_backup(remote.directory / NAME, keys.identity, keys.age_bin)
    assert report.integrity_ok and report.alembic_version == "0005"
    assert report.row_counts["accounts"] > 0


# --- 設定：沒有 remote 不得默默降級 ----------------------------------------------------------


def test_missing_remote_without_local_only_is_a_configuration_failure(seeded_db, keys, backup_dir):
    config = make_config(seeded_db, backup_dir, keys, remote=None, local_only=False)
    with pytest.raises(backup.BackupFailed):
        backup.run_backup(seeded_db, config)
    (row,) = runs(seeded_db)
    assert row.status == "FAILED" and row.file_name is None and row.size_bytes is None
    assert "RCLONE_REMOTE" in row.error and "--local-only" in row.error
    assert not backup_dir.exists()  # 設定錯誤在產生任何檔案之前就擋下


def test_local_only_is_an_explicit_success_with_a_fixed_warning(seeded_db, keys, backup_dir):
    config = make_config(seeded_db, backup_dir, keys, remote=None, local_only=True)
    run = backup.run_backup(seeded_db, config)
    assert run.status == "SUCCESS" and run.error == backup.LOCAL_ONLY_WARNING
    assert (backup_dir / NAME).exists()


def test_local_only_never_touches_the_remote_even_when_it_is_configured(
    seeded_db, keys, backup_dir, remote, monkeypatch
):
    monkeypatch.setattr(backup_remote, "upload", lambda *a, **k: pytest.fail("不應上傳"))
    monkeypatch.setattr(backup_remote, "list_files", lambda *a, **k: pytest.fail("不應連線"))
    config = full_config(seeded_db, backup_dir, keys, remote, local_only=True)
    run = backup.run_backup(seeded_db, config)
    assert run.error == backup.LOCAL_ONLY_WARNING
    assert names(remote.directory) == []


@pytest.mark.parametrize(
    "remote_name, path", [("a:b", "x"), ("", "x"), ("gdrive", "../up"), ("gdrive", "")]
)
def test_invalid_remote_format_fails_before_creating_any_file(
    seeded_db, keys, backup_dir, remote_name, path
):
    bad = RemoteConfig(remote_name, path)
    config = make_config(seeded_db, backup_dir, keys, remote=bad)
    with pytest.raises(backup.BackupFailed):
        backup.run_backup(seeded_db, config)
    (row,) = runs(seeded_db)
    assert row.status == "FAILED" and row.file_name is None
    assert not backup_dir.exists()


# --- 上傳失敗：FAILED、本機檔保留、不清理 ----------------------------------------------------


def assert_upload_failed(db, backup_dir, remote_dir=None):
    (row,) = runs(db)
    assert row.status == "FAILED"
    assert row.file_name == NAME and row.size_bytes == (backup_dir / NAME).stat().st_size
    assert row.error == backup.UPLOAD_FAILED
    assert (backup_dir / NAME).exists()  # 本機救命檔不因雲端失敗而刪除
    return row


def test_unknown_remote_keeps_the_local_file_and_records_failed(
    seeded_db, keys, backup_dir, remote
):
    bad = RemoteConfig(
        "nosuch", remote.config.path, remote.config.rclone_bin, remote.config.config_file
    )
    config = make_config(seeded_db, backup_dir, keys, remote=bad)
    with pytest.raises(backup.BackupFailed):
        backup.run_backup(seeded_db, config)
    assert_upload_failed(seeded_db, backup_dir)
    assert names(remote.directory) == []


def test_missing_rclone_binary_keeps_the_local_file_and_records_failed(
    seeded_db, keys, backup_dir, remote
):
    bad = RemoteConfig("fake", remote.config.path, "no-such-rclone-xyz", remote.config.config_file)
    with pytest.raises(backup.BackupFailed):
        backup.run_backup(seeded_db, make_config(seeded_db, backup_dir, keys, remote=bad))
    assert_upload_failed(seeded_db, backup_dir)


def test_upload_failure_skips_all_cleanup(seeded_db, keys, backup_dir, remote, monkeypatch):
    backup_dir.mkdir()
    old_local = "ledger-20250101-030000.db.gz.age"
    (backup_dir / old_local).write_bytes(b"x")
    (remote.directory / old_local).write_bytes(b"x")
    monkeypatch.setattr(
        backup_remote, "upload", lambda *a, **k: (_ for _ in ()).throw(RemoteError("x"))
    )
    with pytest.raises(backup.BackupFailed):
        backup.run_backup(seeded_db, full_config(seeded_db, backup_dir, keys, remote))
    assert old_local in names(backup_dir)  # 降級時不動舊備份
    assert names(remote.directory) == [old_local]


def test_remote_listing_without_the_file_is_an_upload_failure(
    seeded_db, keys, backup_dir, remote, monkeypatch
):
    monkeypatch.setattr(backup_remote, "list_files", lambda cfg: [])
    with pytest.raises(backup.BackupFailed):
        backup.run_backup(seeded_db, full_config(seeded_db, backup_dir, keys, remote))
    assert_upload_failed(seeded_db, backup_dir)


def test_remote_size_mismatch_is_an_upload_failure(
    seeded_db, keys, backup_dir, remote, monkeypatch
):
    monkeypatch.setattr(
        backup_remote, "list_files", lambda cfg: [backup_remote.RemoteFile(NAME, 1)]
    )
    with pytest.raises(backup.BackupFailed):
        backup.run_backup(seeded_db, full_config(seeded_db, backup_dir, keys, remote))
    assert_upload_failed(seeded_db, backup_dir)


def test_same_name_different_content_on_the_remote_fails_and_never_overwrites(
    seeded_db, keys, backup_dir, remote
):
    (remote.directory / NAME).write_bytes(b"someone else's file")
    with pytest.raises(backup.BackupFailed):
        backup.run_backup(seeded_db, full_config(seeded_db, backup_dir, keys, remote))
    assert (remote.directory / NAME).read_bytes() == b"someone else's file"
    assert_upload_failed(seeded_db, backup_dir)


def test_next_full_success_clears_the_failure(seeded_db, keys, backup_dir, remote, monkeypatch):
    bad = RemoteConfig(
        "nosuch", remote.config.path, remote.config.rclone_bin, remote.config.config_file
    )
    with pytest.raises(backup.BackupFailed):
        backup.run_backup(seeded_db, make_config(seeded_db, backup_dir, keys, remote=bad))
    run = backup.run_backup(seeded_db, full_config(seeded_db, backup_dir, keys, remote))
    assert [(r.status, r.error) for r in runs(seeded_db)] == [
        ("FAILED", backup.UPLOAD_FAILED),
        ("SUCCESS", None),
    ]
    assert run.file_name == NAME.replace(".db", "-2.db")  # 第一次的本機檔還在，所以是 -2
    assert backup.backup_status(seeded_db).level in {"ok", "warn"}


# --- 遠端保留 --------------------------------------------------------------------------------

DAILY_OLD = "ledger-20260802-030000.db.gz.age"  # 8 月的第二份、超過 30 天 → 刪
MONTH_FIRST_AUG = "ledger-20260801-030000.db.gz.age"  # 8 月第一份 → 留
SEP_FIRST = "ledger-20260901-030000.db.gz.age"
SEP_SECOND = "ledger-20260902-030000.db.gz.age"  # 超過 30 天、非當月第一份 → 刪
NOV_2025 = "ledger-20251101-030000.db.gz.age"  # 11 個月前的每月第一份 → 留
OCT_2025 = "ledger-20251001-030000.db.gz.age"  # 12 個月前 → 超出 → 刪
RECENT = "ledger-20261014-030000.db.gz.age"
UNRELATED = ["notes.txt", "ledger-latest.db", "ledger-20260101-030000.db.gz", "README.md"]


def populate(directory):
    for name in (
        DAILY_OLD, MONTH_FIRST_AUG, SEP_FIRST, SEP_SECOND, NOV_2025, OCT_2025, RECENT, *UNRELATED
    ):  # fmt: skip
        (directory / name).write_bytes(b"x")


def test_remote_retention_keeps_30_days_and_12_monthly_and_deletes_the_rest(
    seeded_db, keys, backup_dir, remote
):
    populate(remote.directory)
    run = backup.run_backup(seeded_db, full_config(seeded_db, backup_dir, keys, remote))
    assert run.status == "SUCCESS" and run.error is None
    assert names(remote.directory) == sorted(
        [MONTH_FIRST_AUG, SEP_FIRST, NOV_2025, RECENT, NAME, *UNRELATED]
    )


def test_unrelated_remote_files_and_subfolders_are_never_deleted(
    seeded_db, keys, backup_dir, remote
):
    populate(remote.directory)
    (remote.directory / "archive").mkdir()
    (remote.directory / "archive" / OCT_2025).write_bytes(b"nested old backup name")
    backup.run_backup(seeded_db, full_config(seeded_db, backup_dir, keys, remote))
    for name in UNRELATED:
        assert (remote.directory / name).read_bytes() == b"x"
    assert (remote.directory / "archive" / OCT_2025).exists()  # 不遞迴、不碰子資料夾


def test_the_file_just_uploaded_is_never_deleted(seeded_db, keys, backup_dir, remote, monkeypatch):
    monkeypatch.setattr(backup, "select_deletions", lambda names, now: sorted(names))
    run = backup.run_backup(seeded_db, full_config(seeded_db, backup_dir, keys, remote))
    assert run.error is None and NAME in names(remote.directory)
    assert NAME in names(backup_dir)


def test_local_retention_still_applies_in_remote_mode(seeded_db, keys, backup_dir, remote):
    backup_dir.mkdir()
    populate(backup_dir)
    backup.run_backup(seeded_db, full_config(seeded_db, backup_dir, keys, remote))
    assert DAILY_OLD not in names(backup_dir) and SEP_SECOND not in names(backup_dir)
    assert MONTH_FIRST_AUG in names(backup_dir) and "notes.txt" in names(backup_dir)


# --- 清理失敗：仍然 SUCCESS，只加固定警告 ----------------------------------------------------


def test_remote_cleanup_failure_is_success_with_a_fixed_warning(
    seeded_db, keys, backup_dir, remote, monkeypatch, caplog
):
    populate(remote.directory)

    def refuse(*args, **kwargs):
        raise RemoteError("rclone 刪除檔案失敗（exit 1）")

    monkeypatch.setattr(backup_remote, "delete", refuse)
    with caplog.at_level(logging.ERROR):
        run = backup.run_backup(seeded_db, full_config(seeded_db, backup_dir, keys, remote))
    assert run.status == "SUCCESS" and run.error == backup.REMOTE_CLEANUP_WARNING
    assert NAME in names(remote.directory) and (backup_dir / NAME).exists()
    assert "backup remote cleanup failed" in caplog.text
    assert backup.backup_status(seeded_db).level == "warn"


def test_local_and_remote_cleanup_failures_are_reported_separately_and_together(
    seeded_db, keys, backup_dir, remote, monkeypatch
):
    populate(remote.directory)
    monkeypatch.setattr(backup, "cleanup", lambda *a, **k: (_ for _ in ()).throw(OSError("x")))
    run = backup.run_backup(seeded_db, full_config(seeded_db, backup_dir, keys, remote))
    assert run.error == backup.CLEANUP_WARNING  # 只有本機失敗

    monkeypatch.setattr(
        backup_remote, "delete", lambda *a, **k: (_ for _ in ()).throw(RemoteError("x"))
    )
    populate(remote.directory)
    both = backup.run_backup(seeded_db, full_config(seeded_db, backup_dir, keys, remote))
    assert both.error == backup.WARNING_SEPARATOR.join(
        [backup.CLEANUP_WARNING, backup.REMOTE_CLEANUP_WARNING]
    )


def test_next_clean_run_recovers_to_ok(seeded_db, keys, backup_dir, remote, monkeypatch):
    populate(remote.directory)
    with monkeypatch.context() as patch:
        patch.setattr(
            backup_remote, "delete", lambda *a, **k: (_ for _ in ()).throw(RemoteError("x"))
        )
        backup.run_backup(seeded_db, full_config(seeded_db, backup_dir, keys, remote))
    second = backup.run_backup(seeded_db, full_config(seeded_db, backup_dir, keys, remote))
    assert second.error is None
    assert backup.backup_status(seeded_db).level == "ok"


# --- rclone 原始輸出與 token 不進 DB／log ----------------------------------------------------


def test_raw_rclone_stderr_and_tokens_never_reach_the_database_or_logs(
    seeded_db, keys, backup_dir, tmp_path, caplog, monkeypatch
):
    real_run = subprocess.run
    fake_bin = sys.executable

    def dispatcher(command, **kwargs):
        if command[0] == fake_bin or command[0].endswith("python3"):
            err = (
                b'ERROR: {"access_token":"ya29.TOPSECRET"} Authorization: Bearer ya29.BEARERSECRET'
            )
            return subprocess.CompletedProcess(command, 1, b"", err)
        return real_run(command, **kwargs)

    monkeypatch.setattr(backup_remote.subprocess, "run", dispatcher)
    config = make_config(
        seeded_db,
        backup_dir,
        keys,
        remote=RemoteConfig("gdrive", "x", fake_bin, tmp_path / "rc.conf"),
    )
    with caplog.at_level(logging.DEBUG), pytest.raises(backup.BackupFailed):
        backup.run_backup(seeded_db, config)
    (row,) = runs(seeded_db)
    assert row.error == backup.UPLOAD_FAILED
    everything = caplog.text + (row.error or "") + (row.file_name or "")
    for secret in ("TOPSECRET", "BEARERSECRET", "rc.conf", keys.recipient):
        assert secret not in everything


# --- 還原驗證：從遠端下載 → 解密 → SQLite ----------------------------------------------------


def seed_remote(seeded_db, keys, backup_dir, remote):
    backup.run_backup(seeded_db, full_config(seeded_db, backup_dir, keys, remote))


def test_remote_restore_round_trip_latest(seeded_db, keys, backup_dir, remote):
    seed_remote(seeded_db, keys, backup_dir, remote)
    name, report = backup.verify_remote_backup(remote.config, "latest", keys.identity, keys.age_bin)
    assert name == NAME
    assert report.integrity_ok and report.alembic_version == "0005"
    source = backup.verify_backup(backup_dir / NAME, keys.identity, keys.age_bin)
    assert report.row_counts == source.row_counts


def test_latest_only_considers_valid_backup_names(seeded_db, keys, backup_dir, remote):
    seed_remote(seeded_db, keys, backup_dir, remote)
    for decoy in ("zzz-notes.txt", "ledger-99999999-999999.db.gz.age", "ledger-latest.db.gz.age"):
        (remote.directory / decoy).write_bytes(b"decoy")
    name, _ = backup.verify_remote_backup(remote.config, "latest", keys.identity, keys.age_bin)
    assert name == NAME


def test_latest_picks_the_newest_valid_name(seeded_db, keys, backup_dir, remote):
    seed_remote(seeded_db, keys, backup_dir, remote)
    older = "ledger-20260101-030000.db.gz.age"
    (remote.directory / older).write_bytes((remote.directory / NAME).read_bytes())
    assert backup.latest_backup_name([older, NAME, "x.txt"]) == NAME
    assert backup.latest_backup_name(["x.txt"]) is None


def test_remote_restore_of_a_named_file(seeded_db, keys, backup_dir, remote):
    seed_remote(seeded_db, keys, backup_dir, remote)
    name, report = backup.verify_remote_backup(remote.config, NAME, keys.identity, keys.age_bin)
    assert name == NAME and report.integrity_ok


@pytest.mark.parametrize("which", ["notes.txt", "../x", "ledger-20250101-030000.db.gz.age"])
def test_remote_restore_rejects_non_backup_or_missing_names(
    seeded_db, keys, backup_dir, remote, which
):
    seed_remote(seeded_db, keys, backup_dir, remote)
    (remote.directory / "notes.txt").write_bytes(b"x")
    with pytest.raises(backup.BackupStepError):
        backup.verify_remote_backup(remote.config, which, keys.identity, keys.age_bin)


def test_remote_restore_with_an_empty_remote_fails(keys, remote):
    with pytest.raises(backup.BackupStepError, match="沒有任何合法"):
        backup.verify_remote_backup(remote.config, "latest", keys.identity, keys.age_bin)


def test_remote_restore_with_the_wrong_identity_fails(
    seeded_db, keys, backup_dir, remote, tmp_path
):
    seed_remote(seeded_db, keys, backup_dir, remote)
    other = make_age_keys(tmp_path, "other.txt")
    with pytest.raises(backup.BackupStepError):
        backup.verify_remote_backup(remote.config, "latest", other.identity, keys.age_bin)


def test_remote_restore_of_a_corrupted_backup_fails(seeded_db, keys, backup_dir, remote):
    seed_remote(seeded_db, keys, backup_dir, remote)
    target = remote.directory / NAME
    data = bytearray(target.read_bytes())
    data[len(data) // 2] ^= 0xFF
    target.write_bytes(bytes(data))
    with pytest.raises(backup.BackupStepError):
        backup.verify_remote_backup(remote.config, "latest", keys.identity, keys.age_bin)


def test_remote_restore_cleans_its_temp_download_and_never_touches_the_source_db(
    seeded_db, keys, backup_dir, remote, monkeypatch
):
    seed_remote(seeded_db, keys, backup_dir, remote)
    db_file = db_path_of(seeded_db)
    before = hashlib.sha256(db_file.read_bytes()).hexdigest()
    seen = []
    real = backup_remote.download

    def spy(config, name, dest):
        seen.append(dest)
        return real(config, name, dest)

    monkeypatch.setattr(backup_remote, "download", spy)
    backup.verify_remote_backup(remote.config, "latest", keys.identity, keys.age_bin)
    assert seen and not seen[0].exists()  # 暫存下載目錄在結束時已刪除
    assert hashlib.sha256(db_file.read_bytes()).hexdigest() == before  # 正式 DB 沒被碰


def test_remote_restore_failure_also_cleans_the_temp_download(
    seeded_db, keys, backup_dir, remote, tmp_path, monkeypatch
):
    seed_remote(seeded_db, keys, backup_dir, remote)
    seen = []
    real = backup_remote.download

    def spy(config, name, dest):
        seen.append(dest)
        return real(config, name, dest)

    monkeypatch.setattr(backup_remote, "download", spy)
    other = make_age_keys(tmp_path, "other2.txt")
    with pytest.raises(backup.BackupStepError):
        backup.verify_remote_backup(remote.config, "latest", other.identity, keys.age_bin)
    assert seen and not seen[0].exists()
