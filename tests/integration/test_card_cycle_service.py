"""信用卡本期應繳 amount_due() 與 current_cycle()（P3-2A；規格 8.1、T18、6.2、M07）。

測試用科目表的聯邦信用卡（2101）已有設定：結帳日 15、繳款日 3。
每個測試都明確傳入 today，不靠系統時鐘；時鐘固定在 2026-11-30，讓 10 月的交易不會觸發 V11。
"""

from datetime import date

import pytest
from sqlalchemy import select

from app.models import Account, PaymentMethod
from app.services import accounts as account_service
from app.services import credit_cards, transactions
from app.services.entries import (
    CardPaymentForm,
    ExpenseForm,
    OpeningForm,
    OpeningLine,
    RefundForm,
)
from app.services.errors import RuleViolation
from app.services.ledger_queries import balance
from tests.ledger_helpers import acc, freeze_today, insert_raw_transaction, post_manual

D = date


@pytest.fixture(autouse=True)
def _today(monkeypatch):
    freeze_today(monkeypatch, D(2026, 11, 30))


@pytest.fixture
def card(seeded_db) -> int:
    return acc(seeded_db, "2101")


def card_method(db) -> int:
    return db.scalar(select(PaymentMethod.id).where(PaymentMethod.name == "聯邦信用卡"))


def charge(db, day: date, amount: int) -> int:
    """在信用卡上記一筆消費（MANUAL；只看卡片分錄時用）。"""
    return post_manual(
        db, day, ("5201", amount), ("2101", -amount), confirmed={"V11"}
    ).transaction_id


def spend(db, day: date, amount: int, **extra) -> int:
    """真正的 EXPENSE 交易，刷聯邦信用卡。"""
    form = ExpenseForm(
        txn_date=day,
        amount=amount,
        account_id=acc(db, "5201"),
        payment_method_id=card_method(db),
        **extra,
    )
    return transactions.record_expense(db, form, confirmed={"V11"}).transaction_id


def pay(db, day: date, amount: int) -> int:
    """繳卡費：聯邦活存 → 聯邦信用卡（CARD_PAYMENT）。"""
    form = CardPaymentForm(day, amount, acc(db, "2101"), acc(db, "1103"))
    return transactions.record_card_payment(db, form, confirmed={"V11"}).transaction_id


def due(db, card_id: int, today: date):
    result = credit_cards.amount_due(db, card_id, today)
    assert result is not None
    return result


def figures(result) -> tuple[int, int, int, int]:
    return (result.statement_balance, result.paid_since, result.amount_due, result.overpaid)


# --- T18 與週期歸屬 ----------------------------------------------------------------------


def test_t18_card_amount_due_excludes_spending_after_closing(seeded_db, card):
    charge(seeded_db, D(2026, 10, 14), 500)
    charge(seeded_db, D(2026, 10, 16), 300)
    result = due(seeded_db, card, D(2026, 10, 20))
    assert figures(result) == (500, 0, 500, 0)
    assert result.cycle.statement_end == D(2026, 10, 15)


def test_closing_day_spending_belongs_to_this_statement(seeded_db, card):
    charge(seeded_db, D(2026, 10, 15), 200)
    charge(seeded_db, D(2026, 10, 16), 100)
    assert due(seeded_db, card, D(2026, 10, 20)).amount_due == 200


def test_on_closing_day_today_counts_for_this_statement(seeded_db, card):
    charge(seeded_db, D(2026, 10, 15), 200)
    result = due(seeded_db, card, D(2026, 10, 15))
    assert result.cycle.statement_end == D(2026, 10, 15)
    assert figures(result) == (200, 0, 200, 0)


def test_day_before_closing_still_refers_to_previous_statement(seeded_db, card):
    charge(seeded_db, D(2026, 9, 10), 400)
    charge(seeded_db, D(2026, 10, 10), 100)  # 10/15 才結帳，今天 10/14 還不在任何已結帳的單裡
    result = due(seeded_db, card, D(2026, 10, 14))
    assert result.cycle.statement_end == D(2026, 9, 15)
    assert result.amount_due == 400


