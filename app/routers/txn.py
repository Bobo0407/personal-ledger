"""/txn 交易列表與搜尋、/txn/new 快速記帳。

/txn/new（P2 Step 1：只支援 EXPENSE；規格 7.3、7.4、UF01、UF02、F15、F16）。

Router 只做：驗證登入、解析表單型態、呼叫 transactions.record_expense()、依結果選擇回應。
借貸、驗證規則、寫入全部在 P1 的 transactions／validation／LedgerService。

POST 只支援 HTMX（V1 不要求無 JS 版本），回應一律是 partials/ 片段：
- 200：已儲存（儲存並記下一筆），或「返回修改」重新顯示表單
- 200 + HX-Redirect：已儲存（儲存並回首頁）
- 422：表單解析失敗（Web 欄位錯誤）或 P1 ValidationFailed（規格 9.3）
- 409：P1 ConfirmationRequired，回傳確認畫面；按「仍要儲存」帶 confirmed 重送（規格 9.3、T19）

P2 Step 5A：/txn/new 的類型切換（支出／收入／轉帳），只有新增
- GET /txn/new?type=expense|income|transfer（預設 expense；不合法的值一律當成 expense）。
  切換類型只帶日期與金額；新表單拿到新的送出權杖。
- POST /txn/new 依隱藏欄位 txn_type 分派；不合法的 txn_type 直接 422，不會呼叫 orchestration。
  三種類型都用同一套一次性送出權杖（transactions._post_once）。
- 轉帳轉入信用卡時由 P1 自動記成繳卡費（D9）；成功訊息讀回實際存下的交易類型。

P2 Step 4A：交易明細、修改（只限快速記帳格式的支出）、軟刪除、還原
- GET /txn/{id}：明細（含已刪除的交易）；找不到 → 404
- GET/POST /txn/{id}/edit：txn_detail.require_quick_entry_expense() 先擋下不能用表單修改的交易，
  再走 transactions.update_transaction()；422／409 與新增相同；成功後回到明細
- POST /txn/{id}/delete、/restore：transactions.delete_transaction()／restore_transaction()；
  P1 擋下時回 422 並在明細頁顯示原因；成功後回到明細
- 修改、還原成功時，P1 會回傳提醒（PostResult.notices，例如 V13）：不導頁，
  直接回傳明細內容並顯示提醒（修改用 HX-Retarget 換掉整個 main、HX-Push-Url 改網址）
- NotFound 只在這個 router 對應成 404，不做全域框架

P2 Step 4B：異動紀錄與已刪除交易（只讀）
- 明細頁底部顯示 services/audit_history.py 產生的可讀摘要（預設收合）
- GET /txn/deleted：已刪除交易的唯讀列表，不經過 active_postings()、沒有金額合計

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
from datetime import date
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from starlette.responses import Response

from app import clock
from app.db import get_db
from app.deps import require_user
from app.models import User
from app.schemas.search import parse_page, parse_search
from app.schemas.txn import FormParseError, parse_expense, parse_income, parse_transfer
from app.services import (
    audit_history,
    entry_options,
    merchants,
    search,
    transactions,
    txn_detail,
)
from app.services.errors import NotFound, RuleViolation
from app.services.issues import Issue
from app.services.validation import ConfirmationRequired, ValidationFailed
from app.templating import render

router = APIRouter()

# 畫面上的欄位（錯誤可以顯示在欄位旁）。FORM_FIELDS 是支出表單（新增與修改共用）。
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

# --- /txn/new 的交易類型（P2 Step 5A）---
# 欄位、儲存並記下一筆保留的欄位、表單模板、標題集中在這裡
TYPE_EXPENSE, TYPE_INCOME, TYPE_TRANSFER = "expense", "income", "transfer"
TXN_TYPE_CHOICES = ((TYPE_EXPENSE, "支出"), (TYPE_INCOME, "收入"), (TYPE_TRANSFER, "轉帳"))
TYPE_FIELDS = {
    TYPE_EXPENSE: FORM_FIELDS,
    TYPE_INCOME: ("txn_date", "amount", "account_id", "receive_account_id", "description"),
    TYPE_TRANSFER: (
        "txn_date",
        "amount",
        "from_account_id",
        "to_account_id",
        "fee_amount",
        "fee_account_id",
        "description",
    ),
}
# 「儲存並記下一筆」保留的欄位（UF02 的精神：日期 + 錢從哪裡來）；其他欄位清空，並換新的送出權杖
KEPT_AFTER_SAVE = {
    TYPE_EXPENSE: ("txn_date", "payment_method_id"),
    TYPE_INCOME: ("txn_date", "receive_account_id"),
    TYPE_TRANSFER: ("txn_date", "from_account_id"),
}
# 切換類型時保留的欄位
KEPT_ON_SWITCH = ("txn_date", "amount")
FORM_TEMPLATES = {
    TYPE_EXPENSE: "partials/txn_form.html",
    TYPE_INCOME: "partials/txn_form_income.html",
    TYPE_TRANSFER: "partials/txn_form_transfer.html",
}
HEADINGS = {TYPE_EXPENSE: "記一筆支出", TYPE_INCOME: "記一筆收入", TYPE_TRANSFER: "記一筆轉帳"}

# P1 Issue.field 與畫面欄位名稱不同的對應（只是顯示位置，不是規則）：
# - 商家在畫面上輸入的是名稱
# - 轉帳轉入信用卡時 P1 改走繳卡費（D9），錯誤欄位是 card_account_id，畫面上就是「轉入帳戶」
ISSUE_FIELD_TO_FORM = {"merchant_id": "merchant_name", "card_account_id": "to_account_id"}

ACTION_NEXT = "next"  # 儲存並記下一筆
ACTION_HOME = "home"  # 儲存並回首頁
ACTION_EDIT = "edit"  # 確認畫面的「返回修改」：不儲存，回到可編輯表單

# 錯誤顯示用：(規則代碼或 None, 訊息)。Web 解析錯誤沒有規則代碼。
ErrorItem = tuple[str | None, str]


def _new_token() -> str:
    return secrets.token_urlsafe(24)


def _options(db: Session, txn_type: str = TYPE_EXPENSE) -> dict:
    """各類型表單的下拉選單（只列啟用中的末層科目；P1 validation 仍是最終檢查）。"""
    if txn_type == TYPE_INCOME:
        return {
            "income_groups": entry_options.income_account_groups(db),
            "receive_groups": entry_options.balance_sheet_groups(db),
        }
    if txn_type == TYPE_TRANSFER:
        return {
            "from_groups": entry_options.balance_sheet_groups(db, exclude_credit_cards=True),
            "to_groups": entry_options.balance_sheet_groups(db),
            "fee_groups": entry_options.expense_account_groups(db),
        }
    methods, default_pm_id = entry_options.payment_method_choices(db)
    return {
        "account_groups": entry_options.expense_account_groups(db),
        "payment_methods": methods,
        "default_payment_method_id": default_pm_id,
        "merchant_names": entry_options.merchant_names(db),
        "voucher_choices": entry_options.voucher_choices(),
    }


def _blank_values(txn_type: str = TYPE_EXPENSE) -> dict[str, str]:
    values = dict.fromkeys(TYPE_FIELDS[txn_type], "")
    values[TOKEN_FIELD] = _new_token()
    return values


def _initial_values(txn_type: str, default_pm_id: int | None) -> dict[str, str]:
    values = _blank_values(txn_type)
    values["txn_date"] = clock.today().isoformat()
    if txn_type == TYPE_EXPENSE:
        # 預設付款方式只屬於支出（P11）；收入、轉帳不設預設帳戶
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
    txn_id: int | None = None,
    template: str | None = None,
    txn_type: str = TYPE_EXPENSE,
) -> Response:
    """txn_id 不是 None 時是修改表單（/txn/{id}/edit，只有支出，沒有送出權杖）。"""
    return render(
        request,
        template or FORM_TEMPLATES[txn_type],
        {
            **_options(db, txn_type),
            "values": values,
            "field_errors": field_errors or {},
            "form_errors": form_errors or [],
            "saved": saved,
            "mode": "edit" if txn_id is not None else "create",
            "form_url": f"/txn/{txn_id}/edit" if txn_id is not None else "/txn/new",
            "txn_id": txn_id,
            "txn_type": txn_type,
        },
        status_code=status_code,
    )


def _split_issues(
    issues: tuple[Issue, ...], fields: tuple[str, ...] = FORM_FIELDS
) -> tuple[dict[str, list[ErrorItem]], list[ErrorItem]]:
    """有對應欄位的錯誤放在欄位旁；field 為 None 或畫面上沒有的欄位（例如分錄層級的
    V03、V04、TXN_STRUCTURE）放在表單頂端，不能默默吞掉。"""
    by_field: dict[str, list[ErrorItem]] = {}
    top: list[ErrorItem] = []
    for issue in issues:
        item = (issue.code, issue.message)
        field = ISSUE_FIELD_TO_FORM.get(issue.field, issue.field)
        if field in fields:
            by_field.setdefault(field, []).append(item)
        else:
            top.append(item)
    return by_field, top


def _parse_confirmed(value: str) -> tuple[str, ...]:
    return tuple(code for code in (c.strip() for c in value.split(",")) if code)


def _txn_type(value: str | None) -> str:
    """GET 的 ?type=：只接受三種類型，其他一律當成支出。"""
    return value if value in TYPE_FIELDS else TYPE_EXPENSE


@router.get("/txn/new")
def new_txn(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
) -> Response:
    params = request.query_params
    txn_type = _txn_type(params.get("type"))
    options = _options(db, txn_type)
    values = _initial_values(txn_type, options.get("default_payment_method_id"))
    # 類型切換（HTMX hx-include）帶來的日期與金額；日期格式不對就維持今天
    if params.get("amount"):
        values["amount"] = params["amount"]
    if params.get("txn_date"):
        try:
            values["txn_date"] = date.fromisoformat(params["txn_date"]).isoformat()
        except ValueError:
            pass
    template = "partials/txn_new_body.html" if _is_htmx(request) else "pages/txn_new.html"
    return render(
        request,
        template,
        {
            **options,
            "values": values,
            "field_errors": {},
            "form_errors": [],
            "saved": None,
            "mode": "create",
            "form_url": "/txn/new",
            "txn_type": txn_type,
            "txn_type_choices": TXN_TYPE_CHOICES,
            "heading": HEADINGS[txn_type],
            "form_template": FORM_TEMPLATES[txn_type],
        },
    )


def _record(db: Session, txn_type: str, data, *, confirmed, user_id: int, token: str):
    """依類型呼叫 P1 orchestration；帳務規則全部在 P1。"""
    if txn_type == TYPE_EXPENSE:
        merchant_id = None
        if data.merchant_name is not None:
            merchant_id = merchants.resolve_or_create(db, data.merchant_name).id
        form = data.to_form(merchant_id=merchant_id)
        record = transactions.record_expense
    elif txn_type == TYPE_INCOME:
        form, record = data.to_form(), transactions.record_income
    else:
        form, record = data.to_form(), transactions.record_transfer
    result = record(db, form, confirmed=confirmed, user_id=user_id, submission_token=token)
    return result, form


_PARSERS = {TYPE_EXPENSE: parse_expense, TYPE_INCOME: parse_income, TYPE_TRANSFER: parse_transfer}


@router.post("/txn/new")
def create_txn(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    txn_type: Annotated[str, Form()] = TYPE_EXPENSE,
    txn_date: Annotated[str, Form()] = "",
    amount: Annotated[str, Form()] = "",
    account_id: Annotated[str, Form()] = "",
    payment_method_id: Annotated[str, Form()] = "",
    description: Annotated[str, Form()] = "",
    merchant_name: Annotated[str, Form()] = "",
    voucher_type: Annotated[str, Form()] = "",
    invoice_no: Annotated[str, Form()] = "",
    receive_account_id: Annotated[str, Form()] = "",
    from_account_id: Annotated[str, Form()] = "",
    to_account_id: Annotated[str, Form()] = "",
    fee_amount: Annotated[str, Form()] = "",
    fee_account_id: Annotated[str, Form()] = "",
    submission_token: Annotated[str, Form()] = "",
    confirmed: Annotated[str, Form()] = "",
    action: Annotated[str, Form()] = ACTION_NEXT,
) -> Response:
    if txn_type not in TYPE_FIELDS:
        # 表單一定帶合法的 txn_type；不合法代表表單被竄改或過期：不呼叫 orchestration
        return _form(
            request,
            db,
            _initial_values(TYPE_EXPENSE, None),
            status_code=422,
            form_errors=[(None, "表單已失效，請重新整理後再試。")],
        )

    submitted = {
        "txn_date": txn_date,
        "amount": amount,
        "account_id": account_id,
        "payment_method_id": payment_method_id,
        "description": description,
        "merchant_name": merchant_name,
        "voucher_type": voucher_type,
        "invoice_no": invoice_no,
        "receive_account_id": receive_account_id,
        "from_account_id": from_account_id,
        "to_account_id": to_account_id,
        "fee_amount": fee_amount,
        "fee_account_id": fee_account_id,
    }
    fields = TYPE_FIELDS[txn_type]
    values = {name: submitted[name] for name in fields}
    values[TOKEN_FIELD] = submission_token

    def form_response(**kwargs) -> Response:
        return _form(request, db, values, txn_type=txn_type, **kwargs)

    if action == ACTION_EDIT:
        return form_response()
    if action != ACTION_HOME:
        action = ACTION_NEXT

    try:
        data = _PARSERS[txn_type](values)
    except FormParseError as exc:
        if TOKEN_FIELD in exc.errors:
            # 權杖缺少或格式不對：這份表單不能再用；換一個新權杖讓使用者重新送出
            values[TOKEN_FIELD] = _new_token()
        field_errors = {f: [(None, msg)] for f, msg in exc.errors.items() if f in fields}
        form_errors = [(None, msg) for f, msg in exc.errors.items() if f not in fields]
        return form_response(status_code=422, field_errors=field_errors, form_errors=form_errors)

    # 已用過的權杖：直接回傳第一次的結果，不建立商家、不驗證、不寫入（重送不得有任何副作用）
    form = None
    result = transactions.find_replay(db, data.submission_token)
    if result is None:
        try:
            result, form = _record(
                db,
                txn_type,
                data,
                confirmed=_parse_confirmed(confirmed),
                user_id=user.id,
                token=data.submission_token,
            )
        except ValidationFailed as exc:
            field_errors, form_errors = _split_issues(exc.result.errors, fields)
            return form_response(
                status_code=422, field_errors=field_errors, form_errors=form_errors
            )
        except ConfirmationRequired as exc:
            # 確認畫面只帶「這次觸發」的代碼，加上之前已確認的；內容不能在確認畫面修改，
            # 要修改就按「返回修改」回到表單（重新送出時會重新檢查、重新確認）。
            codes = dict.fromkeys((*_parse_confirmed(confirmed), *(i.code for i in exc.pending)))
            return render(
                request,
                "partials/txn_confirm.html",
                {
                    "values": {"txn_type": txn_type, **values},
                    "pending": exc.pending,
                    "confirmed": ",".join(codes),
                    "action": action,
                },
                status_code=409,
            )

    if action == ACTION_HOME:
        return Response(status_code=200, headers={"HX-Redirect": "/"})

    # 儲存並記下一筆：保留這個類型的「日期 + 錢從哪裡來」，其他欄位清空，並換新的送出權杖
    next_values = _blank_values(txn_type)
    for key in KEPT_AFTER_SAVE[txn_type]:
        next_values[key] = values[key]
    if result.replayed:
        # 同一個權杖已經建立過交易：這次沒有寫入，只告知第一次建立的那筆
        saved = {"transaction_id": result.transaction_id, "replayed": True}
    elif txn_type == TYPE_EXPENSE:
        saved = {
            "transaction_id": result.transaction_id,
            "replayed": False,
            "account_id": form.account_id,
            "amount": form.amount,
            "notices": result.notices,  # V13 等「提醒」：已經儲存，只顯示不阻擋
        }
    else:
        # 讀回實際存下的交易：轉入信用卡時 P1 已記成繳卡費（D9），Web 不自己判斷
        detail = txn_detail.get_detail(db, result.transaction_id)
        saved = {
            "transaction_id": result.transaction_id,
            "replayed": False,
            "label": detail.type_label,
            "amount": detail.magnitude,
            "notices": result.notices,
        }
    return _form(request, db, next_values, txn_type=txn_type, saved=saved)


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


# --- 交易明細、修改、刪除、還原（P2 Step 4A）-------------------------------------------------


def _redirect(request: Request, url: str) -> Response:
    """HTMX 請求用 HX-Redirect 整頁導向；一般請求用 303。"""
    if _is_htmx(request):
        return Response(status_code=200, headers={"HX-Redirect": url})
    return RedirectResponse(url, status_code=303)


def _not_found(request: Request, txn_id: int) -> Response:
    if _is_htmx(request):
        # HTMX 不 swap 404；整頁導向明細網址，由 GET 顯示 404 頁
        return Response(status_code=404, headers={"HX-Redirect": f"/txn/{txn_id}"})
    return render(request, "pages/txn_not_found.html", {}, status_code=404)


def _detail(
    request: Request,
    db: Session,
    txn_id: int,
    *,
    status_code: int = 200,
    form_errors: list[ErrorItem] | None = None,
    notices: tuple[Issue, ...] = (),
    done_message: str | None = None,
    htmx_template: str = "partials/txn_detail_body.html",
    headers: dict[str, str] | None = None,
) -> Response:
    try:
        detail = txn_detail.get_detail(db, txn_id)
    except NotFound:
        return _not_found(request, txn_id)
    template = htmx_template if _is_htmx(request) else "pages/txn_detail.html"
    response = render(
        request,
        template,
        {
            "detail": detail,
            "history": audit_history.history(db, txn_id),
            "form_errors": form_errors or [],
            "notices": notices,
            "done_message": done_message,
            # 只有「修改成功、換掉整個 main」的 HTMX 回應需要一併換掉頁面標題
            "replace_title": template == "partials/txn_detail_main.html",
        },
        status_code=status_code,
    )
    if _is_htmx(request) and headers:
        response.headers.update(headers)
    return response


@router.get("/txn/{txn_id:int}")
def show_txn(
    request: Request,
    txn_id: int,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
) -> Response:
    return _detail(request, db, txn_id)


@router.get("/txn/{txn_id:int}/edit")
def edit_txn_page(
    request: Request,
    txn_id: int,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
) -> Response:
    try:
        values = txn_detail.edit_values(db, txn_id)
    except NotFound:
        return _not_found(request, txn_id)
    except RuleViolation:
        # 不能用快速記帳表單修改：回到明細頁（明細會說明原因）
        return RedirectResponse(f"/txn/{txn_id}", status_code=303)
    return _form(request, db, values, txn_id=txn_id, template="pages/txn_edit.html")


@router.post("/txn/{txn_id:int}/edit")
def edit_txn(
    request: Request,
    txn_id: int,
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
    confirmed: Annotated[str, Form()] = "",
    action: Annotated[str, Form()] = "save",
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
    }
    # 伺服器端再檢查一次：不是快速記帳格式的支出，直接 POST 也不能修改（不建立商家、不寫入）
    try:
        txn_detail.require_quick_entry_expense(db, txn_id)
    except NotFound:
        return _not_found(request, txn_id)
    except RuleViolation as exc:
        return _form(
            request,
            db,
            values,
            txn_id=txn_id,
            status_code=422,
            form_errors=[(exc.code, exc.message)],
        )

    if action == ACTION_EDIT:
        return _form(request, db, values, txn_id=txn_id)

    try:
        data = parse_expense(values, require_submission_token=False)
    except FormParseError as exc:
        field_errors = {f: [(None, msg)] for f, msg in exc.errors.items() if f in FORM_FIELDS}
        form_errors = [(None, msg) for f, msg in exc.errors.items() if f not in FORM_FIELDS]
        return _form(
            request,
            db,
            values,
            txn_id=txn_id,
            status_code=422,
            field_errors=field_errors,
            form_errors=form_errors,
        )

    merchant_id = None
    if data.merchant_name is not None:
        merchant_id = merchants.resolve_or_create(db, data.merchant_name).id
    form = data.to_form(merchant_id=merchant_id)

    try:
        result = transactions.update_transaction(
            db, txn_id, form, confirmed=_parse_confirmed(confirmed), user_id=user.id
        )
    except NotFound:
        return _not_found(request, txn_id)
    except ValidationFailed as exc:
        field_errors, form_errors = _split_issues(exc.result.errors)
        return _form(
            request,
            db,
            values,
            txn_id=txn_id,
            status_code=422,
            field_errors=field_errors,
            form_errors=form_errors,
        )
    except ConfirmationRequired as exc:
        codes = dict.fromkeys((*_parse_confirmed(confirmed), *(i.code for i in exc.pending)))
        return render(
            request,
            "partials/txn_confirm.html",
            {
                "values": values,
                "pending": exc.pending,
                "confirmed": ",".join(codes),
                "action": "save",
                "form_url": f"/txn/{txn_id}/edit",
            },
            status_code=409,
        )
    # 修改頁的 HTMX 目標是 #txn-area；改成換掉整個 main 並把網址換成明細，才能顯示提醒
    return _detail(
        request,
        db,
        txn_id,
        notices=result.notices,
        done_message="已儲存修改。",
        htmx_template="partials/txn_detail_main.html",
        headers={
            "HX-Retarget": "main.container",
            "HX-Reswap": "innerHTML",
            "HX-Push-Url": f"/txn/{txn_id}",
        },
    )


def _lifecycle_errors(exc: ValidationFailed) -> list[ErrorItem]:
    return [(issue.code, issue.message) for issue in exc.result.errors]


@router.post("/txn/{txn_id:int}/delete")
def delete_txn(
    request: Request,
    txn_id: int,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
) -> Response:
    """軟刪除（只設 deleted_at）。已有有效退款的支出由 P1 回 REFUND_EXISTS。"""
    try:
        transactions.delete_transaction(db, txn_id, user_id=user.id)
    except NotFound:
        return _not_found(request, txn_id)
    except ValidationFailed as exc:
        return _detail(request, db, txn_id, status_code=422, form_errors=_lifecycle_errors(exc))
    return _redirect(request, f"/txn/{txn_id}")


@router.post("/txn/{txn_id:int}/restore")
def restore_txn(
    request: Request,
    txn_id: int,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
) -> Response:
    """還原：P1 validate_restore() 重新檢查 V02、D12、
    退款（REFUND_ORIGINAL／REFUND_DATE／V07）、V08；不重跑 V10／V11／V12，所以不需要確認。"""
    try:
        result = transactions.restore_transaction(db, txn_id, user_id=user.id)
    except NotFound:
        return _not_found(request, txn_id)
    except ValidationFailed as exc:
        return _detail(request, db, txn_id, status_code=422, form_errors=_lifecycle_errors(exc))
    # 還原表單的 HTMX 目標就是 #txn-detail（網址已經是 /txn/{id}）：直接回傳明細並顯示提醒
    return _detail(request, db, txn_id, notices=result.notices, done_message="已還原。")


@router.get("/txn/deleted")
def deleted_txns(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
) -> Response:
    """已刪除交易的唯讀列表（最近刪除的在前）；每列連到既有的已刪除明細，在那裡還原。"""
    result = search.deleted_page(db, page=parse_page(request.query_params.get("page")))
    return render(
        request,
        "pages/txn_deleted.html",
        {
            "result": result,
            "today": clock.today(),
            "prev_url": f"/txn/deleted?page={result.page - 1}" if result.page > 1 else None,
            "next_url": (
                f"/txn/deleted?page={result.page + 1}" if result.page < result.page_count else None
            ),
        },
    )
