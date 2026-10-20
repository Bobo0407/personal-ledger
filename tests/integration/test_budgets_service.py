"""月預算 service（P3-3A；規格 F20、F21、M05、T15、T16、T17、8.1、5.4）。

時鐘固定在 2026-10-15（tests.ledger_helpers.TODAY）。
used 的定義：指定月份整個 calendar month 內，第一層費用科目與所有子孫科目的有效分錄加總，
排除 exclude_from_budget = 1；沒有「截至今天」的截止，日期在該月的未來 POSTED 交易也計入。
"""

from datetime import date

import pytest
from sqlalchemy import func, select

from app.models import Account, Budget, PaymentMethod, Setting
from app.services import accounts as account_service
from app.services import budgets, transactions
from app.services.budgets import BudgetStatus
from app.services.entries import ExpenseForm, RefundForm, TransferForm
from app.services.errors import InvalidSetting, NotFound, RuleViolation
from app.services.ledger_queries import class_total, period_total
from tests.ledger_helpers import (
    TODAY,
    acc,
    freeze_today,
    insert_raw_transaction,
    post_manual,
)

D = date
OCT = "2026-10"
NOV = "2026-11"


@pytest.fixture(autouse=True)
def _today(monkeypatch):
    freeze_today(monkeypatch)


def pm(db, name="LINE Pay") -> int:
    return db.scalar(select(PaymentMethod.id).where(PaymentMethod.name == name))


def spend(db, day: date, amount: int, code="5101", *, exclude=False, **extra) -> int:
    form = ExpenseForm(
        txn_date=day,
        amount=amount,
        account_id=acc(db, code),
        payment_method_id=pm(db),
        exclude_from_budget=exclude,
        **extra,
    )
    return transactions.record_expense(db, form, confirmed={"V10", "V11"}).transaction_id


def refund(db, original: int, day: date, amount: int) -> int:
    form = RefundForm(original, day, amount, pm(db))
    return transactions.record_refund(db, form, confirmed={"V11"}).transaction_id


def set_budget(db, code: str, amount: int, month: str = OCT) -> Budget:
    return budgets.set_budget(db, acc(db, code), month, amount)


def used(db, code: str, month: str = OCT) -> int:
    progress = budgets.account_progress(db, acc(db, code), month)
    assert progress is not None
    return progress.used


def make_top_level_expense(db, code="5900", name="其他") -> Account:
    return account_service.create_account(
        db, code=code, name=name, account_class="EXPENSE", subtype=None
    )


# --- set_budget：M05、金額、月份 --------------------------------------------------------------


def test_set_budget_creates_version(seeded_db):
    row = set_budget(seeded_db, "5100", 5000)
    assert (row.account_id, row.monthly_amount, row.effective_from) == (
        acc(seeded_db, "5100"),
        5000,
        OCT,
    )
    assert seeded_db.scalar(select(func.count()).select_from(Budget)) == 1


def test_m05_first_level_leaf_expense_is_allowed_and_counts_own_postings(seeded_db):
    other = make_top_level_expense(seeded_db)
    budgets.set_budget(seeded_db, other.id, OCT, 3000)
    post_manual(seeded_db, TODAY, ("5900", 700), ("1101", -700))
    assert used(seeded_db, "5900") == 700


@pytest.mark.parametrize(
    "code",
    ["5101", "5502", "4201", "1101", "2101", "3101"],
    ids=["child-expense", "grandchild-expense", "income", "asset", "liability", "equity"],
)
def test_m05_rejects_non_first_level_expense(seeded_db, code):
    with pytest.raises(RuleViolation) as exc_info:
        budgets.set_budget(seeded_db, acc(seeded_db, code), OCT, 5000)
    assert exc_info.value.code == "M05"
    assert seeded_db.scalar(select(func.count()).select_from(Budget)) == 0


@pytest.mark.parametrize("amount", [0, -1, True, "5000", 5000.5, None])
def test_invalid_amount_rejected(seeded_db, amount):
    with pytest.raises(RuleViolation) as exc_info:
        budgets.set_budget(seeded_db, acc(seeded_db, "5100"), OCT, amount)
    assert exc_info.value.code == "BUDGET_AMOUNT"


@pytest.mark.parametrize("month", ["2026-13", "2026-1", "202610", "", "2026-10-01"])
def test_invalid_month_rejected(seeded_db, month):
    with pytest.raises(RuleViolation) as exc_info:
        budgets.set_budget(seeded_db, acc(seeded_db, "5100"), month, 5000)
    assert exc_info.value.code == "BUDGET_MONTH"


