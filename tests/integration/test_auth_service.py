"""services/auth.py：登入、鎖定、Session 驗證與登出（規格 1.6、9.3、T28）。"""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, select

from app import clock, security
from app.config import Settings
from app.db import make_sessionmaker
from app.models import User, UserSession
from app.services import auth
from app.services.auth import LOGIN_FAILED_MESSAGE, LoginFailed

T0 = datetime(2026, 10, 7, 12, 0, 0, tzinfo=clock.APP_TIMEZONE)
PASSWORD = "correct-password"


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None, secret_key="test-secret")


@pytest.fixture
def db(migrated_engine):
    s = make_sessionmaker(migrated_engine)()
    yield s
    s.close()


@pytest.fixture
def user(db) -> User:
    u = User(username="bo", password_hash=security.hash_password(PASSWORD))
    db.add(u)
    db.commit()
    return u


def count(db, model) -> int:
    return db.scalar(select(func.count()).select_from(model))


def fail(db, settings, n: int, at: datetime = T0) -> None:
    for _ in range(n):
        with pytest.raises(LoginFailed):
            auth.login(db, settings, "bo", "wrong", now=at)


# --- 登入成功 ---


def test_login_success_creates_session(db, settings, user):
    result = auth.login(db, settings, "bo", PASSWORD, user_agent="pytest", now=T0)

    assert result.user_id == user.id
    assert result.expires_at == T0 + timedelta(days=30)

    us = db.scalar(select(UserSession))
    assert us.token_hash == security.hash_session_token(result.token)
    assert us.token_hash != result.token  # 資料庫只存雜湊
    assert us.expires_at == "2026-11-06T12:00:00+08:00"
    assert us.user_agent == "pytest"
    assert us.last_seen_at == "2026-10-07T12:00:00+08:00"  # 登入成功時寫入
    assert us.revoked_at is None


# --- 登入失敗與鎖定 ---


def test_wrong_password_counts_failure(db, settings, user):
    fail(db, settings, 1)
    db.refresh(user)
    assert user.failed_login_count == 1
    assert user.locked_until is None
    assert count(db, UserSession) == 0


def test_unknown_user_writes_nothing(db, settings):
    with pytest.raises(LoginFailed) as exc:
        auth.login(db, settings, "nobody", PASSWORD, now=T0)
    assert exc.value.reason == "invalid_credentials"
    assert count(db, User) == 0
    assert count(db, UserSession) == 0


def test_five_failures_lock_even_correct_password(db, settings, user):
    # T28：連錯 5 次後，就算密碼正確也不能登入
    fail(db, settings, 5)
    db.refresh(user)
    assert user.locked_until == "2026-10-07T12:15:00+08:00"

    with pytest.raises(LoginFailed) as exc:
        auth.login(db, settings, "bo", PASSWORD, now=T0 + timedelta(minutes=14, seconds=59))
    assert exc.value.reason == "locked"
    assert count(db, UserSession) == 0


def test_failures_during_lock_do_not_extend_it(db, settings, user):
    fail(db, settings, 5)
    fail(db, settings, 3, at=T0 + timedelta(minutes=10))
    db.refresh(user)
    assert user.locked_until == "2026-10-07T12:15:00+08:00"
    assert user.failed_login_count == 5


def test_login_allowed_after_lock_expires(db, settings, user):
    fail(db, settings, 5)
    auth.login(db, settings, "bo", PASSWORD, now=T0 + timedelta(minutes=15))
    db.refresh(user)
    assert user.failed_login_count == 0
    assert user.locked_until is None


def test_count_restarts_from_zero_after_lock_expires(db, settings, user):
    fail(db, settings, 5)
    fail(db, settings, 1, at=T0 + timedelta(minutes=16))
    db.refresh(user)
    assert user.failed_login_count == 1
    assert user.locked_until is None


def test_success_resets_failure_count(db, settings, user):
    fail(db, settings, 4)
    auth.login(db, settings, "bo", PASSWORD, now=T0)
    db.refresh(user)
    assert user.failed_login_count == 0


def test_all_failures_share_one_message(db, settings, user):
    messages = set()
    for name, pw in [("nobody", PASSWORD), ("bo", "wrong")]:
        with pytest.raises(LoginFailed) as exc:
            auth.login(db, settings, name, pw, now=T0)
        messages.add(str(exc.value))
    fail(db, settings, 4)
    with pytest.raises(LoginFailed) as exc:
        auth.login(db, settings, "bo", PASSWORD, now=T0)  # 已鎖定
    messages.add(str(exc.value))
    assert messages == {LOGIN_FAILED_MESSAGE}


# --- Session 驗證 ---


def test_authenticate_valid_token_is_read_only(db, settings, user):
    token = auth.login(db, settings, "bo", PASSWORD, now=T0).token
    before = db.scalar(select(UserSession))
    snapshot = (before.last_seen_at, before.expires_at, before.updated_at)

    assert auth.authenticate(db, token, now=T0 + timedelta(days=1)) == user

    db.expire_all()  # 重新從資料庫讀，確認沒有任何寫入
    after = db.scalar(select(UserSession))
    assert (after.last_seen_at, after.expires_at, after.updated_at) == snapshot
    assert after.last_seen_at == "2026-10-07T12:00:00+08:00"


@pytest.mark.parametrize("token", [None, "", "not-a-real-token"])
def test_authenticate_rejects_missing_or_unknown_token(db, token):
    assert auth.authenticate(db, token, now=T0) is None


def test_session_expiry_is_fixed_not_sliding(db, settings, user):
    token = auth.login(db, settings, "bo", PASSWORD, now=T0).token
    # 第 29 天還在用，效期也不會延長
    assert auth.authenticate(db, token, now=T0 + timedelta(days=29)) == user
    assert db.scalar(select(UserSession)).expires_at == "2026-11-06T12:00:00+08:00"
    # 剛好 30 天到期
    assert auth.authenticate(db, token, now=T0 + timedelta(days=30)) is None


# --- 登出 ---


def test_logout_revokes_session(db, settings, user):
    token = auth.login(db, settings, "bo", PASSWORD, now=T0).token
    auth.logout(db, token, now=T0 + timedelta(hours=1))

    assert db.scalar(select(UserSession)).revoked_at == "2026-10-07T13:00:00+08:00"
    assert auth.authenticate(db, token, now=T0 + timedelta(hours=2)) is None


def test_logout_is_idempotent(db, settings, user):
    token = auth.login(db, settings, "bo", PASSWORD, now=T0).token
    auth.logout(db, token, now=T0 + timedelta(hours=1))
    auth.logout(db, token, now=T0 + timedelta(hours=2))  # 不覆蓋第一次的撤銷時間
    auth.logout(db, "unknown-token")
    auth.logout(db, None)
    assert db.scalar(select(UserSession)).revoked_at == "2026-10-07T13:00:00+08:00"
