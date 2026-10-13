"""快速記帳表單的型態解析（規格 7.4：POST /txn/new 收到表單，Pydantic 驗證型態）。

這裡只回答「這個輸入能不能解析」，不回答「解析後合不合法」：
- 空白欄位轉成 None，交給 P1 判斷必填（V05）。
- 整數、日期無法解析時丟 FormParseError，訊息是 Web 層的欄位錯誤，
  不使用 P1 的規則代碼，也不新增業務規則。
- 解析成功後的所有業務規則（金額 > 0 的 V01、必填的 V05、V03、V04……）
  都由 P1 的 transactions／validation 負責。

P2 Step 2：
- merchant_name：只去頭尾空白；名稱比對與建立由 services/merchants.py 負責。
- voucher_type：只接受 VoucherType 的值或空白。
- invoice_no：去頭尾空白、轉大寫，並且只在「2 個英文字母-8 位數字」時移除中間那一個連字號
  （AB-12345678 → AB12345678）。其他任何格式原樣交給 P1，由 V08 判斷格式，Web 不另訂格式規則。
- submission_token：一次性送出權杖，必填；缺少或格式不對 → 表單已失效（422）。
"""

import re
from datetime import date
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict, ValidationError

from app.models.enums import VoucherType
from app.services.entries import ExpenseForm, IncomeForm, TransferForm

_INT = re.compile(r"[+-]?[0-9]+")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_INVOICE_WITH_HYPHEN = re.compile(r"([A-Z]{2})-([0-9]{8})")
_TOKEN = re.compile(r"[A-Za-z0-9_-]{16,128}")
_VOUCHER_TYPES = frozenset(v.value for v in VoucherType)

# 每個欄位解析失敗時顯示的訊息
PARSE_MESSAGES = {
    "txn_date": "日期格式不正確",
    "amount": "請輸入整數金額",
    "account_id": "請重新選擇科目",
    "payment_method_id": "請重新選擇付款方式",
    "voucher_type": "請重新選擇憑證類型",
    "receive_account_id": "請重新選擇收款帳戶",
    "from_account_id": "請重新選擇轉出帳戶",
    "to_account_id": "請重新選擇轉入帳戶",
    "fee_amount": "請輸入整數手續費",
    "fee_account_id": "請重新選擇手續費科目",
    "submission_token": "表單已失效，請重新整理後再試。",
}


class FormParseError(Exception):
    """表單欄位無法解析。errors：{欄位名稱: 訊息}。"""

    def __init__(self, errors: dict[str, str]) -> None:
        super().__init__("；".join(errors.values()))
        self.errors = errors


def _blank(value: object) -> object:
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return value


def _parse_int(value: object) -> object:
    value = _blank(value)
    if isinstance(value, str):
        # 只接受 ASCII 數字；int() 會接受的底線、全形數字在這裡一律視為無法解析
        if not _INT.fullmatch(value):
            raise ValueError("not an integer")
        return int(value)
    return value


def _parse_date(value: object) -> object:
    value = _blank(value)
    if isinstance(value, str):
        # 只接受 YYYY-MM-DD（<input type="date"> 送出的格式）
        if not _DATE.fullmatch(value):
            raise ValueError("not a date")
        return date.fromisoformat(value)
    return value


def _parse_voucher_type(value: object) -> object:
    value = _blank(value)
    if isinstance(value, str) and value not in _VOUCHER_TYPES:
        raise ValueError("unknown voucher type")
    return value


def _normalize_invoice_no(value: object) -> object:
    value = _blank(value)
    if isinstance(value, str):
        value = value.upper()
        match = _INVOICE_WITH_HYPHEN.fullmatch(value)
        if match:
            value = match.group(1) + match.group(2)
    return value


def _parse_token(value: object) -> object:
    value = _blank(value)
    if not isinstance(value, str) or not _TOKEN.fullmatch(value):
        raise ValueError("missing or malformed submission token")
    return value


OptionalInt = Annotated[int | None, BeforeValidator(_parse_int)]
OptionalDate = Annotated[date | None, BeforeValidator(_parse_date)]
OptionalText = Annotated[str | None, BeforeValidator(_blank)]


