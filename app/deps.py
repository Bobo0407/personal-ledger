"""Web 層共用依賴：設定、目前使用者、未登入導向。"""

from typing import Annotated
from urllib.parse import quote

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import get_db
from app.models import User
from app.services import auth

SESSION_COOKIE = "ledger_session"


class NotAuthenticated(Exception):
    def __init__(self, next_path: str) -> None:
        super().__init__(next_path)
        self.next_path = next_path


def get_app_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_current_user(request: Request, db: Annotated[Session, Depends(get_db)]) -> User | None:
    return auth.authenticate(db, request.cookies.get(SESSION_COOKIE))


def require_user(request: Request, user: Annotated[User | None, Depends(get_current_user)]) -> User:
    if user is None:
        path = request.url.path
        if request.url.query:
            path = f"{path}?{request.url.query}"
        raise NotAuthenticated(path)
    return user


def safe_next(value: str | None) -> str:
    """只允許站內相對路徑，避免 open redirect。不合格一律回首頁。"""
    if (
        not value
        or not value.startswith("/")
        or value.startswith("//")
        or "\\" in value
        or any(ord(ch) < 0x20 for ch in value)
    ):
        return "/"
    return value


def login_url(next_path: str) -> str:
    return f"/login?next={quote(safe_next(next_path), safe='')}"