def test_effective_from_may_not_be_before_current_month(seeded_db):
    with pytest.raises(RuleViolation) as exc_info:
        set_budget(seeded_db, "5100", 5000, "2026-09")
    assert exc_info.value.code == "BUDGET_PAST_MONTH"
    set_budget(seeded_db, "5100", 5000, "2026-10")  # 本月可以
    set_budget(seeded_db, "5100", 5000, "2026-11")  # 未來月份可以
    set_budget(seeded_db, "5100", 5000, "2027-01")


def test_inactive_account_cannot_get_a_new_budget(seeded_db):
    other = make_top_level_expense(seeded_db)
    other.is_active = False
    seeded_db.commit()
    with pytest.raises(RuleViolation) as exc_info:
        budgets.set_budget(seeded_db, other.id, OCT, 3000)
    assert exc_info.value.code == "INACTIVE"


def test_unknown_account_is_not_found(seeded_db):
    with pytest.raises(NotFound):
        budgets.set_budget(seeded_db, 999999, OCT, 3000)


def test_same_month_updates_in_place_and_other_month_inserts(seeded_db):
    first = set_budget(seeded_db, "5100", 5000)
    again = set_budget(seeded_db, "5100", 5500)
    assert again.id == first.id and again.monthly_amount == 5500
    later = set_budget(seeded_db, "5100", 6000, "2026-12")
    assert later.id != first.id
    assert seeded_db.scalar(select(func.count()).select_from(Budget)) == 2


def test_failed_set_budget_changes_nothing(seeded_db):
    set_budget(seeded_db, "5100", 5000)
    with pytest.raises(RuleViolation):
        budgets.set_budget(seeded_db, acc(seeded_db, "5100"), OCT, 0)
    assert budgets.account_progress(seeded_db, acc(seeded_db, "5100"), OCT).budget == 5000


# --- T15、T16、T17 -------------------------------------------------------------------------


def test_t15_budget_progress_crosses_80_percent(seeded_db):
    set_budget(seeded_db, "5100", 5000)
    spend(seeded_db, TODAY, 3900)
    before = budgets.account_progress(seeded_db, acc(seeded_db, "5100"), OCT)
    assert (before.used, before.usage_pct, before.status) == (3900, 78, BudgetStatus.OK)

    spend(seeded_db, TODAY, 200)
    after = budgets.account_progress(seeded_db, acc(seeded_db, "5100"), OCT)
    assert (after.used, after.usage_pct, after.remaining) == (4100, 82, 900)
    assert after.status == BudgetStatus.WARN
    # V14 的「跨過」判斷（接線留 P3-3C）
    assert budgets.crossing(before.used, after.used, after.budget, 80) == 80


def test_t16_exclude_from_budget_leaves_budget_unchanged_but_not_expense_total(seeded_db):
    set_budget(seeded_db, "5200", 50000)
    spend(seeded_db, TODAY, 30000, "5201", exclude=True)
    assert used(seeded_db, "5200") == 0
    assert budgets.account_progress(seeded_db, acc(seeded_db, "5200"), OCT).status == "OK"
    # 月報的支出仍然包含這 30,000（規格 T16）
    assert class_total(seeded_db, "EXPENSE", D(2026, 10, 1), D(2026, 10, 31)) == 30000
    assert period_total(seeded_db, acc(seeded_db, "5200"), D(2026, 10, 1), D(2026, 10, 31)) == 30000
    # 沒勾選的照常計入
    spend(seeded_db, TODAY, 800, "5201")
    assert used(seeded_db, "5200") == 800


def test_t17_child_accounts_roll_up_into_first_level(seeded_db):
    set_budget(seeded_db, "5100", 5000)
    spend(seeded_db, TODAY, 1000, "5101")
    spend(seeded_db, TODAY, 300, "5102")
    progress = budgets.account_progress(seeded_db, acc(seeded_db, "5100"), OCT)
    assert (progress.used, progress.remaining, progress.usage_pct) == (1300, 3700, 26)


def test_other_first_level_accounts_do_not_mix(seeded_db):
    set_budget(seeded_db, "5100", 5000)
    set_budget(seeded_db, "5200", 5000)
    spend(seeded_db, TODAY, 400, "5101")
    spend(seeded_db, TODAY, 700, "5201")
    assert used(seeded_db, "5100") == 400
    assert used(seeded_db, "5200") == 700


