"""/txn 交易列表與搜尋、/txn/new 快速記帳。

/txn/new（P2 Step 1：只支援 EXPENSE；規格 7.3、7.4、UF01、UF02、F15、F16）。

Router 只做：驗證登入、解析表單型態、呼叫 transactions.record_expense()、依結果選擇回應。
借貸、驗證規則、寫入全部在 P1 的 transactions／validation／LedgerService。

POST 只支援 HTMX（V1 不要求無 JS 版本），回應一律是 partials/ 片段：
- 200：已儲存（儲存並記下一筆），或「返回修改」重新顯示表單
- 200 + HX-Redirect：已儲存（儲存並回首頁）
- 422：表單解析失敗（Web 欄位錯誤）或 P1 ValidationFailed（規格 9.3）
- 409：P1 ConfirmationRequired，回傳確認畫面；按「仍要儲存」帶 confirmed 重送（規格 9.3、T19）

P2 Step 3：GET /txn 交易列表與搜尋（F22、F23、UF10）
- 條件放在 query string（可書籤、重新整理保留）；完全沒帶條件時預設本月。
- 條件欄位出現但是空白，代表不限（例如清空日期 = 不限日期）。
- HTMX 請求只回傳 partials/txn_list_body.html（篩選表單 + 結果），換掉 #txn-list。
- 條件不合理回 422；搜尋只讀，不建立商家。

P2 Step 2：
- 商家：輸入名稱，services/merchants.resolve_or_create() 換成 merchant_id（決策 M2）。
- 憑證類型、發票號碼：V08 由 P1 validation 判斷。
- 一次性送出權杖（規格 9.3「重複送出」，決策 A）：GET 時產生；422、409、返回修改沿用同一個；
  儲存成功後換新的。同一個權杖再送一次時不會新增交易，回傳第一次建立的那筆。
"""

import secrets
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy.orm import Session
from starlette.responses import Response

from app import clock
from app.db import get_db
from app.deps import require_user
from app.models import User
from app.schemas.search import parse_page, parse_search
from app.schemas.txn import FormParseError, parse_expense
from app.services import entry_options, merchants, search, transactions
from app.services.issues import Issue
from app.services.validation import ConfirmationRequired, ValidationFailed
from app.templating import render

router = APIRouter()

# 畫面上的欄位（錯誤可以顯示在欄位旁）
FORM_FIELDS = (
    "txn_date",
    "amount",
    "account_id",
    "payment_method_id",
    "description",
    "merchant_name",
    "voucher_type",
    "invoice_no",
)
TOKEN_FIELD = "submission_token"
# P1 Issue.field 與畫面欄位名稱不同的對應：商家在畫面上輸入的是名稱
ISSUE_FIELD_TO_FORM = {"merchant_id": "merchant_name"}
# 「儲存並記下一筆」保留的欄位（UF02）；其他欄位清空，並換一個新的送出權杖
KEPT_AFTER_SAVE = ("txn_date", "payment_method_id")

ACTION_NEXT = "next"  # 儲存並記下一筆
ACTION_HOME = "home"  # 儲存並回首頁
ACTION_EDIT = "edit"  # 確認畫面的「返回修改」：不儲存，回到可編輯表單

# 錯誤顯示用：(規則代碼或 None, 訊息)。Web 解析錯誤沒有規則代碼。
ErrorItem = tuple[str | None, str]


def _new_token() -> str:
    return secrets.token_urlsafe(24)


def _options(db: Session) -> dict:
    methods, default_pm_id = entry_options.payment_method_choices(db)
    return {
        "account_groups": entry_options.expense_account_groups(db),
        "payment_methods": methods,
        "default_payment_method_id": default_pm_id,
        "merchant_names": entry_options.merchant_names(db),
        "voucher_choices": entry_options.voucher_choices(),
    }


def _blank_values() -> dict[str, str]:
    values = dict.fromkeys(FORM_FIELDS, "")
    values[TOKEN_FIELD] = _new_token()
    return values


