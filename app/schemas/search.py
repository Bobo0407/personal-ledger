"""/txn 搜尋條件的解析（P2 Step 3）。

只在 Web 邊界處理「搜尋條件本身是否合理」，不進 P1 的 validation：
- 日期格式（YYYY-MM-DD）、金額必須是 0 以上的整數
- date_from 不能晚於 date_to、min_amount 不能大於 max_amount
空白欄位代表不限。
"""

from pydantic import BaseModel, ConfigDict, ValidationError

from app.schemas.txn import FormParseError, OptionalDate, OptionalInt, OptionalText
from app.services.search import SearchFilters

PARSE_MESSAGES = {
    "date_from": "日期格式不正確",
    "date_to": "日期格式不正確",
    "min_amount": "請輸入整數金額",
    "max_amount": "請輸入整數金額",
}


class TxnSearchInput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    date_from: OptionalDate = None
    date_to: OptionalDate = None
    merchant: OptionalText = None
    min_amount: OptionalInt = None
    max_amount: OptionalInt = None
    tag: OptionalText = None  # P2 Step 8B：單一標籤

    def to_filters(self) -> SearchFilters:
        return SearchFilters(
            date_from=self.date_from,
            date_to=self.date_to,
            merchant_name=self.merchant,
            min_amount=self.min_amount,
            max_amount=self.max_amount,
            tag_name=self.tag,
        )


def parse_search(raw: dict[str, str]) -> TxnSearchInput:
    """解析搜尋條件；不合理時丟 FormParseError（{欄位: 訊息}）。"""
    try:
        data = TxnSearchInput.model_validate(raw)
    except ValidationError as exc:
        errors: dict[str, str] = {}
        for error in exc.errors():
            field = str(error["loc"][0]) if error["loc"] else ""
            errors.setdefault(field, PARSE_MESSAGES.get(field, "欄位格式不正確"))
        raise FormParseError(errors) from None

    errors = {}
    for field in ("min_amount", "max_amount"):
        value = getattr(data, field)
        if value is not None and value < 0:
            errors[field] = "金額必須是 0 以上的整數"
    if data.date_from and data.date_to and data.date_from > data.date_to:
        errors["date_to"] = "結束日期不能早於開始日期"
    if (
        "min_amount" not in errors
        and "max_amount" not in errors
        and data.min_amount is not None
        and data.max_amount is not None
        and data.min_amount > data.max_amount
    ):
        errors["max_amount"] = "最高金額不能小於最低金額"
    if errors:
        raise FormParseError(errors)
    return data


def parse_page(value: str | None) -> int:
    """頁碼不是使用者輸入的條件：不合法就當作第 1 頁。"""
    try:
        return max(int(value or "1"), 1)
    except ValueError:
        return 1