def test_used_matches_period_total_when_nothing_is_excluded(seeded_db):
    set_budget(seeded_db, "5100", 5000)
    spend(seeded_db, TODAY, 1000, "5101")
    spend(seeded_db, TODAY, 300, "5102")
    expected = period_total(seeded_db, acc(seeded_db, "5100"), D(2026, 10, 1), D(2026, 10, 31))
    assert used(seeded_db, "5100") == expected == 1300


# --- 不依交易類型篩選、整筆交易排除 ---------------------------------------------------------------


def test_fee_and_interest_lines_count_toward_their_own_first_level_budget(seeded_db):
    set_budget(seeded_db, "5500", 1000)
    spend(seeded_db, TODAY, 2000, "5101", fee_amount=30, fee_account_id=acc(seeded_db, "5502"))
    assert used(seeded_db, "5500") == 30


def test_atm_withdrawal_only_fee_counts_toward_budget(seeded_db):
    """V1.1 回歸：提款本金（郵局 → 現金）不進任何預算，只有手續費計入它所屬的第一層預算。"""
    set_budget(seeded_db, "5100", 5000)
    set_budget(seeded_db, "5500", 1000)
    form = TransferForm(
        TODAY, 3000, acc(seeded_db, "1102"), acc(seeded_db, "1101"), 5, acc(seeded_db, "5502")
    )
    transactions.record_transfer(seeded_db, form, confirmed={"V10", "V11", "V13"})
    assert used(seeded_db, "5500") == 5
    assert used(seeded_db, "5100") == 0


def test_exclude_flag_excludes_fee_lines_of_that_transaction_too(seeded_db):
    set_budget(seeded_db, "5100", 5000)
    set_budget(seeded_db, "5500", 1000)
    spend(
        seeded_db,
        TODAY,
        2000,
        "5101",
        exclude=True,
        fee_amount=30,
        fee_account_id=acc(seeded_db, "5502"),
    )
    assert used(seeded_db, "5100") == 0
    assert used(seeded_db, "5500") == 0


def test_manual_expense_postings_are_counted(seeded_db):
    """不依 txn_type 篩選：MANUAL 的費用分錄（例如記在保險底下）照樣計入。"""
    set_budget(seeded_db, "5600", 3000)
    post_manual(seeded_db, TODAY, ("5602", 800), ("1102", -800))
    assert used(seeded_db, "5600") == 800


# --- 退款 ------------------------------------------------------------------------------


def test_refund_in_same_month_offsets_used(seeded_db):
    set_budget(seeded_db, "5100", 5000)
    original = spend(seeded_db, TODAY, 1000)
    refund(seeded_db, original, TODAY, 300)
    assert used(seeded_db, "5100") == 700


def test_refund_of_excluded_expense_stays_excluded(seeded_db):
    set_budget(seeded_db, "5100", 5000)
    original = spend(seeded_db, TODAY, 1000, exclude=True)
    refund(seeded_db, original, TODAY, 300)
    assert used(seeded_db, "5100") == 0
    spend(seeded_db, TODAY, 500)
    assert used(seeded_db, "5100") == 500


def test_refund_larger_than_the_months_spending_makes_used_negative(seeded_db):
    set_budget(seeded_db, "5100", 5000)
    original = spend(seeded_db, TODAY, 1000)  # 10 月
    refund(seeded_db, original, D(2026, 11, 2), 300)  # 11 月才退款
    november = budgets.account_progress(seeded_db, acc(seeded_db, "5100"), NOV)
    assert november.used == -300  # 不 clamp
    assert november.remaining == 5300
    assert november.usage_pct == 0
    assert november.status == BudgetStatus.OK
    assert used(seeded_db, "5100", OCT) == 1000  # 10 月不受影響


# --- 哪些分錄有效 ----------------------------------------------------------------------------


def test_deleted_transaction_is_excluded_and_restore_counts_again(seeded_db):
    set_budget(seeded_db, "5100", 5000)
    kept = spend(seeded_db, TODAY, 400)
    gone = spend(seeded_db, TODAY, 600)
    transactions.delete_transaction(seeded_db, gone)
    assert used(seeded_db, "5100") == 400
    transactions.restore_transaction(seeded_db, gone)
    assert used(seeded_db, "5100") == 1000
    assert kept != gone


