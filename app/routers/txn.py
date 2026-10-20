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

P2 Step 8B：標籤
- 新增（支出、收入、轉帳；轉入信用卡的繳卡費也走轉帳表單）與修改（支出、收入、轉帳、繳卡費）
  都有單一標籤文字欄。找不到的名稱先建立並 commit（決策 M2），再交給 P1 寫入關聯。
- 修改：表單送來的標籤字串是完整的一組（空字串 = 移除全部）；POST 沒有 tags 欄位 = 不處理標籤。
- 防重送檢查在建立商家、標籤之前；重送不會留下新的主資料。
- 搜尋：單一標籤；退款的商家、標籤分類跟著原交易目前的值（repositories/ledger.py）。

P2 Step 7：退款（GET/POST /txn/{id}/refund，UF05）
- 原交易 id 只取自網址；欄位只有日期、退款金額、退回方式（付款方式）、備註。
- 預填剩餘可退（只是預填，上限由 P1 V07 判斷）；原交易條件、日期由 P1 判斷。
- 一次性送出權杖同新增；重送時回到「第一次建立的退款」所屬的原交易，不看這次網址的 id。
- 成功後回到原交易明細（顯示已退、剩餘可退、退款連結）。

P2 Step 6：支出的進階欄位（手續費、分期利息、不計入預算），新增與修改都支援；外幣延後。
- 帳務（分錄、交易規模、V10、V12、P12、REFUND_LOCKED）全部由 P1 判斷，Web 只傳值。
- 支出的修改條件改成和 5B 相同的「還原 → builder 重建 → 比對」（txn_edit）。

P2 Step 5B：修改收入、轉帳、繳卡費
- 表單依「原交易」的類型決定（不信任送來的 txn_type）：收入 → 收入表單；轉帳、繳卡費 → 轉帳表單。
  繳卡費一樣送 TransferForm，D9 由 P1 每次重新判斷；類型變了由 P1 回 TXN_TYPE_IMMUTABLE。
- 修改沒有送出權杖；新增的權杖仍必填。
- 選單的「目前值」已停用時仍列出（標示已停用），其他停用值不列（支出也一起適用）。

P2 Step 4A：交易明細、修改（只限快速記帳格式的支出）、軟刪除、還原
- GET /txn/{id}：明細（含已刪除的交易）；找不到 → 404
- GET/POST /txn/{id}/edit：txn_detail.edit_state() 先擋下不能用表單修改的交易，
  再走 transactions.update_transaction()；422／409 與新增相同；成功後回到明細
- POST /txn/{id}/delete、/restore：transactions.delete_transaction()／restore_transaction()；
  P1 擋下時回 422 並在明細頁顯示原因；成功後回到明細
- P3-3C：提醒（V13、V14）與「這筆交易計入的預算卡片」一併顯示；儲存並回首頁、刪除會離開頁面，
  提醒用 app/flash.py 的一次性 cookie 帶到下一頁（首頁、明細頁）顯示一次。
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

P2 Step 10：連續補記「本次已記 N 筆」（UF02）
- 「儲存並記下一筆」成功（含重送）才把 transaction id 加進簽章 Cookie `ledger_run`
  （app/entry_run.py）；422、409、返回修改都不改它。「儲存並回首頁」成功後清掉它，結束這一輪。
