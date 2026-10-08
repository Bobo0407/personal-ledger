import pytest
from pydantic import ValidationError

from app.config import DatabaseSettings, Settings

STRONG_SECRET = "x" * 48
ENV_KEYS = [
    "APP_ENV",
    "DATABASE_URL",
    "SECRET_KEY",
    "SESSION_COOKIE_SECURE",
    "LOG_DIR",
    "LOG_LEVEL",
]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    # 避免開發者本機的環境變數影響測試結果
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def load(**overrides) -> Settings:
    # _env_file=None：不讀專案根目錄的 .env
    return Settings(_env_file=None, **overrides)


def test_defaults_follow_spec():
    s = load(secret_key="dev-secret")
    assert s.app_env == "dev"
    assert s.database_url == "sqlite:///./data/ledger.db"
    assert s.session_cookie_secure is False
    assert s.session_ttl_days == 30
    assert s.login_max_failures == 5
    assert s.login_lockout_minutes == 15


def test_reads_environment_variables(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("SECRET_KEY", "from-env")
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "true")
    s = load()
    assert s.app_env == "test"
    assert s.secret_key == "from-env"
    assert s.session_cookie_secure is True


def test_secret_key_is_required():
    with pytest.raises(ValidationError):
        load()


def test_unknown_app_env_is_rejected():
    with pytest.raises(ValidationError):
        load(secret_key="dev-secret", app_env="staging")


def test_prod_rejects_placeholder_secret():
    with pytest.raises(ValidationError, match="SECRET_KEY"):
        load(app_env="prod", secret_key="change-me", session_cookie_secure=True)


def test_prod_rejects_short_secret():
    with pytest.raises(ValidationError, match="SECRET_KEY"):
        load(app_env="prod", secret_key="too-short", session_cookie_secure=True)


def test_prod_requires_secure_cookie():
    with pytest.raises(ValidationError, match="SESSION_COOKIE_SECURE"):
        load(app_env="prod", secret_key=STRONG_SECRET, session_cookie_secure=False)


def test_prod_accepts_safe_settings():
    s = load(app_env="prod", secret_key=STRONG_SECRET, session_cookie_secure=True)
    assert s.app_env == "prod"


# --- 回歸測試：migration 只需要 DATABASE_URL，不能被 SECRET_KEY 卡住 ---


def test_database_settings_works_without_secret_key(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite:///./custom/ledger.db")
    s = DatabaseSettings(_env_file=None)
    assert s.database_url == "sqlite:///./custom/ledger.db"
    assert not hasattr(s, "secret_key")


def test_full_settings_still_requires_secret_key(monkeypatch):
    # 同樣的環境下（有 DATABASE_URL、沒有 SECRET_KEY），完整設定仍必須驗證失敗
    monkeypatch.setenv("DATABASE_URL", "sqlite:///./custom/ledger.db")
    with pytest.raises(ValidationError, match="secret_key"):
        Settings(_env_file=None)
