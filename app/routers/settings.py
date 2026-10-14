"""/settings 設定（P3-2B，規格 F07、7.3）。目前只有「信用卡設定」一個區塊。

只接請求、檢查登入、呼叫 services/credit_cards.py、選模板；驗證規則（M07）都在 service。
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from starlette.responses import Response

from app.db import get_db
from app.deps import require_user
from app.models import User
from app.services import credit_cards
from app.services.errors import NotFound, RuleViolation
from app.templating import render

router = APIRouter()

# 不屬於某張卡欄位的錯誤（只有手動竄改請求才會遇到）顯示在頁面最上方
PAGE_LEVEL_CODES = {"NOT_CREDIT_CARD", "INACTIVE"}


def _page(
    request: Request,
    db: Session,
    *,
    status_code: int = 200,
    saved: int | None = None,
    error_for: int | None = None,
    error: RuleViolation | None = None,
    submitted: dict[str, str] | None = None,
) -> Response:
    return render(
        request,
        "pages/settings.html",
        {
            "rows": credit_cards.list_card_settings(db),
            "autopay_options": credit_cards.autopay_candidates(db),
            "saved": saved,
            "error_for": error_for,
            "error": error,
            "submitted": submitted or {},
        },
        status_code=status_code,
    )


def _to_int(text: str) -> int | None:
    """空白 → None（「必填」錯誤）；不是整數 → 0（超出 1–31，同樣由 service 擋下）。"""
    text = text.strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return 0


@router.get("/settings")
def settings_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    saved: int | None = None,
) -> Response:
    return _page(request, db, saved=saved)


@router.post("/settings/cards/{card_id:int}")
def save_card(
    request: Request,
    card_id: int,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    closing_day: Annotated[str, Form()] = "",
    due_day: Annotated[str, Form()] = "",
    autopay_account_id: Annotated[str, Form()] = "",
) -> Response:
    autopay = autopay_account_id.strip()
    try:
        credit_cards.update_card_settings(
            db,
            card_id,
            closing_day=_to_int(closing_day),
            due_day=_to_int(due_day),
            autopay_account_id=None if not autopay else (_to_int(autopay) or -1),
        )
    except NotFound:
        return RedirectResponse("/settings", status_code=303)
    except RuleViolation as exc:
        return _page(
            request,
            db,
            status_code=422,
            error_for=None if exc.code in PAGE_LEVEL_CODES else card_id,
            error=exc,
            submitted={
                "card_id": str(card_id),
                "closing_day": closing_day,
                "due_day": due_day,
                "autopay_account_id": autopay_account_id,
            },
        )
    return RedirectResponse(f"/settings?saved={card_id}", status_code=303)