def test_deleted_raw_transaction_is_excluded(seeded_db):
    set_budget(seeded_db, "5100", 5000)
    insert_raw_transaction(seeded_db, "2026-10-10", ("5101", 500), ("1101", -500), deleted=True)
    assert used(seeded_db, "5100") == 0


def test_draft_transaction_is_excluded(seeded_db):
    set_budget(seeded_db, "5100", 5000)
    insert_raw_transaction(seeded_db, "2026-10-10", ("5101", 500), ("1101", -500), status="DRAFT")
    insert_raw_transaction(seeded_db, "2026-10-11", ("5101", 200), ("1101", -200))
    assert used(seeded_db, "5100") == 200


# --- 月份區間：整個 calendar month，沒有 today 截止 ----------------------------------------------


def test_future_dated_posted_transaction_counts_in_its_month(seeded_db):
    """今天是 10/15；10/25 的 POSTED 交易照樣計入 10 月（沒有 as-of-today 截止）。"""
    set_budget(seeded_db, "5100", 5000)
    spend(seeded_db, D(2026, 10, 10), 100)
    spend(seeded_db, D(2026, 10, 25), 250)
    assert used(seeded_db, "5100", OCT) == 350


def test_future_month_counts_its_own_postings_only(seeded_db):
    set_budget(seeded_db, "5100", 5000)
    spend(seeded_db, D(2026, 10, 10), 100)
    spend(seeded_db, D(2026, 11, 5), 700)
    assert used(seeded_db, "5100", OCT) == 100
    assert used(seeded_db, "5100", NOV) == 700
    assert used(seeded_db, "5100", "2026-12") == 0


def test_month_boundaries_are_inclusive_and_neighbours_excluded(seeded_db):
    set_budget(seeded_db, "5100", 5000)
    for day, amount in [
        (D(2026, 9, 30), 1),
        (D(2026, 10, 1), 10),
        (D(2026, 10, 31), 100),
        (D(2026, 11, 1), 1000),
    ]:
        spend(seeded_db, day, amount)
    assert used(seeded_db, "5100", OCT) == 110
    assert used(seeded_db, "5100", NOV) == 1000


def test_leap_year_february_last_day_is_included(seeded_db, monkeypatch):
    freeze_today(monkeypatch, D(2028, 2, 10))
    budgets.set_budget(seeded_db, acc(seeded_db, "5100"), "2028-02", 5000)
    spend(seeded_db, D(2028, 2, 29), 40)
    spend(seeded_db, D(2028, 3, 1), 900)
    assert used(seeded_db, "5100", "2028-02") == 40


# --- 生效版本 ----------------------------------------------------------------------------


def test_version_in_force_is_latest_effective_from_not_after_the_month(seeded_db):
    set_budget(seeded_db, "5100", 5000, "2026-10")
    set_budget(seeded_db, "5100", 6000, "2026-12")
    aid = acc(seeded_db, "5100")
    progress = {m: budgets.account_progress(seeded_db, aid, m) for m in ("2026-10", NOV, "2026-12")}
    assert [(p.budget, p.effective_from) for p in progress.values()] == [
        (5000, "2026-10"),
        (5000, "2026-10"),
        (6000, "2026-12"),
    ]
    assert budgets.account_progress(seeded_db, aid, "2027-06").budget == 6000


def test_month_before_first_effective_month_has_no_budget(seeded_db):
    set_budget(seeded_db, "5100", 5000, "2026-11")
    aid = acc(seeded_db, "5100")
    assert budgets.account_progress(seeded_db, aid, OCT) is None
    assert budgets.monthly_progress(seeded_db, OCT) == ()
    assert budgets.account_progress(seeded_db, aid, NOV).budget == 5000


def test_changing_a_future_version_does_not_touch_earlier_months(seeded_db):
    set_budget(seeded_db, "5100", 5000, OCT)
    set_budget(seeded_db, "5100", 6000, "2026-12")
    set_budget(seeded_db, "5100", 7000, "2026-12")  # 同月版本原地更新
    aid = acc(seeded_db, "5100")
    assert budgets.account_progress(seeded_db, aid, NOV).budget == 5000
    assert budgets.account_progress(seeded_db, aid, "2026-12").budget == 7000


# --- monthly_progress ------------------------------------------------------------------------


