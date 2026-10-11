"""一般交易 builder 的純函式行為：check() 的表單語意與 build() 的分錄結構（Step 4）。"""

from datetime import date

import pytest

from app.services.drafts import PostingDraft
from app.services.entries import (
    CardPaymentEntry,
    CardPaymentForm,
    ExpenseEntry,
    ExpenseForm,
    IncomeEntry,
    IncomeForm,
    TransferEntry,
    TransferForm,
)

D = date(2026, 10, 15)
LUNCH, POST_OFFICE, CARD, FEE, INTEREST, FUBON, SALARY = 51, 11, 21, 55, 53, 13, 41


def codes(issues) -> list[tuple[str, str | None]]:
    return [(i.code, i.field) for i in issues]


# --- ExpenseEntry -----------------------------------------------------------------


def test_expense_builds_main_and_payment_lines():
    form = ExpenseForm(D, 120, LUNCH, payment_method_id=7, description=" 午餐 ")
    draft = ExpenseEntry.build(form, payment_account_id=POST_OFFICE)

    assert draft.txn_type == "EXPENSE"
    assert draft.payment_method_id == 7
    assert draft.description == "午餐"
    assert draft.postings == (
        PostingDraft(LUNCH, 120, line_role="MAIN"),
        PostingDraft(POST_OFFICE, -120),
    )


def test_expense_with_fee_and_interest_and_original_currency():
    form = ExpenseForm(
        D,
        1000,
        LUNCH,
        payment_method_id=5,
        fee_amount=15,
        fee_account_id=FEE,
        interest_amount=600,
        interest_account_id=INTEREST,
        original_currency="JPY",
        original_amount_minor=4800,
    )
    draft = ExpenseEntry.build(form, payment_account_id=CARD)
    assert draft.postings == (
        PostingDraft(LUNCH, 1000, "MAIN", "JPY", 4800),
        PostingDraft(FEE, 15, "FEE"),  # 手續費不帶原幣（P8）
        PostingDraft(INTEREST, 600, "INTEREST"),
        PostingDraft(CARD, -1615),
    )


@pytest.mark.parametrize("zero", [None, 0])
def test_expense_zero_fee_and_interest_mean_none(zero):
    form = ExpenseForm(D, 100, LUNCH, 5, fee_amount=zero, interest_amount=zero)
    assert ExpenseEntry.check(form) == ()
    assert len(ExpenseEntry.build(form, payment_account_id=CARD).postings) == 2


@pytest.mark.parametrize("amount", [0, -120, 12.5, True, "120"])
def test_expense_v01_amount_must_be_positive_int(amount):
    form = ExpenseForm(D, amount, LUNCH, 5)
    assert codes(ExpenseEntry.check(form)) == [("V01", "amount")]


def test_expense_required_fields():
    form = ExpenseForm(D, None, None, None)
    assert codes(ExpenseEntry.check(form)) == [
        ("V05", "amount"),
        ("V05", "account_id"),
        ("V05", "payment_method_id"),
    ]


def test_expense_date_is_left_to_ledger_validation():
    # 日期是否填寫由 validation.py 的 V05 負責，不在 check() 重複
    assert ExpenseEntry.check(ExpenseForm(None, 100, LUNCH, 5)) == ()


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"fee_amount": 15}, [("V05", "fee_account_id")]),
        ({"fee_amount": -15, "fee_account_id": FEE}, [("V01", "fee_amount")]),
        ({"interest_amount": 600}, [("V05", "interest_account_id")]),
        ({"interest_amount": 1.5, "interest_account_id": INTEREST}, [("V01", "interest_amount")]),
    ],
)
def test_expense_optional_amounts(kwargs, expected):
    assert codes(ExpenseEntry.check(ExpenseForm(D, 100, LUNCH, 5, **kwargs))) == expected


# --- IncomeEntry ------------------------------------------------------------------


def test_income_builds_receive_and_main_lines():
    draft = IncomeEntry.build(IncomeForm(D, 30_000, SALARY, receive_account_id=POST_OFFICE))
    assert draft.txn_type == "INCOME"
    assert draft.payment_method_id is None  # P5：收入不使用付款方式
    assert draft.postings == (
        PostingDraft(POST_OFFICE, 30_000),
        PostingDraft(SALARY, -30_000, line_role="MAIN"),
    )


def test_income_required_fields_and_v01():
    assert codes(IncomeEntry.check(IncomeForm(D, None, None, None))) == [
        ("V05", "amount"),
        ("V05", "account_id"),
        ("V05", "receive_account_id"),
    ]
    assert codes(IncomeEntry.check(IncomeForm(D, -1, SALARY, POST_OFFICE))) == [("V01", "amount")]


# --- TransferEntry ----------------------------------------------------------------


def test_transfer_with_fee_has_three_lines():
    form = TransferForm(D, 5000, POST_OFFICE, FUBON, fee_amount=15, fee_account_id=FEE)
    draft = TransferEntry.build(form)
    assert draft.txn_type == "TRANSFER"
    assert draft.payment_method_id is None
    assert draft.postings == (
        PostingDraft(FUBON, 5000),
        PostingDraft(FEE, 15, "FEE"),
        PostingDraft(POST_OFFICE, -5015),
    )
    assert sum(p.amount for p in draft.postings) == 0


def test_transfer_without_fee_has_two_lines():
    draft = TransferEntry.build(TransferForm(D, 5000, POST_OFFICE, FUBON))
    assert draft.postings == (PostingDraft(FUBON, 5000), PostingDraft(POST_OFFICE, -5000))


def test_transfer_v06_same_account():
    form = TransferForm(D, 100, POST_OFFICE, POST_OFFICE)
    assert codes(TransferEntry.check(form)) == [("V06", "to_account_id")]


def test_transfer_required_fields():
    assert codes(TransferEntry.check(TransferForm(D, 100, None, None))) == [
        ("V05", "from_account_id"),
        ("V05", "to_account_id"),
    ]


# --- CardPaymentEntry -------------------------------------------------------------


def test_card_payment_is_only_asset_to_liability():
    draft = CardPaymentEntry.build(CardPaymentForm(D, 800, CARD, FUBON))
    assert draft.txn_type == "CARD_PAYMENT"
    assert draft.payment_method_id is None
    assert draft.postings == (PostingDraft(CARD, 800), PostingDraft(FUBON, -800))


def test_card_payment_required_fields_and_v01():
    assert codes(CardPaymentEntry.check(CardPaymentForm(D, 0, None, None))) == [
        ("V01", "amount"),
        ("V05", "card_account_id"),
        ("V05", "from_account_id"),
    ]
