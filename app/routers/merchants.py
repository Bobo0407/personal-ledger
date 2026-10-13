"""/merchants 商家與別名管理（P2 Step 9，F17）。

V1 只管理別名：列出商家（含停用）、新增別名、刪除別名。商家改名／合併／刪除、
啟用停用、預設科目與付款方式都不在這一頁（預設值的設定介面延到 V1.5／S01）。
規則都在 services/merchants.py；這裡只接請求、檢查登入、呼叫服務、選模板。
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from starlette.responses import Response

from app.db import get_db
from app.deps import require_user
from app.models import User
from app.services import merchants as merchant_service
from app.services.errors import NotFound, RuleViolation
from app.templating import render

router = APIRouter()


def _is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"


def _page(
    request: Request,
    db: Session,
    *,
    status_code: int = 200,
    error_for: int | None = None,
    error: RuleViolation | None = None,
    alias_value: str = "",
) -> Response:
    return render(
        request,
        "pages/merchants.html",
        {
            "merchants": merchant_service.list_with_aliases(db),
            "error_for": error_for,
            "error": error,
            "alias_value": alias_value,
        },
        status_code=status_code,
    )


def _section(
    request: Request,
    db: Session,
    merchant_id: int,
    *,
    status_code: int = 200,
    error: RuleViolation | None = None,
    alias_value: str = "",
    done_message: str | None = None,
) -> Response:
    """HTMX 回應：只換掉這個商家的區塊。"""
    return render(
        request,
        "partials/merchant_item.html",
        {
            "m": merchant_service.get_view(db, merchant_id),
            "error_for": merchant_id if error is not None else None,
            "error": error,
            "alias_value": alias_value,
            "done_message": done_message,
        },
        status_code=status_code,
    )


def _after_write(request: Request, db: Session, merchant_id: int, message: str) -> Response:
    if _is_htmx(request):
        return _section(request, db, merchant_id, done_message=message)
    return RedirectResponse("/merchants", status_code=303)


def _not_found(request: Request) -> Response:
    """商家或別名已不存在（例如另一個分頁剛刪掉）：重新載入整頁。"""
    if _is_htmx(request):
        return Response(status_code=404, headers={"HX-Redirect": "/merchants"})
    return RedirectResponse("/merchants", status_code=303)


@router.get("/merchants")
def merchants_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
) -> Response:
    return _page(request, db)


@router.post("/merchants/{merchant_id:int}/aliases")
def add_alias(
    request: Request,
    merchant_id: int,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    alias: Annotated[str, Form()] = "",
) -> Response:
    try:
        created = merchant_service.add_alias(db, merchant_id, alias)
    except NotFound:
        return _not_found(request)
    except RuleViolation as exc:
        if _is_htmx(request):
            return _section(request, db, merchant_id, status_code=422, error=exc, alias_value=alias)
        return _page(
            request, db, status_code=422, error_for=merchant_id, error=exc, alias_value=alias
        )
    return _after_write(request, db, merchant_id, f"已新增別名「{created.alias}」")


@router.post("/merchants/{merchant_id:int}/aliases/{alias_id:int}/delete")
def delete_alias(
    request: Request,
    merchant_id: int,
    alias_id: int,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
) -> Response:
    try:
        merchant_service.remove_alias(db, merchant_id, alias_id)
    except NotFound:
        return _not_found(request)
    return _after_write(request, db, merchant_id, "已刪除別名")