def test_monthly_progress_defaults_to_current_month_and_sorts(seeded_db):
    set_budget(seeded_db, "5200", 4000)
    set_budget(seeded_db, "5100", 5000)
    spend(seeded_db, TODAY, 100, "5101")
    rows = budgets.monthly_progress(seeded_db)
    assert [(r.account_name, r.month, r.used) for r in rows] == [
        ("餐飲", OCT, 100),
        ("購物", OCT, 0),
    ]


def test_monthly_progress_empty_without_budgets(seeded_db):
    assert budgets.monthly_progress(seeded_db) == ()


def test_monthly_progress_rejects_bad_month(seeded_db):
    with pytest.raises(RuleViolation) as exc_info:
        budgets.monthly_progress(seeded_db, "2026-13")
    assert exc_info.value.code == "BUDGET_MONTH"


def test_inactive_account_with_budget_is_still_listed(seeded_db):
    other = make_top_level_expense(seeded_db)
    budgets.set_budget(seeded_db, other.id, OCT, 3000)
    post_manual(seeded_db, TODAY, ("5900", 700), ("1101", -700))
    other.is_active = False
    seeded_db.commit()
    [row] = budgets.monthly_progress(seeded_db)
    assert (row.account_name, row.is_active, row.used) == ("其他", False, 700)


def test_status_boundaries_through_progress(seeded_db):
    set_budget(seeded_db, "5100", 5000)
    spend(seeded_db, TODAY, 3999)
    assert budgets.account_progress(seeded_db, acc(seeded_db, "5100")).status == "OK"
    spend(seeded_db, TODAY, 1)  # 4000 = 80%
    assert budgets.account_progress(seeded_db, acc(seeded_db, "5100")).status == "WARN"
    spend(seeded_db, TODAY, 1000)  # 5000 = 100%
    over = budgets.account_progress(seeded_db, acc(seeded_db, "5100"))
    assert (over.status, over.remaining, over.usage_pct) == ("OVER", 0, 100)
    spend(seeded_db, TODAY, 500)
    assert budgets.account_progress(seeded_db, acc(seeded_db, "5100")).remaining == -500


# --- budget_alert_pct ------------------------------------------------------------------------


def test_alert_pct_defaults_to_80_when_setting_missing(seeded_db):
    seeded_db.delete(seeded_db.get(Setting, "budget_alert_pct"))
    seeded_db.commit()
    assert budgets.budget_alert_pct(seeded_db) == 80


def test_alert_pct_is_read_from_settings(seeded_db):
    seeded_db.get(Setting, "budget_alert_pct").value = "70"
    seeded_db.commit()
    set_budget(seeded_db, "5100", 5000)
    spend(seeded_db, TODAY, 3500)
    assert budgets.account_progress(seeded_db, acc(seeded_db, "5100")).status == "WARN"


@pytest.mark.parametrize("value", ["1", "99"])
def test_alert_pct_accepts_range_ends(seeded_db, value):
    seeded_db.get(Setting, "budget_alert_pct").value = value
    seeded_db.commit()
    assert budgets.budget_alert_pct(seeded_db) == int(value)


@pytest.mark.parametrize("value", ["0", "100", "150", "-5", "abc", "", "8.5", " 80", "0080x"])
def test_alert_pct_invalid_fails_loudly(seeded_db, value):
    seeded_db.get(Setting, "budget_alert_pct").value = value
    seeded_db.commit()
    with pytest.raises(InvalidSetting) as exc_info:
        budgets.budget_alert_pct(seeded_db)
    assert exc_info.value.key == "budget_alert_pct"
    set_budget(seeded_db, "5100", 5000)
    with pytest.raises(InvalidSetting):
        budgets.monthly_progress(seeded_db)


def test_invalid_setting_is_still_importable_from_validation():
    from app.services import validation

    assert validation.InvalidSetting is InvalidSetting


# --- 刪除科目 --------------------------------------------------------------------------------


def test_delete_account_with_budget_gives_friendly_error(seeded_db):
    other = make_top_level_expense(seeded_db)
    budgets.set_budget(seeded_db, other.id, OCT, 3000)
    with pytest.raises(RuleViolation) as exc_info:
        account_service.delete_account(seeded_db, other.id)
    assert exc_info.value.code == "BUDGET_ACCOUNT"
    assert seeded_db.get(Account, other.id) is not None


def test_delete_account_without_budget_still_works(seeded_db):
    other = make_top_level_expense(seeded_db)
    account_service.delete_account(seeded_db, other.id)
    assert seeded_db.get(Account, other.id) is None