class _ExpenseFields(BaseModel):
    """支出表單開放的欄位；其他欄位（手續費、利息、原幣、不計入預算……）一律忽略。"""

    model_config = ConfigDict(extra="ignore")

    txn_date: OptionalDate = None
    amount: OptionalInt = None
    account_id: OptionalInt = None
    payment_method_id: OptionalInt = None
    description: OptionalText = None
    merchant_name: OptionalText = None
    voucher_type: Annotated[str | None, BeforeValidator(_parse_voucher_type)] = None
    invoice_no: Annotated[str | None, BeforeValidator(_normalize_invoice_no)] = None

    def to_form(self, merchant_id: int | None = None) -> ExpenseForm:
        """merchant_id 由呼叫端用 merchant_name 向 services/merchants.py 換得。"""
        return ExpenseForm(
            txn_date=self.txn_date,
            amount=self.amount,
            account_id=self.account_id,
            payment_method_id=self.payment_method_id,
            description=self.description,
            merchant_id=merchant_id,
            voucher_type=self.voucher_type,
            invoice_no=self.invoice_no,
        )


class ExpenseInput(_ExpenseFields):
    """新增（POST /txn/new）：一次性送出權杖必填（規格 9.3、P2 Step 2 決策 A）。"""

    submission_token: Annotated[str, BeforeValidator(_parse_token)]


class ExpenseEditInput(_ExpenseFields):
    """修改（POST /txn/{id}/edit，P2 Step 4A）：沒有送出權杖；送來的權杖欄位一律忽略。
    修改不會新增交易，重送同一份修改不會造成重複記帳。"""


def parse_expense(
    raw: dict[str, str], *, require_submission_token: bool = True
) -> ExpenseInput | ExpenseEditInput:
    """把表單原始字串解析成支出輸入；無法解析時丟 FormParseError。

    require_submission_token=True（預設）給新增使用，缺少或格式錯誤的權杖 → 表單已失效。
    只有修改才傳 False。
    """
    model = ExpenseInput if require_submission_token else ExpenseEditInput
    return _validate(model, raw)


class IncomeInput(BaseModel):
    """收入（P2 Step 5A，UF03）：只有新增，一次性送出權杖必填。不開放商家。"""

    model_config = ConfigDict(extra="ignore")

    txn_date: OptionalDate = None
    amount: OptionalInt = None
    account_id: OptionalInt = None  # 收入科目
    receive_account_id: OptionalInt = None
    description: OptionalText = None
    submission_token: Annotated[str, BeforeValidator(_parse_token)]

    def to_form(self) -> IncomeForm:
        return IncomeForm(
            txn_date=self.txn_date,
            amount=self.amount,
            account_id=self.account_id,
            receive_account_id=self.receive_account_id,
            description=self.description,
        )


class TransferInput(BaseModel):
    """轉帳（P2 Step 5A，UF03、UF04）：只有新增，一次性送出權杖必填。
    轉入信用卡時由 P1 自動改成繳卡費（D9），這裡不判斷。"""

    model_config = ConfigDict(extra="ignore")

    txn_date: OptionalDate = None
    amount: OptionalInt = None
    from_account_id: OptionalInt = None
    to_account_id: OptionalInt = None
    fee_amount: OptionalInt = None
    fee_account_id: OptionalInt = None
    description: OptionalText = None
    submission_token: Annotated[str, BeforeValidator(_parse_token)]

    def to_form(self) -> TransferForm:
        return TransferForm(
            txn_date=self.txn_date,
            amount=self.amount,
            from_account_id=self.from_account_id,
            to_account_id=self.to_account_id,
            fee_amount=self.fee_amount,
            fee_account_id=self.fee_account_id,
            description=self.description,
        )


def parse_income(raw: dict[str, str]) -> IncomeInput:
    return _validate(IncomeInput, raw)


def parse_transfer(raw: dict[str, str]) -> TransferInput:
    return _validate(TransferInput, raw)


def _validate(model, raw: dict[str, str]):
    """解析表單；無法解析時丟 FormParseError（{欄位: 訊息}）。"""
    try:
        return model.model_validate(raw)
    except ValidationError as exc:
        errors: dict[str, str] = {}
        for error in exc.errors():
            field = str(error["loc"][0]) if error["loc"] else ""
            errors.setdefault(field, PARSE_MESSAGES.get(field, "欄位格式不正確"))
        raise FormParseError(errors) from None
