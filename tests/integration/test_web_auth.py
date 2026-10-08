"""Web 層：未登入導向、登入、登出、CSRF（規格 9.3、T27–T29）。"""

import re

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import func, select

from app import config, security
from app.config import Settings
from app.csrf import CSRF_COOKIE
from app.db import get_db, make_sessionmaker
from app.deps import SESSION_COOKIE
from app.main import create_app
from app.models import User, UserSession
from app.services.auth import LOGIN_FAILED_MESSAGE

PASSWORD = "correct-password"
CSRF_INPUT = re.compile(r'name="csrf_token" value="([^"]+)"')


@pytest.fixture
def session_factory(migrated_engine):
    return make_sessionmaker(migrated_engine)


@pytest.fixture
def client(session_factory, tmp_path):
    # 日誌寫到暫存目錄，不污染專案的 ./logs
    settings = Settings(_env_file=None, secret_key="test-secret-key", log_dir=tmp_path / "logs")
    app = create_app(settings)

    def override_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app, follow_redirects=False) as c:
        yield c


@pytest.fixture
def user(session_factory):
    with session_factory() as db:
        db.add(User(username="bo", password_hash=security.hash_password(PASSWORD)))
        db.commit()


def csrf_token_from(client: TestClient, path: str = "/login") -> str:
    resp = client.get(path)
    assert resp.status_code == 200
    match = CSRF_INPUT.search(resp.text)
    assert match, "頁面上找不到 csrf_token 欄位"
    return match.group(1)


def do_login(client, password=PASSWORD, next_path="/", token=None):
    token = token if token is not None else csrf_token_from(client)
    return client.post(
        "/login",
        data={"username": "bo", "password": password, "next": next_path, "csrf_token": token},
    )


def session_count(session_factory) -> int:
    with session_factory() as db:
        return db.scalar(select(func.count()).select_from(UserSession))


# --- 未登入導向（T27 的 P0 版本）---


def test_home_requires_login(client):
    resp = client.get("/")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login?next=%2F"


def test_redirect_keeps_original_path_and_query(client):
    resp = client.get("/?tab=1")
    assert resp.headers["location"] == "/login?next=%2F%3Ftab%3D1"


def test_login_page_renders_form_and_csrf_cookie(client):
    resp = client.get("/login")
    assert resp.status_code == 200
    assert CSRF_INPUT.search(resp.text)
    assert CSRF_COOKIE in resp.cookies
    assert resp.headers["cache-control"] == "no-store"


# --- 登入 ---


def test_login_success(client, user, session_factory):
    resp = do_login(client)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"

    set_cookie = resp.headers["set-cookie"]
    assert f"{SESSION_COOKIE}=" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "Max-Age=2592000" in set_cookie  # 固定 30 天
    assert "samesite=lax" in set_cookie.lower()
    assert session_count(session_factory) == 1

    home = client.get("/")
    assert home.status_code == 200
    assert "bo" in home.text


def test_login_redirects_to_next(client, user):
    resp = do_login(client, next_path="/txn?page=2")
    assert resp.headers["location"] == "/txn?page=2"


@pytest.mark.parametrize("evil", ["https://evil.example", "//evil.example"])
def test_login_blocks_open_redirect(client, user, evil):
    resp = do_login(client, next_path=evil)
    assert resp.headers["location"] == "/"


def test_wrong_password_shows_generic_error(client, user, session_factory):
    resp = do_login(client, password="wrong-password")
    assert resp.status_code == 401
    assert LOGIN_FAILED_MESSAGE in resp.text
    assert 'value="bo"' in resp.text  # 帳號保留
    assert "wrong-password" not in resp.text  # 密碼不回填
    assert SESSION_COOKIE not in resp.cookies
    assert session_count(session_factory) == 0


def test_lockout_through_web(client, user):
    # T28：連錯 5 次後，正確密碼也登不進去
    for _ in range(5):
        assert do_login(client, password="wrong").status_code == 401
    resp = do_login(client)
    assert resp.status_code == 401
    assert LOGIN_FAILED_MESSAGE in resp.text


def test_logged_in_user_visiting_login_is_redirected(client, user):
    do_login(client)
    resp = client.get("/login")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"


def test_relogin_revokes_previous_session(client, user, session_factory):
    do_login(client)
    # 已登入時 /login 會導回首頁，所以從首頁的登出表單取得 CSRF 權杖
    do_login(client, token=csrf_token_from(client, "/"))
    with session_factory() as db:
        sessions = db.scalars(select(UserSession).order_by(UserSession.id)).all()
    assert len(sessions) == 2
    assert sessions[0].revoked_at is not None
    assert sessions[1].revoked_at is None


# --- CSRF（T29）---


def test_login_without_csrf_token_is_forbidden(client, user, session_factory):
    client.get("/login")  # 有 CSRF Cookie，但表單沒帶權杖
    resp = client.post("/login", data={"username": "bo", "password": PASSWORD})
    assert resp.status_code == 403
    assert session_count(session_factory) == 0


def test_login_with_forged_csrf_token_is_forbidden(client, user, session_factory):
    client.get("/login")  # 有合法的 CSRF Cookie，但表單權杖是偽造的
    resp = do_login(client, token="forged")
    assert resp.status_code == 403
    assert session_count(session_factory) == 0


def test_login_without_csrf_cookie_is_forbidden(client, user):
    token = csrf_token_from(client)
    client.cookies.clear()
    resp = do_login(client, token=token)
    assert resp.status_code == 403


def test_csrf_header_is_accepted(client, user):
    # 之後 HTMX 會用標頭帶權杖
    token = csrf_token_from(client)
    resp = client.post(
        "/login",
        data={"username": "bo", "password": PASSWORD},
        headers={"X-CSRF-Token": token},
    )
    assert resp.status_code == 303


# --- 登出 ---


def test_logout_without_csrf_is_forbidden(client, user):
    do_login(client)
    resp = client.post("/logout")
    assert resp.status_code == 403
    assert client.get("/").status_code == 200  # 仍然是登入狀態


def test_logout_revokes_session(client, user, session_factory):
    do_login(client)
    token = csrf_token_from(client, "/")
    resp = client.post("/logout", data={"csrf_token": token})
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"

    with session_factory() as db:
        assert db.scalar(select(UserSession)).revoked_at is not None
    assert client.get("/").status_code == 303


def test_revoked_cookie_cannot_be_reused(client, user):
    do_login(client)
    stolen = client.cookies.get(SESSION_COOKIE)
    assert stolen
    client.post("/logout", data={"csrf_token": csrf_token_from(client, "/")})

    resp = client.get("/", headers={"Cookie": f"{SESSION_COOKIE}={stolen}"})
    assert resp.status_code == 303


# --- 啟動檢查 ---


def test_create_app_fails_fast_without_secret_key(monkeypatch, tmp_path):
    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.chdir(tmp_path)  # 確保讀不到專案的 .env
    config.get_settings.cache_clear()
    try:
        with pytest.raises(ValidationError):
            create_app()
    finally:
        config.get_settings.cache_clear()
