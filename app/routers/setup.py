"""/setup/opening 期初餘額精靈（P3-7；規格 F04、7.3）。只在第一次上線時使用。

Router 只做：驗證登入、解析表單、呼叫 transactions.record_opening()、依結果選擇回應。
借貸、驗證（V05、OPENING_EXISTS…）與寫入全部在 P1 的 OpeningEntry／validation／LedgerService；
這裡不碰分錄、不算餘額。

- 普通 POST（不用 HTMX）。空白的欄位略過；填 0 也略過（P1 R1）。
- 同一帳戶已經有有效期初時，畫面不提供輸入欄位；手動竄改請求也會被 P1 的 OPENING_EXISTS 擋下，
  所以重複送出不會建立第二筆。
- 錯誤：欄位解析失敗或 P1 ValidationFailed → 422，並保留已輸入的值；
  P1 ConfirmationRequired（例如未來日期 V11）→ 409，同一個表單帶 confirmed 再送一次。
"""

import re
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from starlette.responses import Response

from app import clock
from app.db import get_db
from app.deps import require_user
from app.models import User
from app.services import opening_setup, transactions
from app.services.entries import OpeningForm, OpeningLine
from app.services.validation import ConfirmationRequired, ValidationFailed
from app.templating import render

router = APIRouter()

INTEGER = re.compile(r"[+-]?\d+")


def _page(
    request: Request,
    db: Session,
    *,
    status_code: int = 200,
    saved: bool = False,
    values: dict[str, str] | None = None,
    txn_date: str | None = None,
    errors: list[str] | None = None,
    row_errors: dict[str, str] | None = None,
    pending: list[str] | None = None,
    confirmed: str = "",
) -> Response:
    return render(
        request,
        "pages/setup_opening.html",
        {
            "rows": opening_setup.opening_rows(db),
            "saved": saved,
            "values": values or {},
            "txn_date": txn_date if txn_date is not None else clock.today().isoformat(),
            "errors": errors or [],
            "row_errors": row_errors or {},
            "pending": pending or [],
            "confirmed": confirmed,
        },
        status_code=status_code,
    )


@router.get("/setup/opening")
def opening_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    saved: int = 0,
) -> Response:
    return _page(request, db, saved=bool(saved))


@router.post("/setup/opening")
def save_opening(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    txn_date: Annotated[str, Form()] = "",
    account_id: Annotated[list[int] | None, Form()] = None,
    balance: Annotated[list[str] | None, Form()] = None,
    confirmed: Annotated[str, Form()] = "",
) -> Response:
    ids = account_id or []
    texts = balance or []
    values = {str(a): t for a, t in zip(ids, texts, strict=False)}

    def fail(errors: list[str], row_errors: dict[str, str] | None = None) -> Response:
        return _page(
            request,
            db,
            status_code=422,
            values=values,
            txn_date=txn_date,
            errors=errors,
            row_errors=row_errors,
            confirmed=confirmed,
        )

    if len(ids) != len(texts):
        return fail(["表單內容不完整，請重新整理頁面後再試"])
    try:
        day = date.fromisoformat(txn_date.strip())
    except ValueError:
        return fail(["請輸入期初日期"])

    lines: list[OpeningLine] = []
    row_errors: dict[str, str] = {}
    for acc_id, text in zip(ids, texts, strict=True):
        cleaned = text.strip().replace(",", "")
        if not cleaned:
            continue  # 空白 = 這個帳戶先不輸入
        if not INTEGER.fullmatch(cleaned):
            row_errors[str(acc_id)] = "請輸入整數金額"
            continue
        lines.append(OpeningLine(acc_id, int(cleaned)))
    if row_errors:
        return fail([], row_errors)

    form = OpeningForm(day, tuple(lines))
    codes = tuple(c for c in confirmed.split(",") if c)
    try:
        transactions.record_opening(db, form, confirmed=codes, user_id=user.id)
    except ValidationFailed as exc:
        return fail([issue.message for issue in exc.result.errors])
    except ConfirmationRequired as exc:
        return _page(
            request,
            db,
            status_code=409,
            values=values,
            txn_date=txn_date,
            pending=[issue.message for issue in exc.pending],
            confirmed=",".join(issue.code for issue in exc.pending),
        )
    return RedirectResponse("/setup/opening?saved=1", status_code=303)
