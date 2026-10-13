"""/txn/new 表單的型態解析（app/schemas/txn.py）。

這一層只判斷「能不能解析」；解析成功後是否合法（V01、V05……）由 P1 負責，
所以 0、負數、空白在這裡都要原樣交出去，不能被擋下。
"""

from datetime import date

import pytest

from app.schemas.txn import FormParseError
from app.schemas.txn import parse_expense as _parse_expense

TOKEN = "t" * 32


def parse_expense(raw: dict[str, str]):
    """Step 1 的測試只關心表單欄位；送出權杖（Step 2 起必填）自動補上。"""
    return _parse_expense({"submission_token": TOKEN, **raw})


def test_parses_strings_into_expense_form():
    form = parse_expense(
        {
            "txn_date": "2026-10-15",
            "amount": " 120 ",
            "account_id": "7",
            "payment_method_id": "3",
            "description": "  午餐  ",
        }
    )
    assert form.txn_date == date(2026, 10, 15)
    assert form.amount == 120
    assert form.account_id == 7
    assert form.payment_method_id == 3
    assert form.description == "午餐"


def test_blank_fields_become_none_for_p1_to_judge():
    form = parse_expense(
        {
            "txn_date": "",
            "amount": "  ",
            "account_id": "",
            "payment_method_id": "",
            "description": "",
        }
    )
    assert (form.txn_date, form.amount, form.account_id, form.payment_method_id) == (
        None,
        None,
        None,
        None,
    )
    assert form.description is None


@pytest.mark.parametrize("value", ["0", "-5"])
def test_zero_and_negative_are_left_to_p1(value):
    assert parse_expense({"amount": value}).amount == int(value)


def test_fields_outside_the_form_are_ignored():
    """外幣（V1.5）與 merchant_id 不開放；送來也忽略。"""
    form = parse_expense(
        {
            "amount": "120",
            "merchant_id": "1",
            "original_currency": "USD",
            "original_amount_minor": "399",
        }
    ).to_form()
    assert form.merchant_id is None
    assert (form.original_currency, form.original_amount_minor) == (None, None)


def test_advanced_fields_are_parsed():
    """P2 Step 6：手續費、分期利息、不計入預算。"""
    form = parse_expense(
        {
            "amount": "30000",
            "fee_amount": "15",
            "fee_account_id": "7",
            "interest_amount": "600",
            "interest_account_id": "8",
            "exclude_from_budget": "1",
        }
    ).to_form()
    assert (form.fee_amount, form.fee_account_id) == (15, 7)
    assert (form.interest_amount, form.interest_account_id) == (600, 8)
    assert form.exclude_from_budget is True
    blank = parse_expense({"amount": "1", "fee_amount": " ", "exclude_from_budget": ""}).to_form()
    assert (blank.fee_amount, blank.exclude_from_budget) == (None, False)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("fee_amount", "1.5", "請輸入整數手續費"),
        ("interest_amount", "abc", "請輸入整數分期利息"),
        ("interest_account_id", "x", "請重新選擇分期利息科目"),
        ("exclude_from_budget", "maybe", "請重新勾選「不計入預算」"),
    ],
)
def test_unparseable_advanced_fields(field, value, message):
    with pytest.raises(FormParseError) as exc:
        parse_expense({field: value})
    assert exc.value.errors == {field: message}


@pytest.mark.parametrize("value", ["abc", "12.5", "1_000", "１２０", "1e3", "1,200"])
def test_unparseable_amount(value):
    with pytest.raises(FormParseError) as exc:
        parse_expense({"amount": value})
    assert exc.value.errors == {"amount": "請輸入整數金額"}


@pytest.mark.parametrize("value", ["2026/10/15", "2026-13-01", "20261015", "2026-W42-4", "明天"])
def test_unparseable_date(value):
    with pytest.raises(FormParseError) as exc:
        parse_expense({"txn_date": value})
    assert exc.value.errors == {"txn_date": "日期格式不正確"}


def test_all_unparseable_fields_reported_together():
    with pytest.raises(FormParseError) as exc:
        parse_expense({"amount": "x", "account_id": "x", "payment_method_id": "x"})
    assert exc.value.errors == {
        "amount": "請輸入整數金額",
        "account_id": "請重新選擇科目",
        "payment_method_id": "請重新選擇付款方式",
    }


# --- P2 Step 2：商家、憑證、發票號碼、送出權杖 ------------------------------------------


def test_merchant_name_is_only_trimmed():
    data = parse_expense({"merchant_name": "  ７－１１ "})
    assert data.merchant_name == "７－１１"
    assert parse_expense({"merchant_name": "  "}).merchant_name is None


def test_to_form_carries_merchant_voucher_and_invoice():
    form = parse_expense({"voucher_type": "EINVOICE", "invoice_no": "AB12345678"}).to_form(
        merchant_id=9
    )
    assert (form.merchant_id, form.voucher_type, form.invoice_no) == (9, "EINVOICE", "AB12345678")


@pytest.mark.parametrize("value", ["EINVOICE", "PAPER_INVOICE", "RECEIPT", "NONE"])
def test_voucher_types(value):
    assert parse_expense({"voucher_type": value}).voucher_type == value


@pytest.mark.parametrize("value", ["einvoice", "PHOTO", "1"])
def test_unknown_voucher_type(value):
    with pytest.raises(FormParseError) as exc:
        parse_expense({"voucher_type": value})
    assert exc.value.errors == {"voucher_type": "請重新選擇憑證類型"}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("AB12345678", "AB12345678"),
        ("ab12345678", "AB12345678"),
        ("AB-12345678", "AB12345678"),
        (" ab-12345678 ", "AB12345678"),
        # 只移除「2 個字母與 8 位數字之間的單一連字號」；其他格式原樣交給 V08 判斷
        ("AB--12345678", "AB--12345678"),
        ("A-B12345678", "A-B12345678"),
        ("AB 12345678", "AB 12345678"),
        ("AB-1234", "AB-1234"),
        ("AB1234-5678", "AB1234-5678"),
    ],
)
def test_invoice_no_normalization(raw, expected):
    assert parse_expense({"invoice_no": raw}).invoice_no == expected


@pytest.mark.parametrize("token", [None, "", "short", "has space in it 123456", "x" * 129])
def test_missing_or_malformed_token(token):
    raw = {} if token is None else {"submission_token": token}
    with pytest.raises(FormParseError) as exc:
        _parse_expense(raw)
    assert exc.value.errors == {"submission_token": "表單已失效，請重新整理後再試。"}
