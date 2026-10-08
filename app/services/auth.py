"""登入、Session 驗證、登出（規格 1.6、9.3、7.1 auth.py）。

規則：
- 連續失敗 login_max_failures 次（預設 5）→ 鎖定 login_lockout_minutes 分鐘（預設 15）。
- 鎖定期間：就算密碼正確也不能登入；失敗不再累計，也不延長鎖定。
- 鎖定期滿：計數從 0 重新開始。登入成功：計數歸零。
- 帳號不存在：不寫入任何資料，但仍做一次同等成本的密碼驗證。
- Session：登入後固定有效 session_ttl_days 天（預設 30），不因使用而延長。
- last_seen_at 只在登入成功時寫入；驗證 Session 是唯讀，不會每個請求都寫資料庫。
- 所有失敗對外都是同一個訊息，不透露是帳號錯、密碼錯還是被鎖定。
- 日誌只記 user_id 與原因代碼，不記帳號、密碼、權杖（規格 9.4）。
- 系統只允許一個帳號：create_initial_user 在已有帳號時拒絕建立。
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from sqlalchemy.orm import Session

from app import clock, security
from app.config import Settings
from app.models import User, UserSession
from app.repositories import sessions as session_repo
from app.repositories import users as user_repo

logger = logging.getLogger(__name__)

LOGIN_FAILED_MESSAGE = "帳號或密碼錯誤，或嘗試次數過多，請稍後再試。"

FailureReason = Literal["invalid_credentials", "locked"]


class LoginFailed(Exception):
    """對外訊息固定；reason 只給日誌與測試使用，不能顯示在畫面上。"""

    def __init__(self, reason: FailureReason) -> None:
        super().__init__(LOGIN_FAILED_MESSAGE)
        self.reason = reason


@dataclass(frozen=True)
class LoginSuccess:
    user_id: int
    token: str  # 原始權杖，只交給 Cookie，不寫入資料庫或日誌
    expires_at: datetime


def login(
    db: Session,
    settings: Settings,
    username: str,
    password: str,
    user_agent: str | None = None,
    now: datetime | None = None,
) -> LoginSuccess:
    now = now or clock.now()
    now_iso = clock.to_iso(now)

    user = user_repo.get_by_username(db, username)
    if user is None:
        security.burn_password_check(password)
        logger.warning("login failed reason=unknown_user")
        raise LoginFailed("invalid_credentials")

    if user.locked_until is not None:
        if user.locked_until > now_iso:
            security.burn_password_check(password)
            logger.warning("login failed reason=locked user_id=%s", user.id)
            raise LoginFailed("locked")
        # 鎖定期滿：重新計數
        user.failed_login_count = 0
        user.locked_until = None

    if not security.verify_password(user.password_hash, password):
        _record_failure(user, settings, now)
        db.commit()
        logger.warning(
            "login failed reason=wrong_password user_id=%s failures=%s",
            user.id,
            user.failed_login_count,
        )
        if user.locked_until is not None:
            logger.warning("account locked user_id=%s until=%s", user.id, user.locked_until)
        raise LoginFailed("invalid_credentials")

    user.failed_login_count = 0
    user.locked_until = None

    token = security.generate_session_token()
    expires_at = now + timedelta(days=settings.session_ttl_days)
    session_repo.add(
        db,
        UserSession(
            user=user,
            token_hash=security.hash_session_token(token),
            expires_at=clock.to_iso(expires_at),
            last_seen_at=now_iso,
            user_agent=user_agent,
        ),
    )
    db.commit()
    logger.info("login succeeded user_id=%s", user.id)
    return LoginSuccess(user_id=user.id, token=token, expires_at=expires_at)


def _record_failure(user: User, settings: Settings, now: datetime) -> None:
    user.failed_login_count += 1
    if user.failed_login_count >= settings.login_max_failures:
        user.locked_until = clock.to_iso(now + timedelta(minutes=settings.login_lockout_minutes))


def authenticate(db: Session, token: str | None, now: datetime | None = None) -> User | None:
    """有效的 Session 權杖回傳使用者，否則回傳 None。唯讀：不更新任何欄位，也不延長效期。"""
    if not token:
        return None
    now_iso = clock.to_iso(now or clock.now())

    user_session = session_repo.get_by_token_hash(db, security.hash_session_token(token))
    if user_session is None or user_session.revoked_at is not None:
        return None
    if user_session.expires_at <= now_iso:
        return None
    return user_session.user


def logout(db: Session, token: str | None, now: datetime | None = None) -> None:
    """撤銷這個權杖對應的 Session。權杖不存在或已撤銷時什麼都不做。"""
    if not token:
        return
    user_session = session_repo.get_by_token_hash(db, security.hash_session_token(token))
    if user_session is None or user_session.revoked_at is not None:
        return
    user_session.revoked_at = clock.to_iso(now or clock.now())
    db.commit()
    logger.info("logout user_id=%s session_id=%s", user_session.user_id, user_session.id)


class UserAlreadyExists(Exception):
    pass


MIN_PASSWORD_LENGTH = 12


def validate_new_password(password: str) -> None:
    """新密碼規則（P0 決策）：至少 12 個字元，不要求大小寫、數字或符號組合。

    只用於設定新密碼；登入時不檢查，避免規則調整後鎖住既有帳號。
    """
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"密碼至少需要 {MIN_PASSWORD_LENGTH} 個字元。")


def create_initial_user(db: Session, username: str, password: str) -> User:
    """建立唯一的登入帳號。已有帳號時丟出 UserAlreadyExists。"""
    username = username.strip()
    if not username:
        raise ValueError("帳號不可空白。")
    validate_new_password(password)
    if user_repo.count(db) > 0:
        raise UserAlreadyExists("已經有帳號了，本系統只允許一個帳號。")

    user = User(username=username, password_hash=security.hash_password(password))
    db.add(user)
    db.commit()
    logger.info("user created user_id=%s", user.id)
    return user
