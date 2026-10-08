"""建立唯一的登入帳號（規格：單一使用者；P0 決策 Q1）。

用法（在專案根目錄）：
    alembic upgrade head
    python scripts/create_user.py

- 只需要 DATABASE_URL，不需要 SECRET_KEY。
- 預設用 getpass 輸入密碼（輸入時畫面不會顯示任何字元）。
- 密碼至少 12 個字元，規則與服務層 validate_new_password() 共用同一個函式。
- 終端機無法使用 getpass 時，可改用一次性環境變數 LEDGER_INITIAL_PASSWORD，
  使用方式見 README。這個變數只有本腳本會讀，讀取後立即從本程序移除；
  不寫入資料庫（資料庫只存 Argon2 雜湊）、不寫入日誌、不放進 .env.example。
- 已經有帳號時拒絕建立。
"""

import getpass
import os
import sys
from collections.abc import Callable, MutableMapping

from sqlalchemy import inspect

from app.config import get_database_settings
from app.db import make_engine, make_sessionmaker
from app.repositories import users as user_repo
from app.services.auth import (
    MIN_PASSWORD_LENGTH,
    UserAlreadyExists,
    create_initial_user,
    validate_new_password,
)

PASSWORD_ENV = "LEDGER_INITIAL_PASSWORD"

FALLBACK_HINT = (
    "無法從終端機讀取密碼，未建立帳號。\n"
    f"請改用一次性環境變數 {PASSWORD_ENV}，使用方式見 README「建立帳號」一節。"
)
CLEANUP_HINT = f"請立即清除環境變數：Remove-Item Env:{PASSWORD_ENV}"


def main(
    database_url: str | None = None,
    input_fn: Callable[[str], str] = input,
    getpass_fn: Callable[[str], str] = getpass.getpass,
    out: Callable[[str], None] = print,
    environ: MutableMapping[str, str] = os.environ,
) -> int:
    # 一開始就取出並移除，不論後續成功或失敗，本程序都不再保留這個值
    env_password = environ.pop(PASSWORD_ENV, "")

    engine = make_engine(database_url or get_database_settings().database_url)
    try:
        if not inspect(engine).has_table("users"):
            out("找不到 users 資料表，請先執行：alembic upgrade head")
            return 1

        with make_sessionmaker(engine)() as db:
            # 先檢查，避免使用者輸入完密碼才被拒絕
            if user_repo.count(db) > 0:
                out("已經有帳號了，本系統只允許一個帳號。")
                return 1

            username = input_fn("帳號：")

            prompt = f"密碼（至少 {MIN_PASSWORD_LENGTH} 個字元，輸入時不會顯示）："
            try:
                password = env_password or getpass_fn(prompt)
                if env_password:
                    out(f"使用環境變數 {PASSWORD_ENV} 提供的密碼。")
                # 與服務層同一條規則；先檢查，不必等到確認密碼後才發現太短
                validate_new_password(password)
                if not env_password and password != getpass_fn("再輸入一次密碼："):
                    out("兩次輸入的密碼不一致，未建立帳號。")
                    return 1
            except (EOFError, KeyboardInterrupt):
                out(FALLBACK_HINT)
                return 1
            except ValueError as exc:
                out(f"{exc}未建立帳號。")
                return 1

            try:
                user = create_initial_user(db, username, password)
            except (UserAlreadyExists, ValueError) as exc:
                out(f"{exc}未建立帳號。")
                return 1

        out(f"已建立帳號：{user.username}")
        if env_password:
            out(CLEANUP_HINT)
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
