"""部署檔的靜態守門測試（P3-6）。只檢查 deploy/ 與 docs/deploy.md 的內容，不連線、不執行 systemd。

守住的約束：uvicorn 只 bind localhost 且單 worker、兩個 service 都 UMask=0077 且不是 root、
正式備份不含本機專用旗標、timer 時間、範本沒有真 secret、Caddyfile 只有 placeholder 網域、
runbook 沒有未實測的 rclone authorize 指令。
真正的 systemd／Caddy／權限行為要在 Linux VM 驗收（docs/deploy.md §10）。
"""

import re
from pathlib import Path

import pytest

from app.config import BackupSettings, Settings

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy"
RUNBOOK = ROOT / "docs" / "deploy.md"
LOCAL_ONLY_FLAG = "--local-only"
DEPLOY_FILES = [
    "ledger.service",
    "ledger-backup.service",
    "ledger-backup.timer",
    "Caddyfile",
    "ledger.env.example",
]

SECRET_PATTERNS = {
    "age private key": r"AGE-SECRET-KEY-[0-9A-Z]+",
    "Google client secret": r"GOCSPX-[A-Za-z0-9_-]+",
    "Google access token": r"ya29\.[A-Za-z0-9_-]+",
    "Google client id": r"[0-9]+-[a-z0-9]+\.apps\.googleusercontent\.com",
    "OAuth json token": r'"(access_token|refresh_token)"\s*:\s*"[^"]+"',
    "rclone client_secret value": r"client_secret\s*=\s*[A-Za-z0-9_-]{10,}",
    "rclone token value": r"^token\s*=\s*\{",
}


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def unit(name: str) -> dict[str, dict[str, list[str]]]:
    """極簡的 systemd unit 解析：{section: {key: [values]}}，忽略註解。"""
    result: dict[str, dict[str, list[str]]] = {}
    section = ""
    for raw in read(DEPLOY / name).splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            result.setdefault(section, {})
            continue
        key, _, value = line.partition("=")
        result[section].setdefault(key.strip(), []).append(value.strip())
    return result


def one(parsed, section: str, key: str) -> str:
    values = parsed[section][key]
    assert len(values) == 1, (section, key, values)
    return values[0]


def env_example() -> dict[str, str]:
    values = {}
    for raw in read(DEPLOY / "ledger.env.example").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            key, _, value = line.partition("=")
            values[key] = value
    return values


# --- 檔案存在 --------------------------------------------------------------------------------


@pytest.mark.parametrize("name", DEPLOY_FILES)
def test_deploy_file_exists(name):
    assert (DEPLOY / name).is_file()


def test_runbook_and_adr_exist():
    assert RUNBOOK.is_file()
    assert (ROOT / "docs" / "adr" / "ADR-010-deployment.md").is_file()


# --- ledger.service --------------------------------------------------------------------------


def test_web_service_binds_localhost_only_with_a_single_worker():
    exec_start = one(unit("ledger.service"), "Service", "ExecStart")
    assert exec_start.startswith("/srv/ledger/venv/bin/uvicorn app.main:create_app --factory")
    assert "--host 127.0.0.1" in exec_start and "--port 8000" in exec_start
    for forbidden in ("0.0.0.0", "--workers", "--reload", "gunicorn", "--host ::"):
        assert forbidden not in exec_start


def test_web_service_runs_as_ledger_with_umask_and_env_file():
    service = unit("ledger.service")["Service"]
    assert service["User"] == ["ledger"] and service["Group"] == ["ledger"]
    assert service["UMask"] == ["0077"]
    assert service["EnvironmentFile"] == ["/srv/ledger/config/ledger.env"]
    assert service["WorkingDirectory"] == ["/srv/ledger/app"]
    assert service["Restart"] == ["on-failure"]


def test_web_service_does_not_run_migrations_on_start():
    service = unit("ledger.service")["Service"]
    assert not any(key.startswith("ExecStartPre") for key in service)
    assert "alembic" not in read(DEPLOY / "ledger.service").replace("migration", "")


# --- ledger-backup.service / timer -----------------------------------------------------------


def test_backup_service_calls_the_existing_script_as_ledger_with_umask():
    service = unit("ledger-backup.service")["Service"]
    assert service["Type"] == ["oneshot"]
    assert service["ExecStart"] == ["/srv/ledger/venv/bin/python scripts/backup.py"]
    assert service["User"] == ["ledger"] and service["Group"] == ["ledger"]
    assert service["UMask"] == ["0077"]
    assert service["EnvironmentFile"] == ["/srv/ledger/config/ledger.env"]
    assert service["WorkingDirectory"] == ["/srv/ledger/app"]
    assert "Restart" not in service  # 失敗就等明天，不重試迴圈
    assert (ROOT / "scripts" / "backup.py").is_file()


@pytest.mark.parametrize("name", DEPLOY_FILES)
def test_no_deploy_file_ever_uses_the_local_only_flag(name):
    assert LOCAL_ONLY_FLAG not in read(DEPLOY / name)


