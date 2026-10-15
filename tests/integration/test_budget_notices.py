"""V14 預算跨門檻提醒（P3-3C；規格 F21、V14、T15）。

時鐘固定在 2026-10-15。預算掛在第一層「餐飲 5100」（子科目 5101、5102）與「購物 5200」。
"""

import pytest
from sqlalchemy import select

from app.models import PaymentMethod, Setting
from app.services import budgets, transactions
from app.services.entries import ExpenseForm, RefundForm
from tests.ledger_helpers import (
    TODAY,
    acc,
    freeze_today,
    insert_raw_transaction,
    post_manual,
)

OCT, NOV, SEP = "2026-10", "2026-11", "2026-09"
FUTURE = TODAY.replace(month=11, day=5)
CONFIG_TEXT = "預算提醒設定異常，這筆交易已儲存，但無法判斷是否跨過預算門檻。"


@pytest.fixture(autouse=True)
def _today(monkeypatch):
    freeze_today(monkeypatch)


@pytest.fixture
def db(seeded_db):
    budgets.set_budget(seeded_db, acc(seeded_db, "5100"), OCT, 1000)
    return seeded_db


def pm(db):
    return db.scalar(select(PaymentMethod.id).where(PaymentMethod.name == "LINE Pay"))


def form(db, amount, *, day=TODAY, code="5101", exclude=False) -> ExpenseForm:
    return ExpenseForm(
        txn_date=day,
        amount=amount,
        account_id=acc(db, code),
        payment_method_id=pm(db),
        exclude_from_budget=exclude,
    )


def spend(db, amount, **kw):
    return transactions.record_expense(db, form(db, amount, **kw), confirmed={"V10", "V11"})


def change(db, txn_id, amount, **kw):
    return transactions.update_transaction(
        db, txn_id, form(db, amount, **kw), confirmed={"V10", "V11"}
    )


def refund(db, original, amount):
    return transactions.record_refund(
        db, RefundForm(original, TODAY, amount, pm(db)), confirmed={"V11"}
    )


def v14(result) -> list[str]:
    return [i.message for i in result.notices if i.code == "V14"]


def set_alert(db, value):
    db.get(Setting, "budget_alert_pct").value = value
    db.commit()


# --- create ------------------------------------------------------------------------------


def test_no_budget_means_no_v14(seeded_db):
    assert v14(spend(seeded_db, 5000)) == []


def test_below_threshold_is_silent(db):
    assert v14(spend(db, 799)) == []


def test_create_crossing_80(db):
    (message,) = v14(spend(db, 850))
    assert message == "餐飲 預算已用 85%（850 / 1,000），接近上限"


def test_exactly_80_percent_counts_as_crossing(db):
    assert len(v14(spend(db, 800))) == 1


def test_exactly_100_percent_is_used_up(db):
    (message,) = v14(spend(db, 1000))
    assert message == "餐飲 預算已用完（1,000 / 1,000）"


def test_create_crossing_100(db):
    (message,) = v14(spend(db, 1200))
    assert message == "餐飲 預算已超支 200 元（已用 1,200 / 1,000）"


def test_crossing_both_thresholds_reports_only_100(db):
    messages = v14(spend(db, 1200))  # 一次從 0 跨到 120%
    assert len(messages) == 1 and "超支" in messages[0]


def test_already_above_80_does_not_repeat(db):
    spend(db, 850)
    assert v14(spend(db, 50)) == []


def test_then_crossing_100_is_reported_once_more(db):
    assert len(v14(spend(db, 850))) == 1
    (message,) = v14(spend(db, 200))
    assert "超支" in message
    assert v14(spend(db, 100)) == []  # 已高於 100% 再增加：不重複


def test_custom_alert_pct(db):
    set_alert(db, "50")
    (message,) = v14(spend(db, 500))
    assert "已用 50%" in message


def test_child_accounts_roll_up_into_the_first_level_budget(db):
    spend(db, 500, code="5101")
    assert len(v14(spend(db, 400, code="5102"))) == 1


def test_exclude_from_budget_never_triggers(db):
    assert v14(spend(db, 5000, exclude=True)) == []


def test_deleted_and_draft_transactions_are_not_in_before(db):
    insert_raw_transaction(db, "2026-10-10", ("5101", 900), ("1101", -900), status="DRAFT")
    insert_raw_transaction(db, "2026-10-10", ("5101", 900), ("1101", -900), deleted=True)
    db.commit()
    assert v14(spend(db, 100)) == []  # used 只有 100，沒有跨過