def _initial_values(default_pm_id: int | None) -> dict[str, str]:
    values = _blank_values()
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
        field = ISSUE_FIELD_TO_FORM.get(issue.field, issue.field)
        if field in FORM_FIELDS:
            by_field.setdefault(field, []).append(item)
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
    merchant_name: Annotated[str, Form()] = "",
    voucher_type: Annotated[str, Form()] = "",
    invoice_no: Annotated[str, Form()] = "",
    submission_token: Annotated[str, Form()] = "",
    confirmed: Annotated[str, Form()] = "",
    action: Annotated[str, Form()] = ACTION_NEXT,
) -> Response:
    values = {
        "txn_date": txn_date,
        "amount": amount,
        "account_id": account_id,
        "payment_method_id": payment_method_id,
        "description": description,
        "merchant_name": merchant_name,
        "voucher_type": voucher_type,
        "invoice_no": invoice_no,
        TOKEN_FIELD: submission_token,
    }
    if action == ACTION_EDIT:
        return _form(request, db, values)
    if action != ACTION_HOME:
        action = ACTION_NEXT

    try:
        data = parse_expense(values)
    except FormParseError as exc:
        if TOKEN_FIELD in exc.errors:
            # 權杖缺少或格式不對：這份表單不能再用；換一個新權杖讓使用者重新送出
            values[TOKEN_FIELD] = _new_token()
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

    # 已用過的權杖：直接回傳第一次的結果，不建立商家、不驗證、不寫入（重送不得有任何副作用）
    result = transactions.find_replay(db, data.submission_token)
    if result is None:
        merchant_id = None
        if data.merchant_name is not None:
            merchant_id = merchants.resolve_or_create(db, data.merchant_name).id
        form = data.to_form(merchant_id=merchant_id)

        try:
            result = transactions.record_expense(
                db,
                form,
                confirmed=_parse_confirmed(confirmed),
                user_id=user.id,
                submission_token=data.submission_token,
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

    # 儲存並記下一筆（UF02）：保留日期、付款方式，其他欄位清空，並換新的送出權杖
    next_values = _blank_values()
    for key in KEPT_AFTER_SAVE:
        next_values[key] = values[key]
    if result.replayed:
        # 同一個權杖已經建立過交易：這次沒有寫入，只告知第一次建立的那筆
        saved = {"transaction_id": result.transaction_id, "replayed": True}
    else:
        saved = {
            "transaction_id": result.transaction_id,
            "replayed": False,
            "account_id": form.account_id,
            "amount": form.amount,
            "notices": result.notices,  # V13 等「提醒」：已經儲存，只顯示不阻擋
        }
    return _form(request, db, next_values, saved=saved)


# --- GET /txn 交易列表與搜尋（P2 Step 3）---------------------------------------------------

SEARCH_FIELDS = ("date_from", "date_to", "merchant", "min_amount", "max_amount")
ADVANCED_SEARCH_FIELDS = ("merchant", "min_amount", "max_amount")


def _is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"


def _page_url(values: dict[str, str], page: int) -> str:
    """換頁連結：保留目前的條件（包含空白值，空白代表不限）。"""
    return "/txn?" + urlencode({**values, "page": page})


@router.get("/txn")
def list_txns(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
) -> Response:
    params = request.query_params
    is_default = not any(key in params for key in SEARCH_FIELDS)
    values = {key: params.get(key, "") for key in SEARCH_FIELDS}
    if is_default:
        first, last = search.month_range(clock.today())
        values["date_from"], values["date_to"] = first.isoformat(), last.isoformat()

    template = "partials/txn_list_body.html" if _is_htmx(request) else "pages/txn_list.html"
    context = {
        "values": values,
        "field_errors": {},
        "advanced_open": any(values[key] for key in ADVANCED_SEARCH_FIELDS),
        "merchant_names": entry_options.merchant_names(db),
        "is_default": is_default,
        "result": None,
        "today": clock.today(),
    }
    try:
        query = parse_search(values)
    except FormParseError as exc:
        context["field_errors"] = {f: [(None, msg)] for f, msg in exc.errors.items()}
        context["advanced_open"] = context["advanced_open"] or any(
            f in exc.errors for f in ADVANCED_SEARCH_FIELDS
        )
        return render(request, template, context, status_code=422)

    result = search.search(db, query.to_filters(), page=parse_page(params.get("page")))
    context["result"] = result
    context["prev_url"] = _page_url(values, result.page - 1) if result.page > 1 else None
    context["next_url"] = (
        _page_url(values, result.page + 1) if result.page < result.page_count else None
    )
    return render(request, template, context)