def test_empty_card_is_zero(seeded_db, card):
    assert figures(due(seeded_db, card, D(2026, 10, 20))) == (0, 0, 0, 0)


# --- 繳款 --------------------------------------------------------------------------------


def test_partial_payment_after_closing_reduces_amount_due(seeded_db, card):
    charge(seeded_db, D(2026, 10, 14), 500)
    pay(seeded_db, D(2026, 10, 18), 200)
    assert figures(due(seeded_db, card, D(2026, 10, 20))) == (500, 200, 300, 0)


def test_full_payment_is_zero_due(seeded_db, card):
    charge(seeded_db, D(2026, 10, 14), 500)
    pay(seeded_db, D(2026, 10, 18), 500)
    assert figures(due(seeded_db, card, D(2026, 10, 20))) == (500, 500, 0, 0)


def test_overpayment_is_reported_separately(seeded_db, card):
    charge(seeded_db, D(2026, 10, 14), 500)
    pay(seeded_db, D(2026, 10, 18), 600)
    result = due(seeded_db, card, D(2026, 10, 20))
    assert (result.amount_due, result.overpaid) == (0, 100)


def test_payment_on_closing_day_is_not_deducted_twice(seeded_db, card):
    charge(seeded_db, D(2026, 10, 10), 500)
    pay(seeded_db, D(2026, 10, 15), 500)  # 結帳日當天：已經在結帳日餘額裡
    assert figures(due(seeded_db, card, D(2026, 10, 20))) == (0, 0, 0, 0)


def test_payment_before_closing_already_in_statement_balance(seeded_db, card):
    charge(seeded_db, D(2026, 10, 1), 500)
    pay(seeded_db, D(2026, 10, 10), 200)
    assert figures(due(seeded_db, card, D(2026, 10, 20))) == (300, 0, 300, 0)


def test_unpaid_previous_statement_carries_into_next(seeded_db, card):
    charge(seeded_db, D(2026, 9, 10), 400)
    charge(seeded_db, D(2026, 10, 10), 100)
    assert figures(due(seeded_db, card, D(2026, 10, 20))) == (500, 0, 500, 0)


def test_paying_previous_statement_before_closing_clears_it(seeded_db, card):
    charge(seeded_db, D(2026, 9, 10), 400)
    pay(seeded_db, D(2026, 10, 3), 400)
    charge(seeded_db, D(2026, 10, 10), 100)
    assert figures(due(seeded_db, card, D(2026, 10, 20))) == (100, 0, 100, 0)


# --- 退款只看字面：不是繳款 ---------------------------------------------------------------


def refund(db, original_id: int, day: date, amount: int) -> None:
    form = RefundForm(original_id, day, amount, card_method(db))
    transactions.record_refund(db, form, confirmed={"V11"})


def test_refund_after_closing_is_not_deducted(seeded_db, card):
    original = spend(seeded_db, D(2026, 10, 10), 500)
    refund(seeded_db, original, D(2026, 10, 18), 200)
    result = due(seeded_db, card, D(2026, 10, 20))
    assert figures(result) == (500, 0, 500, 0)
    # 卡片實際餘額已經少了 200，只是不算進已結帳的這一期
    assert balance(seeded_db, card, D(2026, 10, 20)) == 300


def test_refund_before_closing_is_in_statement_balance(seeded_db, card):
    original = spend(seeded_db, D(2026, 10, 10), 500)
    refund(seeded_db, original, D(2026, 10, 12), 200)
    assert figures(due(seeded_db, card, D(2026, 10, 20))) == (300, 0, 300, 0)


# --- 已刪除、草稿、未來日期 ---------------------------------------------------------------