# --- 未來日期、其他月份 ---------------------------------------------------------------------


def test_future_dated_transaction_uses_its_own_month_budget(db):
    budgets.set_budget(db, acc(db, "5100"), NOV, 2000)
    result = spend(db, 1700, day=FUTURE)  # 11 月預算 2000 的 85%
    (message,) = v14(result)
    assert message == "餐飲（2026-11） 預算已用 85%（1,700 / 2,000），接近上限"


def test_future_month_does_not_use_this_months_budget(db):
    # 11 月沿用 10 月的版本 1000：1700 是 170%，但訊息標示的是 11 月，不是用 10 月的 used
    (message,) = v14(spend(db, 1700, day=FUTURE))
    assert "（2026-11）" in message and "超支 700" in message
    assert v14(spend(db, 100)) == []  # 10 月的 used 沒有被 11 月的交易影響


def test_month_before_first_budget_version_is_silent(db):
    assert v14(spend(db, 5000, day=TODAY.replace(month=9, day=1))) == []


# --- edit --------------------------------------------------------------------------------


def test_edit_increase_crosses(db):
    txn = spend(db, 500).transaction_id
    assert len(v14(change(db, txn, 900, day=TODAY))) == 1


def test_edit_only_counts_the_difference(db):
    txn = spend(db, 700).transaction_id
    assert v14(change(db, txn, 750)) == []  # 700 → 750：總用量 750 < 800
    assert len(v14(change(db, txn, 900))) == 1


def test_edit_decrease_never_triggers(db):
    txn = spend(db, 900).transaction_id
    assert v14(change(db, txn, 100)) == []


def test_edit_crossing_again_after_dropping_below(db):
    txn = spend(db, 900).transaction_id
    change(db, txn, 100)
    assert len(v14(change(db, txn, 900))) == 1


def test_edit_to_another_month(db):
    budgets.set_budget(db, acc(db, "5100"), NOV, 1000)
    spend(db, 700, day=FUTURE)
    txn = spend(db, 100).transaction_id
    (message,) = v14(change(db, txn, 300, day=FUTURE))  # 10 月少 100；11 月 700 → 1000
    assert "（2026-11）" in message and "已用完" in message


def test_edit_to_another_first_level_account(db):
    budgets.set_budget(db, acc(db, "5200"), OCT, 1000)
    txn = spend(db, 900, code="5101").transaction_id
    (message,) = v14(change(db, txn, 900, code="5201"))
    assert message.startswith("購物 ")


def test_exclude_true_to_false_can_cross(db):
    txn = spend(db, 900, exclude=True).transaction_id
    assert len(v14(change(db, txn, 900, exclude=False))) == 1


def test_exclude_false_to_true_never_triggers(db):
    txn = spend(db, 900).transaction_id
    assert v14(change(db, txn, 900, exclude=True)) == []


# --- restore / delete ----------------------------------------------------------------------


def test_restore_crossing(db):
    gone = spend(db, 700).transaction_id
    transactions.delete_transaction(db, gone)
    spend(db, 200)
    (message,) = v14(transactions.restore_transaction(db, gone))
    assert "已用 90%" in message


def test_restore_without_crossing_is_silent(db):
    gone = spend(db, 100).transaction_id
    transactions.delete_transaction(db, gone)
    assert v14(transactions.restore_transaction(db, gone)) == []


def test_delete_expense_never_triggers(db):
    txn = spend(db, 900).transaction_id
    assert v14(transactions.delete_transaction(db, txn)) == []


def test_delete_returns_post_result_with_transaction_id(db):
    txn = spend(db, 100).transaction_id
    result = transactions.delete_transaction(db, txn)
    assert result.transaction_id == txn and result.notices == ()


def test_delete_refund_can_cross(db):
    original = spend(db, 900).transaction_id
    refund_id = refund(db, original, 400).transaction_id  # used 500
    (message,) = v14(transactions.delete_transaction(db, refund_id))  # 回到 900
    assert "已用 90%" in message


# --- refund --------------------------------------------------------------------------------


def test_refund_never_triggers(db):
    original = spend(db, 900).transaction_id
    assert v14(refund(db, original, 400)) == []


def test_editing_refund_smaller_raises_used_and_can_cross(db):
    original = spend(db, 900).transaction_id
    refund_id = refund(db, original, 400).transaction_id  # used 500
    result = transactions.update_transaction(
        db, refund_id, RefundForm(original, TODAY, 100, pm(db)), confirmed={"V11"}
    )  # used 800
    assert len(v14(result)) == 1


