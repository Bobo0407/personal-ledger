"""/txn/new 表單的型態解析（app/schemas/txn.py）。

這一層只判斷「能不能解析」；解析成功後是否合法（V01、V05……）由 P1 負責，
所以 0、負數、空白在這裡都要原樣交出去，不能被擋下。
"""

from datetime import date

import pytest

from app.schemas.txn import FormParseError, parse_expense


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


def test_fields_outside_step1_are_ignored():
    form = parse_expense(
        {"amount": "120", "fee_amount": "30", "invoice_no": "AB12345678", "merchant_id": "1"}
    )
    assert form.fee_amount is None
    assert form.invoice_no is None
    assert form.merchant_id is None


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