def test_deleted_purchase_excluded_and_restored_counts_again(seeded_db, card):
    txn_id = spend(seeded_db, D(2026, 10, 10), 500)
    transactions.delete_transaction(seeded_db, txn_id)
    assert due(seeded_db, card, D(2026, 10, 20)).amount_due == 0
    transactions.restore_transaction(seeded_db, txn_id)
    assert due(seeded_db, card, D(2026, 10, 20)).amount_due == 500


def test_deleted_payment_is_not_deducted(seeded_db, card):
    charge(seeded_db, D(2026, 10, 10), 500)
    payment_id = pay(seeded_db, D(2026, 10, 18), 500)
    assert due(seeded_db, card, D(2026, 10, 20)).amount_due == 0
    transactions.delete_transaction(seeded_db, payment_id)
    assert due(seeded_db, card, D(2026, 10, 20)).amount_due == 500
    transactions.restore_transaction(seeded_db, payment_id)
    assert due(seeded_db, card, D(2026, 10, 20)).amount_due == 0


def test_draft_transaction_excluded(seeded_db, card):
    insert_raw_transaction(seeded_db, "2026-10-10", ("5201", 500), ("2101", -500), status="DRAFT")
    assert due(seeded_db, card, D(2026, 10, 20)).amount_due == 0


def test_future_dated_purchase_and_payment_not_counted(seeded_db, card, monkeypatch):
    freeze_today(monkeypatch, D(2026, 10, 20))
    charge(seeded_db, D(2026, 10, 14), 500)
    charge(seeded_db, D(2026, 10, 25), 300)  # 未來日期的刷卡
    pay(seeded_db, D(2026, 10, 28), 500)  # 未來日期的繳款：還不能扣
    result = credit_cards.amount_due(seeded_db, card)  # today 預設 = 台北今天
    assert figures(result) == (500, 0, 500, 0)
    assert result.cycle.days_until_due == 14
    # 日期到了之後：結帳日 11/15 的餘額 = 500 + 300 − 500
    assert figures(due(seeded_db, card, D(2026, 11, 20))) == (300, 0, 300, 0)


# --- V1 分期全額入帳 ----------------------------------------------------------------------


def test_installment_is_counted_in_full_in_v1(seeded_db, card):
    """V1 分期不拆期：本金與利息在刷卡當天全額入帳，所以本期應繳會偏高（規格 8.1）。"""
    spend(
        seeded_db,
        D(2026, 10, 10),
        1000,
        interest_amount=100,
        interest_account_id=acc(seeded_db, "5501"),
    )
    assert figures(due(seeded_db, card, D(2026, 10, 20))) == (1100, 0, 1100, 0)


# --- 上線首期的已知限制（P3-7 待辦：信用卡首次初始化策略）------------------------------------


def opening_for_card(db, card_id: int, day: date, debt: int) -> None:
    form = OpeningForm(day, (OpeningLine(card_id, debt),))
    transactions.record_opening(db, form)


def test_opening_after_statement_end_is_not_in_statement_balance(seeded_db, card):
    """已知限制，不是要求 service 特判：單一 opening 的日期晚於最近結帳日時，
    結帳日餘額看不到它，amount_due 為 0。隔離情境：這張卡沒有任何其他交易。"""
    opening_for_card(seeded_db, card, D(2026, 10, 20), 8000)
    result = due(seeded_db, card, D(2026, 10, 20))
    assert result.cycle.statement_end == D(2026, 10, 15)  # opening 在 statement_end 之後
    assert result.statement_balance == 0  # closing_balance 不含該 opening
    assert result.amount_due == 0
    # 欠款確實存在，只是系統無法判斷其中哪一段屬於已結帳的帳單
    assert balance(seeded_db, card, D(2026, 10, 20)) == 8000