- 只有建立表單（/txn/new）在下方顯示；顯示的資料每次從資料庫讀（services/txn_run.py）。
"""

import secrets
from datetime import date
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from starlette.responses import Response

from app import clock, entry_run, flash
from app.db import get_db
from app.deps import require_user
from app.models import User
from app.models.enums import TxnType
from app.schemas.search import parse_page, parse_search
from app.schemas.txn import (
    FormParseError,
    parse_expense,
    parse_income,
    parse_refund,
    parse_transfer,
)
from app.services import (
    audit_history,
    budgets,
    entry_options,
    merchants,
    search,
    transactions,
    txn_detail,
    txn_run,
)
from app.services import tags as tag_service
from app.services.errors import NotFound
from app.services.issues import Issue
from app.services.txn_detail import NOT_EDITABLE
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
    # 進階（P2 Step 6）：手續費、分期利息、不計入預算
    "fee_amount",
    "fee_account_id",
    "interest_amount",
    "interest_account_id",
    "exclude_from_budget",
    "tags",  # P2 Step 8B：單一文字欄，用「、」「,」「，」分隔
)
TOKEN_FIELD = "submission_token"

# --- /txn/new 的交易類型（P2 Step 5A）---
# 欄位、儲存並記下一筆保留的欄位、表單模板、標題集中在這裡
TYPE_EXPENSE, TYPE_INCOME, TYPE_TRANSFER = "expense", "income", "transfer"
TXN_TYPE_CHOICES = ((TYPE_EXPENSE, "支出"), (TYPE_INCOME, "收入"), (TYPE_TRANSFER, "轉帳"))
TYPE_FIELDS = {
    TYPE_EXPENSE: FORM_FIELDS,
    TYPE_INCOME: (
        "txn_date",
        "amount",
        "account_id",
        "receive_account_id",
        "description",
        "tags",
    ),
    TYPE_TRANSFER: (
        "txn_date",
        "amount",
        "from_account_id",
        "to_account_id",
        "fee_amount",
        "fee_account_id",
        "description",
        "tags",
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
# 提款（V1.1）：/txn/new?type=transfer&preset=withdraw，帳務上就是「銀行 → 現金」的轉帳
PRESET_WITHDRAW = "withdraw"
WITHDRAW_HEADING = "記一筆提款"

# --- /txn/{id}/edit 的表單（P2 Step 5B）：依「原交易」類型決定，不信任送來的欄位 ---
EDIT_FORM_TYPE = {
    TxnType.EXPENSE: TYPE_EXPENSE,
    TxnType.INCOME: TYPE_INCOME,
    TxnType.TRANSFER: TYPE_TRANSFER,
    TxnType.CARD_PAYMENT: TYPE_TRANSFER,  # 畫面上是「轉入信用卡的轉帳」
}
EDIT_HEADINGS = {
    TxnType.EXPENSE: "修改支出",
    TxnType.INCOME: "修改收入",
    TxnType.TRANSFER: "修改轉帳",
    TxnType.CARD_PAYMENT: "修改繳卡費",
}
# 修改繳卡費時畫面不顯示手續費：送來的手續費仍交給 P1（TXN_STRUCTURE），錯誤顯示在表單頂端
HIDDEN_ON_CARD_PAYMENT = ("fee_amount", "fee_account_id")
# 有「目前值」的下拉選單欄位（停用中的目前值仍要列出）
SELECT_FIELDS = (
    "account_id",
    "payment_method_id",
    "receive_account_id",
    "from_account_id",
    "to_account_id",
    "fee_account_id",
    "interest_account_id",
)

# 退款表單（P2 Step 7）
REFUND_FIELDS = ("txn_date", "amount", "payment_method_id", "description")

# P1 Issue.field 與畫面欄位名稱不同的對應（只是顯示位置，不是規則）：
# - 商家在畫面上輸入的是名稱
# - 轉帳轉入信用卡時 P1 改走繳卡費（D9），錯誤欄位是 card_account_id，畫面上就是「轉入帳戶」
ISSUE_FIELD_TO_FORM = {
    "merchant_id": "merchant_name",
    "card_account_id": "to_account_id",
    "tag_ids": "tags",  # P2 Step 8B：標籤的 V04／V05 顯示在標籤欄位旁
}

ACTION_NEXT = "next"  # 儲存並記下一筆
ACTION_HOME = "home"  # 儲存並回首頁
ACTION_EDIT = "edit"  # 確認畫面的「返回修改」：不儲存，回到可編輯表單

# 錯誤顯示用：(規則代碼或 None, 訊息)。Web 解析錯誤沒有規則代碼。
ErrorItem = tuple[str | None, str]


def _new_token() -> str:
    return secrets.token_urlsafe(24)


def _options(
    db: Session,
    txn_type: str = TYPE_EXPENSE,
    *,
    edit_type: str | None = None,
    keep: dict[str, int] | None = None,
) -> dict:
    """各類型表單的下拉選單（只列啟用中的末層科目；P1 validation 仍是最終檢查）。

    edit_type：修改時原交易的類型（新增為 None）。keep：修改時各選單的「目前值」，
    停用中也列出（P1 D14 允許沒有變更的分錄繼續使用）。"""
    keep = keep or {}
    if txn_type == TYPE_INCOME:
        return {
            "income_groups": entry_options.income_account_groups(db, keep.get("account_id")),
            "receive_groups": entry_options.balance_sheet_groups(
                db, keep_id=keep.get("receive_account_id")
            ),
            "tag_names": tag_service.active_names(db),
        }
    if txn_type == TYPE_TRANSFER:
        to_keep = keep.get("to_account_id")
        if edit_type == TxnType.CARD_PAYMENT:
            # 轉入只列信用卡：改成非信用卡一定會被 TXN_TYPE_IMMUTABLE 擋下
            to_groups = entry_options.credit_card_groups(db, to_keep)
        elif edit_type == TxnType.TRANSFER:
            # 轉入不列信用卡：改成信用卡會變成繳卡費，一定會被 TXN_TYPE_IMMUTABLE 擋下
            to_groups = entry_options.balance_sheet_groups(
                db, exclude_credit_cards=True, keep_id=to_keep
            )
        else:
            to_groups = entry_options.balance_sheet_groups(db)
        return {
            "from_groups": entry_options.balance_sheet_groups(
                db, exclude_credit_cards=True, keep_id=keep.get("from_account_id")
            ),
            "to_groups": to_groups,
            "fee_groups": entry_options.expense_account_groups(db, keep.get("fee_account_id")),
            "card_payment": edit_type == TxnType.CARD_PAYMENT,
            "tag_names": tag_service.active_names(db),
        }
    methods, default_pm_id = entry_options.payment_method_choices(db, keep.get("payment_method_id"))
    return {
        # 快速記帳（建立）的主科目把常用的排最上面（P2 Step 11）；修改表單、手續費、利息維持原順序
        "account_groups": (
            entry_options.ranked_expense_account_groups(db)
            if edit_type is None
            else entry_options.expense_account_groups(db, keep.get("account_id"))
        ),
        "frequent_label": entry_options.FREQUENT_LABEL,
        "fee_groups": entry_options.expense_account_groups(db, keep.get("fee_account_id")),
        "interest_groups": entry_options.expense_account_groups(
            db, keep.get("interest_account_id")
        ),
        "payment_methods": methods,
        "default_payment_method_id": default_pm_id,
        "merchant_options": entry_options.merchant_options(db),
        "voucher_choices": entry_options.voucher_choices(),
        "tag_names": tag_service.active_names(db),
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
    edit_type: str | None = None,
    keep: dict[str, int] | None = None,
    run_ids: tuple[int, ...] | None = None,
) -> Response:
    """txn_id 不是 None 時是修改表單（/txn/{id}/edit，沒有送出權杖）。

    建立表單會在下方顯示「本次已記 N 筆」（P2 Step 10）：run_ids 是這次回應要顯示的清單，
    沒給就讀 Cookie；修改表單不顯示。"""
    return render(
        request,
        template or FORM_TEMPLATES[txn_type],
        {
            **_options(db, txn_type, edit_type=edit_type, keep=keep),
            **({} if txn_id is not None else _run_context(request, db, run_ids)),
            "values": values,
            "field_errors": field_errors or {},
            "form_errors": form_errors or [],
            "saved": saved,
            "mode": "edit" if txn_id is not None else "create",
            "form_url": f"/txn/{txn_id}/edit" if txn_id is not None else "/txn/new",
            "txn_id": txn_id,
            "txn_type": txn_type,
            "form_template": FORM_TEMPLATES[txn_type],
            "heading": EDIT_HEADINGS.get(edit_type, ""),
        },
        status_code=status_code,
    )


def _run_context(request: Request, db: Session, run_ids: tuple[int, ...] | None = None) -> dict:
    """建立表單下方的「本次已記 N 筆」（P2 Step 10）。沒有這一輪時 run 是 None。"""
    ids = entry_run.read(request) if run_ids is None else run_ids
    if not ids:
        return {"run": None}
    return {"run": txn_run.summarize(db, ids), "today": clock.today()}


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
    # 提款入口（V1.1）：仍是一般轉帳，只換標題、預選現金、展開手續費；未知的 preset 一律忽略
    heading = HEADINGS[txn_type]
    fee_open = False
    if txn_type == TYPE_TRANSFER and params.get("preset") == PRESET_WITHDRAW:
        heading = WITHDRAW_HEADING
        fee_open = True
        cash_id = entry_options.default_cash_account_id(db)
        if cash_id is not None:
            values["to_account_id"] = str(cash_id)
    template = "partials/txn_new_body.html" if _is_htmx(request) else "pages/txn_new.html"
    return render(
        request,
        template,
        {
            **options,
            "fee_open": fee_open,
            "values": values,
            "field_errors": {},
            "form_errors": [],
            "saved": None,
            "mode": "create",
            "form_url": "/txn/new",
            "txn_type": txn_type,
            "txn_type_choices": TXN_TYPE_CHOICES,
            "heading": heading,
            "form_template": FORM_TEMPLATES[txn_type],
            **_run_context(request, db),
        },
    )


async def _has_tags_field(request: Request) -> bool:
    """修改表單有沒有送出 tags 欄位（P2 Step 8B）。

    FastAPI 解析 Form 參數時把空字串視為「沒有值」（fastapi/dependencies/utils.py：
    Form 欄位 value == "" 時當成未提供），所以 `tags: str | None = Form(None)` 分不出
    「欄位存在但清空」與「沒有這個欄位」。Starlette 的 FormData 本身分得出來，
    這裡讀同一份已解析、已快取的 FormData 判斷鍵是否存在。"""
    return "tags" in await request.form()


def _tag_ids(db: Session, text: str | None) -> tuple[int, ...] | None:
    """標籤欄位 → tag_ids。None（表單沒有這個欄位）= 不處理標籤；空字串 = ()。
    找不到的名稱先建立並 commit（決策 M2 的取捨，見 services/tags.py）。"""
    if text is None:
        return None
    return tag_service.resolve_or_create_many(db, tag_service.parse(text))


def _record(
    db: Session, txn_type: str, data, *, confirmed, user_id: int, token: str, tag_text: str
):
    """依類型呼叫 P1 orchestration；帳務規則全部在 P1。
    順序：商家 → 標籤（都是主資料，先 commit）
    → record_*（交易、分錄、標籤關聯、稽核同一次提交）。"""
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
    result = record(
        db,
        form,
        confirmed=confirmed,
        user_id=user_id,
        submission_token=token,
        tag_ids=_tag_ids(db, tag_text),
    )
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
    interest_amount: Annotated[str, Form()] = "",
    interest_account_id: Annotated[str, Form()] = "",
    exclude_from_budget: Annotated[str, Form()] = "",
    tags: Annotated[str, Form()] = "",
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
        "interest_amount": interest_amount,
        "interest_account_id": interest_account_id,
        "exclude_from_budget": exclude_from_budget,
        "tags": tags,
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
                tag_text=values.get("tags", ""),
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
        # 儲存並回首頁：離開連續補記，結束這一輪（P2 Step 10）
        home = Response(status_code=200, headers={"HX-Redirect": "/"})
        entry_run.clear_cookie(home)
        # V13／V14 提醒帶過這次 redirect，首頁顯示一次（P3-3C；不碰 ledger_run）
        flash.set_cookie(home, result.notices, request.app.state.settings)
        return home

    # 儲存並記下一筆：保留這個類型的「日期 + 錢從哪裡來」，其他欄位清空，並換新的送出權杖
    next_values = _blank_values(txn_type)
    for key in KEPT_AFTER_SAVE[txn_type]:
        next_values[key] = values[key]
    cards = () if result.replayed else budgets.cards_for_transaction(db, result.transaction_id)
    if result.replayed:
        # 同一個權杖已經建立過交易：這次沒有寫入，只告知第一次建立的那筆
        saved = {"transaction_id": result.transaction_id, "replayed": True}
    elif txn_type == TYPE_EXPENSE:
        saved = {
            "transaction_id": result.transaction_id,
            "replayed": False,
            "account_id": form.account_id,
            "amount": form.amount,
            "notices": result.notices,  # V13、V14 等「提醒」：已經儲存，只顯示不阻擋
            "budget_cards": cards,
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
            "budget_cards": cards,
        }
    # 交易已經寫入（含重送：第一次的那筆）才加入「本次」；集合語意，重送不會多算，
    # 也能補上第一次回應遺失、Cookie 沒寫到的那筆。422、409、返回修改在上面就已經離開。
    run_ids = entry_run.include(entry_run.read(request), result.transaction_id)
    response = _form(request, db, next_values, txn_type=txn_type, saved=saved, run_ids=run_ids)
    entry_run.set_cookie(response, run_ids, request.app.state.settings)
    return response


# --- GET /txn 交易列表與搜尋（P2 Step 3）---------------------------------------------------

SEARCH_FIELDS = ("date_from", "date_to", "merchant", "tag", "min_amount", "max_amount")
ADVANCED_SEARCH_FIELDS = ("merchant", "tag", "min_amount", "max_amount")


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
        "merchant_options": entry_options.merchant_options(db),
        "tag_names": tag_service.active_names(db),
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
    budget_cards: tuple[budgets.BudgetCard, ...] = (),
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
            "budget_cards": budget_cards,
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
    # 刪除後 redirect 過來：顯示一次 flash 提醒（V14），重新整理就不再出現
    response = _detail(request, db, txn_id, notices=flash.read(request))
    flash.pop(request, response)
    return response


def _keep(values: dict[str, str] | None) -> dict[str, int]:
    """原交易各選單的目前值（停用中也要列出）。只取原交易，不取送來的值。"""
    if not values:
        return {}
    return {k: int(v) for k, v in values.items() if k in SELECT_FIELDS and v.isdigit()}


@router.get("/txn/{txn_id:int}/edit")
def edit_txn_page(
    request: Request,
    txn_id: int,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
) -> Response:
    try:
        state = txn_detail.edit_state(db, txn_id)
    except NotFound:
        return _not_found(request, txn_id)
    if state.blocker is not None:
        # 不能用表單修改：回到明細頁（明細會說明原因）
        return RedirectResponse(f"/txn/{txn_id}", status_code=303)
    return _form(
        request,
        db,
        state.values,
        txn_id=txn_id,
        template="pages/txn_edit.html",
        txn_type=EDIT_FORM_TYPE[state.txn_type],
        edit_type=state.txn_type,
        keep=_keep(state.values),
    )


def _update_form(db: Session, form_type: str, data):
    """修改用的 P1 Form。支出的商家名稱換成 merchant_id；
    收入、轉帳直接轉換（繳卡費也是 TransferForm）。"""
    if form_type == TYPE_EXPENSE:
        merchant_id = None
        if data.merchant_name is not None:
            merchant_id = merchants.resolve_or_create(db, data.merchant_name).id
        return data.to_form(merchant_id=merchant_id)
    return data.to_form()


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
    receive_account_id: Annotated[str, Form()] = "",
    from_account_id: Annotated[str, Form()] = "",
    to_account_id: Annotated[str, Form()] = "",
    fee_amount: Annotated[str, Form()] = "",
    fee_account_id: Annotated[str, Form()] = "",
    interest_amount: Annotated[str, Form()] = "",
    interest_account_id: Annotated[str, Form()] = "",
    exclude_from_budget: Annotated[str, Form()] = "",
    tags: Annotated[str | None, Form()] = None,
    tags_in_form: Annotated[bool, Depends(_has_tags_field)] = False,
    confirmed: Annotated[str, Form()] = "",
    action: Annotated[str, Form()] = "save",
) -> Response:
    # 伺服器端先檢查：找不到 → 404；不能用表單修改 → 422（不建立商家、不寫入、不新增稽核）
    try:
        state = txn_detail.edit_state(db, txn_id)
    except NotFound:
        return _not_found(request, txn_id)

    # 表單依「原交易」類型決定；送來的 txn_type 一律不看
    edit_type = state.txn_type if state.txn_type in EDIT_FORM_TYPE else TxnType.EXPENSE
    form_type = EDIT_FORM_TYPE[edit_type]
    fields = TYPE_FIELDS[form_type]
    shown = fields
    if edit_type == TxnType.CARD_PAYMENT:
        shown = tuple(f for f in fields if f not in HIDDEN_ON_CARD_PAYMENT)
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
        "interest_amount": interest_amount,
        "interest_account_id": interest_account_id,
        "exclude_from_budget": exclude_from_budget,
        # 表單沒有標籤欄位時不處理標籤；畫面上顯示目前的標籤
        "tags": (tags or "") if tags_in_form else (state.values or {}).get("tags", ""),
    }
    values = {name: submitted[name] for name in fields}
    # 有標籤欄位 → 完整一組（空字串 = 移除全部）；沒有 → None，不處理標籤（見 _has_tags_field）
    tag_text = (tags or "") if tags_in_form else None

    def form_response(**kwargs) -> Response:
        return _form(
            request,
            db,
            values,
            txn_id=txn_id,
            txn_type=form_type,
            edit_type=edit_type,
            keep=_keep(state.values),
            **kwargs,
        )

    if state.blocker is not None:
        return form_response(status_code=422, form_errors=[(NOT_EDITABLE, state.blocker)])

    if action == ACTION_EDIT:
        return form_response()

    try:
        data = _PARSERS[form_type](values, require_submission_token=False)
    except FormParseError as exc:
        field_errors = {f: [(None, msg)] for f, msg in exc.errors.items() if f in shown}
        form_errors = [(None, msg) for f, msg in exc.errors.items() if f not in shown]
        return form_response(status_code=422, field_errors=field_errors, form_errors=form_errors)

    form = _update_form(db, form_type, data)
    tag_ids = _tag_ids(db, tag_text)  # 商家之後、寫入之前；None = 這次不處理標籤
    try:
        result = transactions.update_transaction(
            db,
            txn_id,
            form,
            confirmed=_parse_confirmed(confirmed),
            user_id=user.id,
            tag_ids=tag_ids,
        )
    except NotFound:
        return _not_found(request, txn_id)
    except ValidationFailed as exc:
        field_errors, form_errors = _split_issues(exc.result.errors, shown)
        return form_response(status_code=422, field_errors=field_errors, form_errors=form_errors)
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
        budget_cards=budgets.cards_for_transaction(db, result.transaction_id),
        done_message="已儲存修改。",
        htmx_template="partials/txn_detail_main.html",
        headers={
            "HX-Retarget": "main.container",
            "HX-Reswap": "innerHTML",
            "HX-Push-Url": f"/txn/{txn_id}",
        },
    )


def _refund_form(
    request: Request,
    db: Session,
    state,
    values: dict[str, str],
    *,
    status_code: int = 200,
    field_errors: dict[str, list[ErrorItem]] | None = None,
    form_errors: list[ErrorItem] | None = None,
    template: str = "partials/txn_refund_form.html",
) -> Response:
    """退回方式只列啟用中的付款方式（退款是新交易，停用的付款方式由 P1 V04 擋下）。"""
    methods, _ = entry_options.payment_method_choices(db)
    return render(
        request,
        template,
        {
            "refund": state,
            "payment_methods": methods,
            "values": values,
            "field_errors": field_errors or {},
            "form_errors": form_errors or [],
            "saved": None,
            "form_url": f"/txn/{state.original_id}/refund",
        },
        status_code=status_code,
    )


def _after_refund(
    request: Request, db: Session, refund_id: int, notices, message: str, *, cards=()
):
    """回到退款所屬的原交易明細。原交易 id 從實際存下（或重送命中）的退款讀回，不看網址。"""
    refund = txn_detail.get_detail(db, refund_id)
    original_id = refund.related_txn_id
    return _detail(
        request,
        db,
        original_id,
        notices=notices,
        budget_cards=cards,
        done_message=message,
        htmx_template="partials/txn_detail_main.html",
        headers={
            "HX-Retarget": "main.container",
            "HX-Reswap": "innerHTML",
            "HX-Push-Url": f"/txn/{original_id}",
        },
    )


@router.get("/txn/{txn_id:int}/refund")
def refund_page(
    request: Request,
    txn_id: int,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
) -> Response:
    try:
        state = txn_detail.refund_state(db, txn_id)
    except NotFound:
        return _not_found(request, txn_id)
    if state.blocker is not None:
        # 不能退款：回到明細頁（明細會說明原因）
        return RedirectResponse(f"/txn/{txn_id}", status_code=303)
    values = {
        "txn_date": clock.today().isoformat(),
        "amount": str(state.remaining),
        "payment_method_id": _text_id(state.default_payment_method_id),
        "description": "",
        TOKEN_FIELD: _new_token(),
    }
    return _refund_form(request, db, state, values, template="pages/txn_refund.html")


def _text_id(value: int | None) -> str:
    return "" if value is None else str(value)


@router.post("/txn/{txn_id:int}/refund")
def create_refund(
    request: Request,
    txn_id: int,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    txn_date: Annotated[str, Form()] = "",
    amount: Annotated[str, Form()] = "",
    payment_method_id: Annotated[str, Form()] = "",
    description: Annotated[str, Form()] = "",
    submission_token: Annotated[str, Form()] = "",
    confirmed: Annotated[str, Form()] = "",
    action: Annotated[str, Form()] = "save",
) -> Response:
    try:
        state = txn_detail.refund_state(db, txn_id)
    except NotFound:
        return _not_found(request, txn_id)
    values = {
        "txn_date": txn_date,
        "amount": amount,
        "payment_method_id": payment_method_id,
        "description": description,
        TOKEN_FIELD: submission_token,
    }

    def form_response(**kwargs) -> Response:
        return _refund_form(request, db, state, values, **kwargs)

    if action == ACTION_EDIT:
        return form_response()

    try:
        data = parse_refund(values)
    except FormParseError as exc:
        if TOKEN_FIELD in exc.errors:
            values[TOKEN_FIELD] = _new_token()
        field_errors = {f: [(None, m)] for f, m in exc.errors.items() if f in REFUND_FIELDS}
        form_errors = [(None, m) for f, m in exc.errors.items() if f not in REFUND_FIELDS]
        return form_response(status_code=422, field_errors=field_errors, form_errors=form_errors)

    # 已用過的權杖：回傳第一次的退款，不驗證、不寫入；回到「那筆退款」的原交易
    replay = transactions.find_replay(db, data.submission_token)
    if replay is not None:
        return _after_refund(
            request, db, replay.transaction_id, (), "這筆退款已經儲存過，沒有重複建立。"
        )
    try:
        result = transactions.record_refund(
            db,
            data.to_form(original_txn_id=txn_id),
            confirmed=_parse_confirmed(confirmed),
            user_id=user.id,
            submission_token=data.submission_token,
        )
    except ValidationFailed as exc:
        field_errors, form_errors = _split_issues(exc.result.errors, REFUND_FIELDS)
        return form_response(status_code=422, field_errors=field_errors, form_errors=form_errors)
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
                "form_url": f"/txn/{txn_id}/refund",
            },
            status_code=409,
        )
    if result.replayed:
        return _after_refund(
            request, db, result.transaction_id, (), "這筆退款已經儲存過，沒有重複建立。"
        )
    refund = txn_detail.get_detail(db, result.transaction_id)
    return _after_refund(
        request,
        db,
        result.transaction_id,
        result.notices,
        f"已退款 {refund.magnitude:,} 元。",
        cards=budgets.cards_for_transaction(db, result.transaction_id),
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
        result = transactions.delete_transaction(db, txn_id, user_id=user.id)
    except NotFound:
        return _not_found(request, txn_id)
    except ValidationFailed as exc:
        return _detail(request, db, txn_id, status_code=422, form_errors=_lifecycle_errors(exc))
    response = _redirect(request, f"/txn/{txn_id}")
    # 刪除退款可能讓預算跨過門檻（V14）：提醒帶過 redirect，明細頁顯示一次
    flash.set_cookie(response, result.notices, request.app.state.settings)
    return response


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
    return _detail(
        request,
        db,
        txn_id,
        notices=result.notices,
        budget_cards=budgets.cards_for_transaction(db, result.transaction_id),
        done_message="已還原。",
    )


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
