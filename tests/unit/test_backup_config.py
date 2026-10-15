"""BackupSettings：備份程序的設定（P3-5A）。不需要 SECRET_KEY。"""

from pathlib import Path

import pytest

from app.config import BackupSettings

KEYS = [
    "DATABASE_URL",
    "BACKUP_DIR",
    "BACKUP_AGE_RECIPIENT",
    "BACKUP_AGE_BIN",
    "SECRET_KEY",
    "RCLONE_REMOTE",
    "RCLONE_PATH",
    "RCLONE_BIN",
    "RCLONE_CONFIG",
]


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


# --- P3-5B：rclone 設定 ----------------------------------------------------------------------

ROOT = Path(__file__).resolve().parents[2]


def test_rclone_defaults_mean_remote_not_configured():
    s = BackupSettings(_env_file=None)
    assert s.rclone_remote is None and s.rclone_config is None
    assert s.rclone_path == "ledger-backups" and s.rclone_bin == "rclone"
    from app.services import backup

    assert backup.config_from_settings(s).remote is None


def test_rclone_settings_build_a_remote_config(monkeypatch):
    monkeypatch.setenv("RCLONE_REMOTE", "gdrive")
    monkeypatch.setenv("RCLONE_PATH", "my-backups")
    monkeypatch.setenv("RCLONE_BIN", "/opt/rclone")
    monkeypatch.setenv("RCLONE_CONFIG", "/etc/ledger/rclone.conf")
    from app.services import backup

    remote = backup.config_from_settings(BackupSettings(_env_file=None)).remote
    assert (remote.remote, remote.path, remote.rclone_bin) == (
        "gdrive",
        "my-backups",
        "/opt/rclone",
    )
    assert remote.config_file == Path("/etc/ledger/rclone.conf")
    assert remote.spec == "gdrive:my-backups"


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_rclone_values_count_as_unset(monkeypatch, blank):
    monkeypatch.setenv("RCLONE_REMOTE", blank)
    monkeypatch.setenv("RCLONE_CONFIG", blank)
    from app.services import backup

    config = backup.config_from_settings(BackupSettings(_env_file=None))
    assert config.remote is None


def test_blank_rclone_config_alone_does_not_become_the_current_directory(monkeypatch):
    monkeypatch.setenv("RCLONE_REMOTE", "gdrive")
    monkeypatch.setenv("RCLONE_CONFIG", "")
    from app.services import backup

    assert backup.config_from_settings(BackupSettings(_env_file=None)).remote.config_file is None


def test_local_only_is_never_the_default():
    from app.services import backup

    assert backup.config_from_settings(BackupSettings(_env_file=None)).local_only is False


def test_env_example_documents_rclone_without_any_secret():
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "RCLONE_REMOTE=\n" in text  # 預設空白：不預填任何 remote
    for key in ("RCLONE_PATH", "RCLONE_BIN", "RCLONE_CONFIG"):
        assert key in text
    for forbidden in ("access_token", "refresh_token", "client_secret", "AGE-SECRET-KEY"):
        assert forbidden not in text


def test_gitignore_keeps_rclone_config_and_age_keys_out_of_git():
    lines = {
        line.strip() for line in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    }
    assert {"rclone.conf", "*.age"} <= lines


def test_backup_code_never_uses_a_shell():
    for name in ("backup.py", "backup_remote.py"):
        source = (ROOT / "app" / "services" / name).read_text(encoding="utf-8")
        assert "shell=True" not in source and "os.system" not in source
    for name in ("backup.py", "restore_check.py"):
        assert "shell=True" not in (ROOT / "scripts" / name).read_text(encoding="utf-8")


def test_remote_adapter_does_not_depend_on_models_or_repositories():
    source = (ROOT / "app" / "services" / "backup_remote.py").read_text(encoding="utf-8")
    assert "app.models" not in source and "app.repositories" not in source
    assert "google" not in source.lower().replace("google drive", "")
    for banned in ("sync", "purge"):
        assert f'"{banned}"' not in source
