"""500 錯誤處理（規格 9.3）與日誌內容（規格 9.4）。"""

import logging
import re
from typing import Annotated

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import security
from app.config import Settings
from app.db import get_db, make_sessionmaker
from app.deps import SESSION_COOKIE
from app.logging_config import APP_LOGGER, LOG_FILE_NAME
from app.main import create_app
from app.models import User

PASSWORD = "correct-password-xyz"
CSRF_INPUT = re.compile(r'name="csrf_token" value="([^"]+)"')


@pytest.fixture
def session_factory(migrated_engine):
    return make_sessionmaker(migrated_engine)


@pytest.fixture
def log_dir(tmp_path):
    return tmp_path / "logs"


@pytest.fixture
def app(session_factory, log_dir):
    settings = Settings(_env_file=None, secret_key="test-secret-key", log_dir=log_dir)
    application = create_app(settings)

    def override_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    application.dependency_overrides[get_db] = override_get_db

    # 只在測試裡加上的路由：寫入一筆資料後、commit 之前發生例外
    @application.get("/_boom")
    def boom(db: Annotated[Session, Depends(get_db)]):
        db.add(User(username="ghost", password_hash="x"))
        db.flush()
        raise RuntimeError("secret-internal-detail")

    return application


@pytest.fixture
def client(app):
    # raise_server_exceptions=False：模擬真實伺服器，拿到錯誤頁而不是讓例外丟進測試
    with TestClient(app, follow_redirects=False, raise_server_exceptions=False) as c:
        yield c


def read_log(log_dir) -> str:
    for h in logging.getLogger(APP_LOGGER).handlers:
        h.flush()
    return (log_dir / LOG_FILE_NAME).read_text(encoding="utf-8")


def user_count(session_factory) -> int:
    with session_factory() as db:
        return db.scalar(select(func.count()).select_from(User))


# --- 500 錯誤 ---


def test_unhandled_error_shows_generic_page(client):
    resp = client.get("/_boom")
    assert resp.status_code == 500
    assert "發生錯誤，資料未變更" in resp.text
    assert "secret-internal-detail" not in resp.text
    assert "Traceback" not in resp.text


def test_unhandled_error_does_not_change_data(client, session_factory):
    client.get("/_boom")
    assert user_count(session_factory) == 0


def test_unhandled_error_is_logged_with_traceback(client, log_dir):
    client.get("/_boom?token=should-not-be-logged")
    log = read_log(log_dir)
    assert "unhandled error GET /_boom" in log
    assert "secret-internal-detail" in log
    assert "Traceback" in log
    assert "should-not-be-logged" not in log  # 查詢字串不寫進日誌


# --- 日誌不含敏感資料 ---


def test_auth_logs_have_no_secrets(client, session_factory, log_dir):
    with session_factory() as db:
        db.add(User(username="bo", password_hash=security.hash_password(PASSWORD)))
        db.commit()

    def post_login(password: str):
        token = CSRF_INPUT.search(client.get("/login").text).group(1)
        return client.post(
            "/login", data={"username": "bo", "password": password, "csrf_token": token}
        )

    assert post_login("wrong-password-abc").status_code == 401
    assert post_login(PASSWORD).status_code == 303
    session_token = client.cookies.get(SESSION_COOKIE)
    assert session_token

    log = read_log(log_dir)
    assert "login failed reason=wrong_password" in log
    assert "login succeeded user_id=" in log
    for secret in (PASSWORD, "wrong-password-abc", session_token):
        assert secret not in log