def test_opening_after_statement_end_then_payment_shows_overpaid_until_next_closing(
    seeded_db, card
):
    opening_for_card(seeded_db, card, D(2026, 10, 20), 8000)
    pay(seeded_db, D(2026, 11, 3), 5000)
    early = due(seeded_db, card, D(2026, 11, 10))  # 還在同一個 statement cycle
    assert (early.statement_balance, early.paid_since) == (0, 5000)
    assert (early.amount_due, early.overpaid) == (0, 5000)
    # 過了下一個結帳日（11/15），結帳日餘額 = 8000 − 5000，恢復正確
    assert figures(due(seeded_db, card, D(2026, 11, 20))) == (3000, 0, 3000, 0)


# --- 設定、邊界 ---------------------------------------------------------------------------


def test_month_end_closing_day_uses_last_day_of_month(seeded_db, card, monkeypatch):
    freeze_today(monkeypatch, D(2027, 3, 15))
    credit_cards.set_profile(seeded_db, card, closing_day=31, due_day=15)
    charge(seeded_db, D(2027, 2, 28), 100)  # 2 月的結帳日 = 2/28
    charge(seeded_db, D(2027, 3, 1), 50)
    result = due(seeded_db, card, D(2027, 3, 1))
    assert result.cycle.statement_end == D(2027, 2, 28)
    assert result.cycle.due_date == D(2027, 3, 15)
    assert result.amount_due == 100


def test_current_cycle_matches_amount_due_cycle(seeded_db, card):
    cycle = credit_cards.current_cycle(seeded_db, card, D(2026, 10, 20))
    assert cycle == due(seeded_db, card, D(2026, 10, 20)).cycle
    assert (cycle.statement_start, cycle.statement_end) == (D(2026, 9, 16), D(2026, 10, 15))
    assert (cycle.due_date, cycle.next_closing) == (D(2026, 11, 3), D(2026, 11, 15))
    assert cycle.due_date > cycle.statement_end


def test_days_until_due_negative_after_due_date(seeded_db, card):
    cycle = credit_cards.current_cycle(seeded_db, card, D(2026, 10, 14))
    assert cycle.due_date == D(2026, 10, 3)
    assert cycle.days_until_due == -11


def test_card_without_profile_returns_none(seeded_db):
    new_card = account_service.create_account(
        seeded_db, code="2102", name="新信用卡", account_class="LIABILITY", subtype="CREDIT_CARD"
    )
    assert credit_cards.current_cycle(seeded_db, new_card.id, D(2026, 10, 20)) is None
    assert credit_cards.amount_due(seeded_db, new_card.id, D(2026, 10, 20)) is None


@pytest.mark.parametrize("fn", [credit_cards.current_cycle, credit_cards.amount_due])
def test_non_credit_card_account_is_rejected(seeded_db, fn):
    with pytest.raises(RuleViolation) as exc_info:
        fn(seeded_db, acc(seeded_db, "1102"), D(2026, 10, 20))
    assert exc_info.value.code == "NOT_CREDIT_CARD"


def test_inactive_card_still_computes(seeded_db, card):
    charge(seeded_db, D(2026, 10, 10), 500)
    seeded_db.get(Account, card).is_active = False
    seeded_db.commit()
    assert due(seeded_db, card, D(2026, 10, 20)).amount_due == 500


def test_other_card_is_not_mixed_in(seeded_db, card):
    other = account_service.create_account(
        seeded_db, code="2102", name="另一張卡", account_class="LIABILITY", subtype="CREDIT_CARD"
    )
    credit_cards.set_profile(seeded_db, other.id, closing_day=15, due_day=3)
    charge(seeded_db, D(2026, 10, 10), 500)
    post_manual(seeded_db, D(2026, 10, 11), ("5201", 400), ("2102", -400))
    form = CardPaymentForm(D(2026, 10, 18), 100, other.id, acc(seeded_db, "1103"))
    transactions.record_card_payment(seeded_db, form)
    assert figures(due(seeded_db, card, D(2026, 10, 20))) == (500, 0, 500, 0)
    assert figures(due(seeded_db, other.id, D(2026, 10, 20))) == (400, 100, 300, 0)