def test_backup_timer_runs_daily_at_0300_taipei_and_catches_up():
    timer = unit("ledger-backup.timer")
    assert timer["Timer"]["OnCalendar"] == ["*-*-* 03:00:00 Asia/Taipei"]
    assert timer["Timer"]["Persistent"] == ["true"]
    assert timer["Install"]["WantedBy"] == ["timers.target"]
    assert "Unit" not in timer["Timer"]  # 預設就是同名的 ledger-backup.service


# --- Caddyfile -------------------------------------------------------------------------------


def test_caddyfile_proxies_to_localhost_and_uses_a_placeholder_domain():
    text = read(DEPLOY / "Caddyfile")
    code = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    assert code[0] == "YOUR_DOMAIN_HERE {" and code[-1] == "}"
    assert "reverse_proxy 127.0.0.1:8000" in code
    assert "0.0.0.0" not in text
    # 除了 placeholder 之外，不得出現任何像網域的字串（避免把真網域 commit 進來）
    assert not re.search(r"\b[a-z0-9-]+(\.[a-z0-9-]+)+\.(com|net|org|io|tw|dev|app|xyz)\b", text)


def test_caddyfile_does_not_expose_uvicorn_or_extra_listeners():
    text = read(DEPLOY / "Caddyfile")
    assert ":8000 {" not in text and "bind " not in text and "http_port" not in text


# --- ledger.env.example ----------------------------------------------------------------------


def test_env_example_is_production_shaped():
    env = env_example()
    assert env["APP_ENV"] == "prod" and env["SESSION_COOKIE_SECURE"] == "true"
    assert env["DATABASE_URL"] == "sqlite:////srv/ledger/data/ledger.db"
    assert env["LOG_DIR"] == "/srv/ledger/logs"
    assert env["BACKUP_DIR"] == "/srv/ledger/data/backups"
    assert env["RCLONE_CONFIG"] == "/srv/ledger/config/rclone.conf"
    assert env["BACKUP_AGE_BIN"].startswith("/") and env["RCLONE_BIN"].startswith("/")


@pytest.mark.parametrize("key", ["SECRET_KEY", "BACKUP_AGE_RECIPIENT", "RCLONE_REMOTE"])
def test_env_example_leaves_instance_specific_values_empty(key):
    assert env_example()[key] == ""


def test_env_example_has_only_known_settings_and_is_systemd_compatible():
    known = {name.upper() for name in Settings.model_fields} | {
        name.upper() for name in BackupSettings.model_fields
    }
    for key in env_example():
        assert key in known, key
    for raw in read(DEPLOY / "ledger.env.example").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            assert not line.startswith("export ") and '"' not in line and "'" not in line


def test_env_example_matches_the_paths_used_by_the_units():
    text = read(DEPLOY / "ledger.env.example")
    assert "/srv/ledger/config/ledger.env" in text
    for name in ("ledger.service", "ledger-backup.service"):
        assert one(unit(name), "Service", "EnvironmentFile") == "/srv/ledger/config/ledger.env"


# --- 沒有真 secret ---------------------------------------------------------------------------


@pytest.mark.parametrize("label", sorted(SECRET_PATTERNS))
def test_no_secret_shaped_text_in_deploy_files_or_runbook(label):
    pattern = re.compile(SECRET_PATTERNS[label], re.MULTILINE)
    for path in [*(DEPLOY / n for n in DEPLOY_FILES), RUNBOOK]:
        assert not pattern.search(read(path)), (label, path.name)


def test_gitignore_blocks_real_production_config():
    lines = {ln.strip() for ln in read(ROOT / ".gitignore").splitlines()}
    assert {"deploy/*.env", "rclone.conf", "*.age"} <= lines


# --- runbook ---------------------------------------------------------------------------------


def test_runbook_does_not_contain_untested_rclone_authorize_commands():
    assert "rclone authorize" not in read(RUNBOOK)


def test_runbook_only_mentions_the_local_only_flag_as_a_prohibition():
    for line in read(RUNBOOK).splitlines():
        assert LOCAL_ONLY_FLAG not in line, line  # runbook 用文字描述，不貼出可複製的旗標


def test_runbook_records_the_rclone_version_and_oauth_rules():
    text = read(RUNBOOK)
    assert "v1.75.1" in text
    assert "drive.file" in text and "共用" in text and "不得上 production" in text
    assert "rclone config" in text


def test_runbook_requires_umask_and_stat_permission_checks():
    text = read(RUNBOOK)
    assert "umask 077" in text and "UMask=0077" in text
    assert "stat -c" in text and "-perm /077" in text
    for name in ("ledger.db", "ledger.env", "rclone.conf", ".age"):
        assert name in text


def test_runbook_migration_flow_snapshots_only_after_the_app_is_stopped():
    text = read(RUNBOOK)
    assert "只在 app 已停止後" in text
    stop = text.index("sudo systemctl stop ledger")
    snapshot = text.index("pre-deploy/ledger-before-")
    migrate = text.index("lrun alembic upgrade head", snapshot)
    assert stop < snapshot < migrate
    assert "不自動 downgrade" in text or "不自動 alembic downgrade" in text
    assert "fix-forward" in text


def test_runbook_keeps_the_scope_boundaries():
    text = read(RUNBOOK)
    assert "opening balance" in text and "P3-7" in text
    assert "CLOSED" in text  # 明確寫出驗收前不得 CLOSED
    assert "03:30" in text
