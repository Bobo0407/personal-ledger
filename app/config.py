"""應用程式設定：從環境變數與 .env 讀取（規格 4.3、1.6）。

- `.env.example` 只列出部署時需要填的值。
- 規格已定死的安全數值（30 天、5 次、15 分鐘）以預設值放在這裡，
  需要時仍可用環境變數覆寫，但不鼓勵。
- DatabaseSettings 只含 DATABASE_URL，給 Alembic 與資料庫層使用，
  不需要 SECRET_KEY；Settings 繼承它，再加上應用程式與安全設定。
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

AppEnv = Literal["dev", "test", "prod", "demo"]

PLACEHOLDER_SECRET = "change-me"
MIN_SECRET_LENGTH = 32


class DatabaseSettings(BaseSettings):
    """只讀 DATABASE_URL。extra="ignore"：.env 裡的其他變數（含 SECRET_KEY）一律忽略。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = "sqlite:///./data/ledger.db"


class BackupSettings(DatabaseSettings):
    """備份程序用的設定（P3-5A）。只需要資料庫與備份設定，不需要 SECRET_KEY。

    backup_age_recipient 是 age 的「公鑰」（age1…），不是私鑰；私鑰不放在伺服器上。
    缺少 recipient 或 rclone_remote 不在這裡擋下：由備份流程檢查並記錄一筆 FAILED，首頁才看得到。
    """

    backup_dir: Path = Path("./data/backups")  # data/ 已在 .gitignore
    backup_age_recipient: str | None = None
    backup_age_bin: str = "age"
    # 雲端備份（P3-5B；rclone）。rclone.conf 含 OAuth token，視為 secret，不進 Git。
    rclone_remote: str | None = None  # rclone config 裡的 remote 名稱；空 = 未設定
    rclone_path: str = "ledger-backups"  # remote 內的資料夾
    rclone_bin: str = "rclone"
    rclone_config: str | None = None  # 選填：rclone.conf 路徑（以 --config 傳給 rclone）


class Settings(DatabaseSettings):
    """完整的應用程式設定；啟動 Web 應用程式時使用，SECRET_KEY 必填。"""

    # --- 部署時要填（對應 .env.example）---
    app_env: AppEnv = "dev"
    secret_key: str = Field(min_length=1)
    session_cookie_secure: bool = False
    log_dir: Path = Path("./logs")
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # --- 規格定死的數值（1.6、9.3）---
    session_ttl_days: int = 30  # 登入後固定有效 30 天，不因使用而延長
    login_max_failures: int = 5
    login_lockout_minutes: int = 15
    # 時區不是部署設定，固定為 Asia/Taipei，定義在 app/clock.py

    @model_validator(mode="after")
    def _check_production_safety(self) -> "Settings":
        if self.app_env == "prod":
            if self.secret_key == PLACEHOLDER_SECRET or len(self.secret_key) < MIN_SECRET_LENGTH:
                raise ValueError(
                    f"APP_ENV=prod 時 SECRET_KEY 必須是至少 {MIN_SECRET_LENGTH} 字元的隨機字串"
                )
            if not self.session_cookie_secure:
                raise ValueError("APP_ENV=prod 時 SESSION_COOKIE_SECURE 必須為 true")
        return self


@lru_cache
def get_database_settings() -> DatabaseSettings:
    return DatabaseSettings()


@lru_cache
def get_settings() -> Settings:
    return Settings()
