"""/budgets 預算設定與進度（P3-3B，規格 7.3、F20）。

只接請求、檢查登入、呼叫 services/budgets.py、選模板；M05、金額與月份驗證都在 service
（不信任前端的 min）。沒有取消／刪除預算的入口（V1 不支援）。
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from starlette.responses import Response

from app.db import get_db
from app.deps import require_user
from app.models import User
from app.services import budgets
from app.services.errors import NotFound, RuleViolation
from app.templating import render

router = APIRouter()

# 不屬於某個科目欄位的錯誤（只有手動竄改請求才會遇到）顯示在頁面最上方
PAGE_LEVEL_CODES = {"M05", "INACTIVE"}
AMOUNT_CODES = {"BUDGET_AMOUNT"}
MONTH_CODES = {"BUDGET_MONTH", "BUDGET_PAST_MONTH"}


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
        "pages/budgets.html",
        {
            "settings": budgets.settings_rows(db),
            "saved": saved,
            "error_for": error_for,
            "error": error,
            "error_field": (
                "amount"
                if error is not None and error.code in AMOUNT_CODES
                else "month"
                if error is not None and error.code in MONTH_CODES
                else None
            ),
            "submitted": submitted or {},
        },
        status_code=status_code,
    )


def _to_int(text: str) -> int | None:
    """空白 → None；不是整數 → 0。兩者都不是合法的預算金額，由 service 回 BUDGET_AMOUNT。"""
    text = text.strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return 0


@router.get("/budgets")
def budgets_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    saved: int | None = None,
) -> Response:
    return _page(request, db, saved=saved)


@router.post("/budgets/{account_id:int}")
def save_budget(
    request: Request,
    account_id: int,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    monthly_amount: Annotated[str, Form()] = "",
    effective_from: Annotated[str, Form()] = "",
) -> Response:
    try:
        budgets.set_budget(
            db,
            account_id,
            effective_from.strip(),
            _to_int(monthly_amount),  # type: ignore[arg-type]
        )
    except NotFound:
        return RedirectResponse("/budgets", status_code=303)
    except RuleViolation as exc:
        return _page(
            request,
            db,
            status_code=422,
            error_for=None if exc.code in PAGE_LEVEL_CODES else account_id,
            error=exc,
            submitted={
                "account_id": str(account_id),
                "monthly_amount": monthly_amount,
                "effective_from": effective_from,
            },
        )
    return RedirectResponse(f"/budgets?saved={account_id}", status_code=303)
