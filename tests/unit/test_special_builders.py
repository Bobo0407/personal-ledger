"""特殊交易 builder 的純函式行為：Opening、Adjustment、Payroll（Step 5）、Refund（Step 6）。"""

from datetime import date

import pytest

from app.services.drafts import PostingDraft
from app.services.entries import (
    AdjustmentEntry,
    AdjustmentForm,
    ManualEntry,
    ManualForm,
    ManualLine,
    OpeningEntry,
    OpeningForm,
    OpeningLine,
    PayrollEntry,
    RefundEntry,
    RefundForm,
)

D = date(2026, 10, 1)
CASH, POST_OFFICE, CARD, EQUITY, ADJ = 11, 12, 21, 31, 32
SIGNS = {CASH: 1, POST_OFFICE: 1, CARD: -1}


def codes(issues):
    return [(i.code, i.field) for i in issues]


# --- OpeningEntry -----------------------------------------------------------------


def test_opening_t13_lines_and_equity():
    form = OpeningForm(
        D, (OpeningLine(CASH, 2000), OpeningLine(POST_OFFICE, 50_000), OpeningLine(CARD, 3000))
    )
    draft = OpeningEntry.build(form, opening_equity_id=EQUITY, signs=SIGNS)
    assert draft.txn_type == "OPENING"
    assert draft.postings == (
        PostingDraft(CASH, 2000),
        PostingDraft(POST_OFFICE, 50_000),
        PostingDraft(CARD, -3000),  # 負債欠款 3,000 → 貸方
        PostingDraft(EQUITY, -49_000),
    )


def test_opening_zero_lines_skipped_and_negative_allowed():
    form = OpeningForm(D, (OpeningLine(CASH, 0), OpeningLine(POST_OFFICE, -500)))
    draft = OpeningEntry.build(form, opening_equity_id=EQUITY, signs=SIGNS)
    assert draft.postings == (PostingDraft(POST_OFFICE, -500), PostingDraft(EQUITY, 500))


def test_opening_without_equity_when_total_is_zero():
    # R2：資產剛好等於負債時不建立 0 元期初權益分錄
    form = OpeningForm(D, (OpeningLine(CASH, 3000), OpeningLine(CARD, 3000)))
    draft = OpeningEntry.build(form, opening_equity_id=EQUITY, signs=SIGNS)
    assert draft.postings == (PostingDraft(CASH, 3000), PostingDraft(CARD, -3000))


def test_opening_check():
    assert codes(OpeningEntry.check(OpeningForm(D, ()))) == [("V05", "lines")]
    form = OpeningForm(D, (OpeningLine(None, None), OpeningLine(CASH, 1.5), OpeningLine(CASH, 0)))
    assert codes(OpeningEntry.check(form)) == [
        ("V05", "lines[0].account_id"),
        ("V05", "lines[0].balance"),
        ("OPENING_BALANCE", "lines[1].balance"),
    ]


def test_opening_form_has_no_counterpart_field():
    assert "opening_equity_id" not in OpeningForm.__dataclass_fields__


# --- AdjustmentEntry --------------------------------------------------------------


def test_adjustment_t33_cash_down():
    form = AdjustmentForm(D, CASH, -200, "盤點短少")
    draft = AdjustmentEntry.build(form, adjustment_account_id=ADJ, sign=1)
    assert draft.txn_type == "ADJUSTMENT"
    assert draft.description == "盤點短少"
    assert draft.postings == (PostingDraft(CASH, -200), PostingDraft(ADJ, 200))


def test_adjustment_liability_direction():
    # 負債 +100 = 欠款增加 100 → 信用卡貸方 100
    draft = AdjustmentEntry.build(
        AdjustmentForm(D, CARD, 100, "帳單差額"), adjustment_account_id=ADJ, sign=-1
    )
    assert draft.postings == (PostingDraft(CARD, -100), PostingDraft(ADJ, 100))


@pytest.mark.parametrize(
    ("delta", "expected"),
    [
        (None, [("V05", "delta")]),
        (0, [("ADJUSTMENT_DELTA", "delta")]),
        (1.5, [("ADJUSTMENT_DELTA", "delta")]),
        (True, [("ADJUSTMENT_DELTA", "delta")]),
        (-200, []),  # 負數合法，不套 V01
    ],
)
def test_adjustment_delta(delta, expected):
    assert codes(AdjustmentEntry.check(AdjustmentForm(D, CASH, delta, "x"))) == expected


def test_adjustment_requires_account_but_leaves_description_to_v15():
    assert codes(AdjustmentEntry.check(AdjustmentForm(D, None, 100, None))) == [
        ("V05", "account_id")
    ]


def test_adjustment_form_has_no_counterpart_field():
    assert set(AdjustmentForm.__dataclass_fields__) == {
        "txn_date",
        "account_id",
        "delta",
        "description",
    }


# --- PayrollEntry -----------------------------------------------------------------


def test_payroll_shares_manual_mechanism_but_is_payroll():
    form = ManualForm(D, (ManualLine(1, 27_050), ManualLine(2, 0), ManualLine(3, -27_050)))
    draft = PayrollEntry.build(form)
    assert draft.txn_type == "PAYROLL"
    assert [p.account_id for p in draft.postings] == [1, 3]  # 0 元欄位不產生分錄
    assert ManualEntry.build(form).txn_type == "MANUAL"


# --- RefundEntry（Step 6）---------------------------------------------------------


def test_refund_builder_reverses_main_and_copies_original_fields():
    form = RefundForm(original_txn_id=7, txn_date=D, amount=800, payment_method_id=3)
    draft = RefundEntry.build(
        form, payment_account_id=CARD, main_account_id=41, merchant_id=5, exclude_from_budget=True
    )
    assert draft.txn_type == "REFUND"
    assert draft.related_txn_id == 7
    assert draft.payment_method_id == 3
    assert (draft.merchant_id, draft.exclude_from_budget) == (5, True)
    assert draft.postings == (PostingDraft(CARD, 800), PostingDraft(41, -800, "MAIN"))


def test_refund_check():
    assert codes(RefundEntry.check(RefundForm(None, D, None, None))) == [
        ("V05", "original_txn_id"),
        ("V05", "amount"),
        ("V05", "payment_method_id"),
    ]
    assert codes(RefundEntry.check(RefundForm(1, D, -5, 2))) == [("V01", "amount")]


def test_refund_form_has_no_account_or_currency_fields():
    # 支出科目、商家、預算旗標從原交易帶入（U7）；不支援原幣（U12）
    assert set(RefundForm.__dataclass_fields__) == {
        "original_txn_id",
        "txn_date",
        "amount",
        "payment_method_id",
        "description",
    }
