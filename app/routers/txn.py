"""/txn/new 快速記帳（P2 Step 1：只支援 EXPENSE；規格 7.3、7.4、UF01、UF02、F15、F16）。

Router 只做：驗證登入、解析表單型態、呼叫 transactions.record_expense()、依結果選擇回應。
借貸、驗證規則、寫入全部在 P1 的 transactions／validation／LedgerService。

POST 只支援 HTMX（V1 不要求無 JS 版本），回應一律是 partials/ 片段：
- 200：已儲存（儲存並記下一筆），或「返回修改」重新顯示表單
- 200 + HX-Redirect：已儲存（儲存並回首頁）
- 422：表單解析失敗（Web 欄位錯誤）或 P1 ValidationFailed（規格 9.3）
- 409：P1 ConfirmationRequired，回傳確認畫面；按「仍要儲存」帶 confirmed 重送（規格 9.3、T19）
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy.orm import Session
from starlette.responses import Response

from app import clock
from app.db import get_db
from app.deps import require_user
from app.models import User
from app.schemas.txn import FormParseError, parse_expense
from app.services import entry_options, transactions
from app.services.issues import Issue
from app.services.validation import ConfirmationRequired, ValidationFailed
from app.templating import render

router = APIRouter()

FORM_FIELDS = ("txn_date", "amount", "account_id", "payment_method_id", "description")
# 「儲存並記下一筆」保留的欄位（UF02）；其他欄位清空
KEPT_AFTER_SAVE = ("txn_date", "payment_method_id")

ACTION_NEXT = "next"  # 儲存並記下一筆
ACTION_HOME = "home"  # 儲存並回首頁
ACTION_EDIT = "edit"  # 確認畫面的「返回修改」：不儲存，回到可編輯表單

# 錯誤顯示用：(規則代碼或 None, 訊息)。Web 解析錯誤沒有規則代碼。
ErrorItem = tuple[str | None, str]


def _options(db: Session) -> dict:
    methods, default_pm_id = entry_options.payment_method_choices(db)
    return {
        "account_groups": entry_options.expense_account_groups(db),
        "payment_methods": methods,
        "default_payment_method_id": default_pm_id,
    }


def _initial_values(default_pm_id: int | None) -> dict[str, str]:
    values = dict.fromkeys(FORM_FIELDS, "")
    values["txn_date"] = clock.today().isoformat()
    values["payment_method_id"] = "" if default_pm_id is None else str(default_pm_id)
    return values


def _form(
    request: Request,
    db: Session,
    values: dict[str, str],
    *,
    status_code: int = 200,
    field_errors: dict[str, list[ErrorItem]] | None = None,
    form_errors: list[ErrorItem] | None = None,
    saved: dict | None = None,
) -> Response:
    return render(
        request,
        "partials/txn_form.html",
        {
            **_options(db),
            "values": values,
            "field_errors": field_errors or {},
            "form_errors": form_errors or [],
            "saved": saved,
        },
        status_code=status_code,
    )


def _split_issues(issues: tuple[Issue, ...]) -> tuple[dict[str, list[ErrorItem]], list[ErrorItem]]:
    """有對應欄位的錯誤放在欄位旁；field 為 None 或畫面上沒有的欄位（例如分錄層級的
    V03、V04）放在表單頂端，不能默默吞掉。"""
    by_field: dict[str, list[ErrorItem]] = {}
    top: list[ErrorItem] = []
    for issue in issues:
        item = (issue.code, issue.message)
        if issue.field in FORM_FIELDS:
            by_field.setdefault(issue.field, []).append(item)
        else:
            top.append(item)
    return by_field, top


def _parse_confirmed(value: str) -> tuple[str, ...]:
    return tuple(code for code in (c.strip() for c in value.split(",")) if code)


@router.get("/txn/new")
def new_txn(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
) -> Response:
    options = _options(db)
    return render(
        request,
        "pages/txn_new.html",
        {
            **options,
            "values": _initial_values(options["default_payment_method_id"]),
            "field_errors": {},
            "form_errors": [],
            "saved": None,
        },
    )


@router.post("/txn/new")
def create_txn(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    txn_date: Annotated[str, Form()] = "",
    amount: Annotated[str, Form()] = "",
    account_id: Annotated[str, Form()] = "",
    payment_method_id: Annotated[str, Form()] = "",
    description: Annotated[str, Form()] = "",
    confirmed: Annotated[str, Form()] = "",
    action: Annotated[str, Form()] = ACTION_NEXT,
) -> Response:
    values = {
        "txn_date": txn_date,
        "amount": amount,
        "account_id": account_id,
        "payment_method_id": payment_method_id,
        "description": description,
    }
    if action == ACTION_EDIT:
        return _form(request, db, values)
    if action != ACTION_HOME:
        action = ACTION_NEXT

    try:
        form = parse_expense(values)
    except FormParseError as exc:
        field_errors = {f: [(None, msg)] for f, msg in exc.errors.items() if f in FORM_FIELDS}
        form_errors = [(None, msg) for f, msg in exc.errors.items() if f not in FORM_FIELDS]
        return _form(
            request,
            db,
            values,
            status_code=422,
            field_errors=field_errors,
            form_errors=form_errors,
        )

    try:
        result = transactions.record_expense(
            db, form, confirmed=_parse_confirmed(confirmed), user_id=user.id
        )
    except ValidationFailed as exc:
        field_errors, form_errors = _split_issues(exc.result.errors)
        return _form(
            request,
            db,
            values,
            status_code=422,
            field_errors=field_errors,
            form_errors=form_errors,
        )
    except ConfirmationRequired as exc:
        # 確認畫面只帶「這次觸發」的代碼，加上之前已確認的；內容不能在確認畫面修改，
        # 要修改就按「返回修改」回到表單（重新送出時會重新檢查、重新確認）。
        codes = dict.fromkeys((*_parse_confirmed(confirmed), *(i.code for i in exc.pending)))
        return render(
            request,
            "partials/txn_confirm.html",
            {
                "values": values,
                "pending": exc.pending,
                "confirmed": ",".join(codes),
                "action": action,
            },
            status_code=409,
        )

    if action == ACTION_HOME:
        return Response(status_code=200, headers={"HX-Redirect": "/"})

    # 儲存並記下一筆（UF02）：保留日期、付款方式，其他欄位清空
    next_values = dict.fromkeys(FORM_FIELDS, "")
    for key in KEPT_AFTER_SAVE:
        next_values[key] = values[key]
    return _form(
        request,
        db,
        next_values,
        saved={
            "transaction_id": result.transaction_id,
            "account_id": form.account_id,
            "amount": form.amount,
            "notices": result.notices,  # V13 等「提醒」：已經儲存，只顯示不阻擋
        },
    )