def test_negative_used_then_expense_does_not_misreport(db):
    # 退款大於當月其他消費 → used 為負；之後的支出只看跨過與否
    original = spend(db, 100, day=TODAY.replace(day=1)).transaction_id
    refund(db, original, 100)
    assert v14(spend(db, 700)) == []
    assert len(v14(spend(db, 100))) == 1


# --- 多個預算 -------------------------------------------------------------------------------


def test_one_manual_transaction_can_cross_several_budgets_100_first(db):
    budgets.set_budget(db, acc(db, "5200"), OCT, 1000)
    result = post_manual(db, TODAY, ("5101", 900), ("5201", 1200), ("1101", -2100))
    messages = v14(result)
    assert [m.split(" ")[0] for m in messages] == ["購物", "餐飲"]
    assert "超支" in messages[0] and "接近上限" in messages[1]


def test_same_level_budgets_follow_account_order(db):
    budgets.set_budget(db, acc(db, "5200"), OCT, 1000)
    result = post_manual(db, TODAY, ("5201", 900), ("5101", 900), ("1101", -1800))
    assert [m.split(" ")[0] for m in v14(result)] == ["餐飲", "購物"]


def test_lines_in_the_same_first_level_account_are_netted_once(db):
    result = post_manual(db, TODAY, ("5101", 500), ("5102", 400), ("1101", -900))
    assert len(v14(result)) == 1


# --- 設定異常 --------------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["abc", "0", "100", "", "-5"])
def test_invalid_alert_pct_saves_and_reports_config_notice(db, bad):
    set_alert(db, bad)
    result = spend(db, 900)
    assert result.transaction_id
    codes = [i.code for i in result.notices]
    assert codes.count("BUDGET_ALERT_CONFIG") == 1 and "V14" not in codes
    (config,) = [i for i in result.notices if i.code == "BUDGET_ALERT_CONFIG"]
    assert config.message == CONFIG_TEXT


def test_config_notice_appears_once_even_with_several_budgets(db):
    budgets.set_budget(db, acc(db, "5200"), OCT, 1000)
    set_alert(db, "abc")
    result = post_manual(db, TODAY, ("5101", 900), ("5201", 900), ("1101", -1800))
    assert [i.code for i in result.notices].count("BUDGET_ALERT_CONFIG") == 1


def test_no_config_notice_without_budget_or_without_increase(db, seeded_db):
    set_alert(db, "abc")
    assert [i.code for i in spend(db, 100, code="5201").notices if i.code != "V13"] == []
    original = spend(db, 100).transaction_id
    assert "BUDGET_ALERT_CONFIG" not in [i.code for i in refund(db, original, 50).notices]


def test_delete_with_invalid_setting_says_deleted(db):
    original = spend(db, 900).transaction_id
    refund_id = refund(db, original, 400).transaction_id
    set_alert(db, "abc")
    (config,) = transactions.delete_transaction(db, refund_id).notices
    assert config.code == "BUDGET_ALERT_CONFIG" and "已刪除" in config.message


# --- 儲存後卡片 -----------------------------------------------------------------------------


def test_cards_show_budgets_the_transaction_counts_toward(db):
    txn = spend(db, 850).transaction_id
    (card,) = budgets.cards_for_transaction(db, txn)
    assert card.progress.used == 850 and card.state == "warn" and card.month_label == ""


def test_cards_empty_when_excluded_or_no_budget(db):
    assert budgets.cards_for_transaction(db, spend(db, 100, exclude=True).transaction_id) == ()
    assert budgets.cards_for_transaction(db, spend(db, 100, code="5201").transaction_id) == ()
    assert budgets.cards_for_transaction(db, 999999) == ()


def test_cards_for_other_month_carry_month_label(db):
    txn = spend(db, 100, day=FUTURE).transaction_id
    (card,) = budgets.cards_for_transaction(db, txn)
    assert card.month_label == NOV


def test_cards_one_per_budget_in_account_order(db):
    budgets.set_budget(db, acc(db, "5200"), OCT, 1000)
    txn = post_manual(db, TODAY, ("5201", 100), ("5101", 100), ("1101", -200)).transaction_id
    assert [c.progress.account_name for c in budgets.cards_for_transaction(db, txn)] == [
        "餐飲",
        "購物",
    ]


def test_cards_empty_with_invalid_setting(db):
    txn = spend(db, 100).transaction_id
    set_alert(db, "abc")
    assert budgets.cards_for_transaction(db, txn) == ()
