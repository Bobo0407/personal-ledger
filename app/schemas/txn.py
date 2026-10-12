"""快速記帳表單的型態解析（規格 7.4：POST /txn/new 收到表單，Pydantic 驗證型態）。

這裡只回答「這個輸入能不能解析」，不回答「解析後合不合法」：
- 空白欄位轉成 None，交給 P1 判斷必填（V05）。
- 整數、日期無法解析時丟 FormParseError，訊息是 Web 層的欄位錯誤，
  不使用 P1 的規則代碼，也不新增業務規則。
- 解析成功後的所有業務規則（金額 > 0 的 V01、必填的 V05、V03、V04……）
  都由 P1 的 transactions／validation 負責。
"""

import re
from datetime import date
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict, ValidationError

from app.services.entries import ExpenseForm

_INT = re.compile(r"[+-]?[0-9]+")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")

# 每個欄位解析失敗時顯示的訊息
PARSE_MESSAGES = {
    "txn_date": "日期格式不正確",
    "amount": "請輸入整數金額",
    "account_id": "請重新選擇科目",
    "payment_method_id": "請重新選擇付款方式",
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


OptionalInt = Annotated[int | None, BeforeValidator(_parse_int)]
OptionalDate = Annotated[date | None, BeforeValidator(_parse_date)]
OptionalText = Annotated[str | None, BeforeValidator(_blank)]


class ExpenseInput(BaseModel):
    """Step 1 支出表單只開放這 5 個欄位；其他欄位（手續費、發票……）一律忽略。"""

    model_config = ConfigDict(extra="ignore")

    txn_date: OptionalDate = None
    amount: OptionalInt = None
    account_id: OptionalInt = None
    payment_method_id: OptionalInt = None
    description: OptionalText = None

    def to_form(self) -> ExpenseForm:
        return ExpenseForm(
            txn_date=self.txn_date,
            amount=self.amount,
            account_id=self.account_id,
            payment_method_id=self.payment_method_id,
            description=self.description,
        )


def parse_expense(raw: dict[str, str]) -> ExpenseForm:
    """把表單原始字串轉成 P1 的 ExpenseForm；無法解析時丟 FormParseError。"""
    try:
        return ExpenseInput.model_validate(raw).to_form()
    except ValidationError as exc:
        errors: dict[str, str] = {}
        for error in exc.errors():
            field = str(error["loc"][0]) if error["loc"] else ""
            errors.setdefault(field, PARSE_MESSAGES.get(field, "欄位格式不正確"))
        raise FormParseError(errors) from None
