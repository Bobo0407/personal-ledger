"""/login、/logout（規格 7.3、9.3）。"""

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from starlette.responses import Response

from app.config import Settings
from app.db import get_db
from app.deps import SESSION_COOKIE, get_app_settings, get_current_user, safe_next
from app.models import User
from app.services import auth
from app.services.auth import LoginFailed
from app.templating import render

router = APIRouter()


@router.get("/login")
def login_page(
    request: Request,
    user: Annotated[User | None, Depends(get_current_user)],
    next_path: Annotated[str, Query(alias="next")] = "/",
) -> Response:
    if user is not None:
        return RedirectResponse(safe_next(next_path), status_code=303)
    return render(
        request,
        "pages/login.html",
        {"next": safe_next(next_path), "username": "", "error": None},
    )


@router.post("/login")
def login_submit(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    username: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
    next_path: Annotated[str, Form(alias="next")] = "/",
) -> Response:
    try:
        result = auth.login(
            db, settings, username, password, user_agent=request.headers.get("user-agent")
        )
    except LoginFailed as exc:
        # 只回填帳號，不回填密碼；訊息固定，不透露失敗原因
        return render(
            request,
            "pages/login.html",
            {"next": safe_next(next_path), "username": username, "error": str(exc)},
            status_code=401,
        )

    # 同一個瀏覽器重新登入時，撤銷舊的 Session
    old_token = request.cookies.get(SESSION_COOKIE)
    if old_token:
        auth.logout(db, old_token)

    response = RedirectResponse(safe_next(next_path), status_code=303)
    response.set_cookie(
        SESSION_COOKIE,
        result.token,
        max_age=settings.session_ttl_days * 24 * 60 * 60,
        path="/",
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
    )
    return response


@router.post("/logout")
def logout(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> Response:
    auth.logout(db, request.cookies.get(SESSION_COOKIE))
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(
        SESSION_COOKIE,
        path="/",
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
    )
    return response
