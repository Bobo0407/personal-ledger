"""/reports/monthly 月報（P3-4，規格 7.3、8.2）。

只接請求、檢查登入、呼叫 services/reports.py、選模板；月份驗證與所有數字都在 service。
月份格式不對 → 422 完整頁（保留月份選擇器），不 500、不靜默改成本月。
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session
from starlette.responses import Response

from app.db import get_db
from app.deps import require_user
from app.models import User
from app.services import reports
from app.services.errors import RuleViolation
from app.templating import render

router = APIRouter()


@router.get("/reports/monthly")
def monthly(
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[Session, Depends(get_db)],
    month: Annotated[str | None, Query()] = None,
) -> Response:
    try:
        report = reports.monthly_report(db, month)
    except RuleViolation as exc:
        return render(
            request,
            "pages/report_monthly.html",
            {"report": None, "error": exc},
            status_code=422,
        )
    return render(request, "pages/report_monthly.html", {"report": report, "error": None})
