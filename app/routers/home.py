"""/ 首頁（規格 8.1、F26）。P3 逐步加入區塊；P3-1：快速記帳入口、帳戶餘額、帳平檢查警示。

router 只驗證登入、呼叫 services/home.py、回傳頁面；不做任何加總。
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session
from starlette.responses import Response

from app import flash
from app.db import get_db
from app.deps import require_user
from app.models import User
from app.services import home as home_service
from app.templating import render

router = APIRouter()


@router.get("/")
def home(
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[Session, Depends(get_db)],
) -> Response:
    summary = home_service.home_summary(db)
    response = render(
        request,
        "pages/home.html",
        {"user": user, "summary": summary, "flash_notices": flash.read(request)},
    )
    flash.pop(request, response)  # 一次性提醒：顯示過就清掉，重新整理不再出現
    return response
