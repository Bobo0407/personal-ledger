"""BackupSettings：備份程序的設定（P3-5A）。不需要 SECRET_KEY。"""

from pathlib import Path

import pytest

from app.config import BackupSettings

KEYS = ["DATABASE_URL", "BACKUP_DIR", "BACKUP_AGE_RECIPIENT", "BACKUP_AGE_BIN", "SECRET_KEY"]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in KEYS:
        monkeypatch.delenv(key, raising=False)


def test_defaults_and_no_secret_key_needed():
    s = BackupSettings(_env_file=None)
    assert s.backup_dir == Path("./data/backups")
    assert s.backup_age_recipient is None
    assert s.backup_age_bin == "age"
    assert s.database_url == "sqlite:///./data/ledger.db"


def test_reads_environment(monkeypatch):
    monkeypatch.setenv("BACKUP_DIR", "/somewhere/backups")
    monkeypatch.setenv("BACKUP_AGE_RECIPIENT", "age1abc")
    monkeypatch.setenv("BACKUP_AGE_BIN", "/opt/age")
    s = BackupSettings(_env_file=None)
    assert (s.backup_dir, s.backup_age_recipient, s.backup_age_bin) == (
        Path("/somewhere/backups"),
        "age1abc",
        "/opt/age",
    )


def test_blank_recipient_in_env_becomes_missing(monkeypatch):
    from app.services import backup

    monkeypatch.setenv("BACKUP_AGE_RECIPIENT", "  ")
    config = backup.config_from_settings(BackupSettings(_env_file=None))
    assert config.recipient is None
