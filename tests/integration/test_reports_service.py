"""月報 service（P3-4；規格 8.2、T16）。

時鐘固定在 2026-10-15（tests.ledger_helpers.TODAY）。
重點：收入／支出依科目大類（不依交易類型）；支出含 exclude_from_budget；預算已用不含；
excluded_amount = actual − budget.used 有正負號；整個 calendar month；所有加總走 active_postings()。
"""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models import PaymentMethod, Setting, Transaction
from app.services import accounts as account_service
from app.services import budgets, ledger_queries, reports, transactions
from app.services.entries import (
    AdjustmentForm,
    CardPaymentForm,
    ExpenseForm,
    IncomeForm,
    ManualForm,
    ManualLine,
    OpeningForm,
    OpeningLine,
    RefundForm,
    TransferForm,
)
from app.services.errors import RuleViolation
from tests.ledger_helpers import TODAY, acc, freeze_today, insert_raw_transaction, post_manual

D = date
OCT, SEP, NOV = "2026-10", "2026-09", "2026-11"
ALL = {"V10", "V11", "V13"}


@pytest.fixture(autouse=True)
def _today(monkeypatch):
    freeze_today(monkeypatch)


def pm(db, name="LINE Pay") -> int:
    return db.scalar(select(PaymentMethod.id).where(PaymentMethod.name == name))


def spend(db, day, amount, code="5101", *, exclude=False, method="LINE Pay") -> int:
    form = ExpenseForm(
        txn_date=day,
        amount=amount,
        account_id=acc(db, code),
        payment_method_id=pm(db, method),
        exclude_from_budget=exclude,
    )
    return transactions.record_expense(db, form, confirmed=ALL).transaction_id


def refund(db, original, day, amount) -> int:
    form = RefundForm(original, day, amount, pm(db))
    return transactions.record_refund(db, form, confirmed=ALL).transaction_id


def earn(db, day, amount, code="4201", to="1102") -> int:
    form = IncomeForm(day, amount, acc(db, code), acc(db, to))
    return transactions.record_income(db, form, confirmed=ALL).transaction_id


def report(db, month=OCT):
    return reports.monthly_report(db, month)


def row(rep, code_name):
    return next(r for r in rep.expense_rows if r.name == code_name)


# --- 收入、支出、結餘 --------------------------------------------------------------------------


def test_income_expense_balance(seeded_db):
    earn(seeded_db, TODAY, 50000)
    spend(seeded_db, TODAY, 1200)
    spend(seeded_db, D(2026, 10, 3), 300, "5201")
    rep = report(seeded_db)
    assert (rep.income, rep.expense, rep.balance) == (50000, 1500, 48500)


def test_income_is_by_account_class_not_txn_type(seeded_db):
    earn(seeded_db, TODAY, 1000)
    post_manual(seeded_db, TODAY, ("1102", 700), ("4201", -700))  # MANUAL 也算收入
    assert report(seeded_db).income == 1700


def test_income_uses_absolute_value_per_spec_literal(seeded_db):
    """規格 8.2：收入 = 收入科目分錄加總的絕對值。收入被沖銷成淨負值的月份仍顯示絕對值。"""
    post_manual(seeded_db, TODAY, ("4201", 500), ("1101", -500))
    rep = report(seeded_db)
    assert rep.income == 500
    assert rep.balance == 500


def test_refund_offsets_expense_in_same_month(seeded_db):
    original = spend(seeded_db, TODAY, 1000)
    refund(seeded_db, original, TODAY, 300)
    rep = report(seeded_db)
    assert rep.expense == 700
    assert row(rep, "餐飲").actual == 700


def test_refund_in_later_month_can_make_expense_negative(seeded_db):
    original = spend(seeded_db, D(2026, 9, 20), 1000)
    refund(seeded_db, original, TODAY, 400)
    assert report(seeded_db, SEP).expense == 1000
    rep = report(seeded_db, OCT)
    assert rep.expense == -400  # 不 clamp
    assert row(rep, "餐飲").actual == -400


def test_transfer_is_not_income_or_expense(seeded_db):
    earn(seeded_db, TODAY, 5000)
    form = TransferForm(TODAY, 2000, acc(seeded_db, "1102"), acc(seeded_db, "1103"))
    transactions.record_transfer(seeded_db, form, confirmed=ALL)
    rep = report(seeded_db)
    assert (rep.income, rep.expense) == (5000, 0)


def test_transfer_fee_counts_as_expense(seeded_db):
    earn(seeded_db, TODAY, 5000)
    form = TransferForm(
        TODAY, 2000, acc(seeded_db, "1102"), acc(seeded_db, "1103"), 15, acc(seeded_db, "5501")
    )
    transactions.record_transfer(seeded_db, form, confirmed=ALL)
    assert report(seeded_db).expense == 15


def test_atm_withdrawal_only_fee_is_expense(seeded_db):
    """V1.1 回歸：ATM 提款 3,000、手續費 5（郵局 → 現金）。

    曾被錯記成支出 3,005，月支出虛增 3,000；正確是資產轉移，只有手續費是支出。"""
    earn(seeded_db, TODAY, 10000)
    cash, post_office, fee = acc(seeded_db, "1101"), acc(seeded_db, "1102"), acc(seeded_db, "5501")
    before = {a: ledger_queries.balance(seeded_db, a) for a in (cash, post_office)}
    form = TransferForm(TODAY, 3000, post_office, cash, 5, fee)
    result = transactions.record_transfer(seeded_db, form, confirmed=ALL)
    txn = seeded_db.get(Transaction, result.transaction_id)
    by_account = {p.account_id: p.amount for p in txn.postings}
    assert txn.txn_type == "TRANSFER"
    assert by_account == {cash: 3000, fee: 5, post_office: -3005}
    assert sum(by_account.values()) == 0
    assert ledger_queries.balance(seeded_db, cash) - before[cash] == 3000
    assert ledger_queries.balance(seeded_db, post_office) - before[post_office] == -3005
    rep = report(seeded_db)
    assert (rep.income, rep.expense) == (10000, 5)


def test_atm_withdrawal_without_fee_is_pure_asset_transfer(seeded_db):
    cash, post_office = acc(seeded_db, "1101"), acc(seeded_db, "1102")
    form = TransferForm(TODAY, 3000, post_office, cash)
    result = transactions.record_transfer(seeded_db, form, confirmed=ALL)
    txn = seeded_db.get(Transaction, result.transaction_id)
    assert {p.account_id: p.amount for p in txn.postings} == {cash: 3000, post_office: -3000}
    rep = report(seeded_db)
    assert (rep.income, rep.expense) == (0, 0)


def test_card_payment_does_not_double_count_expense(seeded_db):
    earn(seeded_db, TODAY, 9000, to="1103")
    spend(seeded_db, TODAY, 1000, method="聯邦信用卡")
    form = CardPaymentForm(TODAY, 1000, acc(seeded_db, "2101"), acc(seeded_db, "1103"))
    transactions.record_card_payment(seeded_db, form, confirmed=ALL)
    assert report(seeded_db).expense == 1000


def test_opening_and_adjustment_are_neither_income_nor_expense(seeded_db):
    open_form = OpeningForm(D(2026, 10, 1), (OpeningLine(acc(seeded_db, "1101"), 5000),))
    transactions.record_opening(seeded_db, open_form)
    adj = AdjustmentForm(TODAY, acc(seeded_db, "1101"), -200, "盤點短少")
    transactions.record_adjustment(seeded_db, adj, confirmed=ALL)
    rep = report(seeded_db)
    assert (rep.income, rep.expense, rep.balance) == (0, 0, 0)
    assert rep.adjustment_total == -200


def test_fee_interest_payroll_and_manual_expense_all_count(seeded_db):
    post_manual(seeded_db, TODAY, ("5503", 120), ("1102", -120))  # 分期利息（手動）
    post_manual(seeded_db, TODAY, ("5502", 30), ("1102", -30))  # 手續費（手動）
    lines = (
        ("1102", 27_050),
        ("5601", 700),
        ("5602", 450),
        ("1201", 1800),
        ("4101", -30_000),
    )
    form = ManualForm(TODAY, tuple(ManualLine(acc(seeded_db, c), a) for c, a in lines), "薪資")
    transactions.record_payroll(seeded_db, form, confirmed=ALL)
    rep = report(seeded_db)
    assert rep.income == 30_000
    assert rep.expense == 120 + 30 + 700 + 450  # 1201 勞退自提是資產，不算費用
    assert rep.balance == 30_000 - 1300


# --- deleted／DRAFT、日期 ---------------------------------------------------------------------


def test_deleted_and_draft_are_excluded_and_restore_brings_back(seeded_db):
    gone = spend(seeded_db, TODAY, 600)
    spend(seeded_db, TODAY, 400)
    insert_raw_transaction(seeded_db, "2026-10-10", ("5101", 111), ("1101", -111), status="DRAFT")
    insert_raw_transaction(seeded_db, "2026-10-10", ("5101", 222), ("1101", -222), deleted=True)
    insert_raw_transaction(seeded_db, "2026-10-10", ("1101", 333), ("4201", -333), status="DRAFT")
    assert report(seeded_db).expense == 1000
    assert report(seeded_db).income == 0
    transactions.delete_transaction(seeded_db, gone)
    assert report(seeded_db).expense == 400
    transactions.restore_transaction(seeded_db, gone)
    assert report(seeded_db).expense == 1000


def test_future_dated_posted_transaction_counts_in_its_own_month(seeded_db):
    spend(seeded_db, D(2026, 10, 25), 250)  # 今天是 10/15
    spend(seeded_db, D(2026, 11, 5), 700)
    assert report(seeded_db, OCT).expense == 250
    assert report(seeded_db, NOV).expense == 700


def test_month_boundaries_are_isolated(seeded_db):
    spend(seeded_db, D(2026, 9, 30), 1)
    spend(seeded_db, D(2026, 10, 1), 10)
    spend(seeded_db, D(2026, 10, 31), 100)
    spend(seeded_db, D(2026, 11, 1), 1000)
    assert report(seeded_db, SEP).expense == 1
    assert report(seeded_db, OCT).expense == 110
    assert report(seeded_db, NOV).expense == 1000


# --- 各科目支出、第二層明細 --------------------------------------------------------------------


def test_first_level_rolls_up_children_and_lists_direct_children(seeded_db):
    spend(seeded_db, TODAY, 1000, "5101")
    spend(seeded_db, TODAY, 300, "5102")
    spend(seeded_db, TODAY, 80, "5201")
    rep = report(seeded_db)
    food = row(rep, "餐飲")
    assert food.actual == 1300
    assert [(c.name, c.amount) for c in food.children] == [("正餐", 1000), ("飲料", 300)]
    assert food.own_amount == 0
    assert row(rep, "購物").actual == 80


def test_zero_children_are_not_listed(seeded_db):
    spend(seeded_db, TODAY, 1000, "5101")
    assert [c.name for c in row(report(seeded_db), "餐飲").children] == ["正餐"]


def test_third_level_is_folded_into_second_level(seeded_db):
    account_service.create_account(
        seeded_db, code="5111", name="早餐", parent_id=acc(seeded_db, "5101")
    )
    spend(seeded_db, TODAY, 90, "5111")
    food = row(report(seeded_db), "餐飲")
    assert food.actual == 90
    assert [(c.name, c.amount) for c in food.children] == [("正餐", 90)]  # 不另列第三層


def test_first_level_direct_postings_show_as_own_amount(seeded_db):
    # LedgerService 不允許記在非末層科目，所以用 raw 寫入製造這個（不應出現的）狀態
    insert_raw_transaction(seeded_db, "2026-10-10", ("5100", 70), ("1101", -70))
    spend(seeded_db, TODAY, 1000, "5101")
    food = row(report(seeded_db), "餐飲")
    assert food.actual == 1070
    assert food.own_amount == 70
    assert sum(c.amount for c in food.children) + food.own_amount == food.actual


def test_first_level_rows_add_up_to_total_expense(seeded_db):
    spend(seeded_db, TODAY, 1000, "5101")
    spend(seeded_db, TODAY, 80, "5201")
    spend(seeded_db, TODAY, 500, "5301", exclude=True)
    post_manual(seeded_db, TODAY, ("5503", 120), ("1102", -120))
    rep = report(seeded_db)
    assert sum(r.actual for r in rep.expense_rows) == rep.expense == 1700


# --- 預算對照 ---------------------------------------------------------------------------------


def test_t16_actual_includes_excluded_but_budget_progress_does_not(seeded_db):
    budgets.set_budget(seeded_db, acc(seeded_db, "5200"), OCT, 50000)
    spend(seeded_db, TODAY, 30000, "5201", exclude=True)
    rep = report(seeded_db)
    assert rep.expense == 30000  # 月報支出仍包含 30,000
    shopping = row(rep, "購物")
    assert shopping.actual == 30000
    assert shopping.budget.progress.used == 0  # 預算進度不變
    assert shopping.budget.state == "ok"
    assert shopping.excluded_amount == 30000  # 正差額


def test_excluded_amount_is_zero_when_nothing_is_excluded(seeded_db):
    budgets.set_budget(seeded_db, acc(seeded_db, "5100"), OCT, 5000)
    spend(seeded_db, TODAY, 1000, "5101")
    food = row(report(seeded_db), "餐飲")
    assert food.excluded_amount == 0
    assert food.actual == food.budget.progress.used == 1000


def test_excluded_refund_gives_negative_excluded_amount(seeded_db):
    budgets.set_budget(seeded_db, acc(seeded_db, "5200"), OCT, 10000)
    original = spend(seeded_db, D(2026, 9, 20), 1000, "5201", exclude=True)
    refund(seeded_db, original, TODAY, 300)  # 排除交易的退款也排除，本月實際支出為 −300
    shopping = row(report(seeded_db), "購物")
    assert shopping.actual == -300
    assert shopping.budget.progress.used == 0
    assert shopping.excluded_amount == -300  # 負差額


@pytest.mark.parametrize(
    "plain, excluded, refunded",
    [(0, 0, 0), (5000, 0, 0), (0, 30000, 0), (5000, 2000, 300), (800, 0, 0), (0, 400, 400)],
)
def test_actual_minus_budget_used_always_equals_excluded_amount(
    seeded_db, plain, excluded, refunded
):
    budgets.set_budget(seeded_db, acc(seeded_db, "5200"), OCT, 10000)
    if plain:
        spend(seeded_db, TODAY, plain, "5201")
    original = spend(seeded_db, D(2026, 10, 2), excluded, "5201", exclude=True) if excluded else 0
    if refunded:
        refund(seeded_db, original, TODAY, refunded)
    shopping = row(report(seeded_db), "購物")
    assert shopping.actual - shopping.budget.progress.used == shopping.excluded_amount
    assert shopping.excluded_amount == excluded - refunded


def test_no_budget_row_still_listed_with_actual(seeded_db):
    spend(seeded_db, TODAY, 80, "5201")
    shopping = row(report(seeded_db), "購物")
    assert shopping.budget is None
    assert shopping.excluded_amount == 0
    assert shopping.actual == 80


def test_budget_with_zero_actual_is_listed_and_zero_without_budget_is_omitted(seeded_db):
    budgets.set_budget(seeded_db, acc(seeded_db, "5300"), OCT, 2000)
    rep = report(seeded_db)
    assert [r.name for r in rep.expense_rows] == ["娛樂"]
    assert rep.expense_rows[0].actual == 0
    assert rep.expense_rows[0].budget.progress.used == 0


def test_inactive_account_is_listed_when_it_has_actual_or_budget(seeded_db):
    spend(seeded_db, TODAY, 80, "5201")
    budgets.set_budget(seeded_db, acc(seeded_db, "5300"), OCT, 2000)
    for code in ("5201", "5200", "5301", "5303", "5300"):  # 先停子科目再停第一層
        account_service.deactivate_account(seeded_db, acc(seeded_db, code))
    named = {r.name: r for r in report(seeded_db).expense_rows}
    assert named["購物"].is_active is False and named["購物"].actual == 80
    assert named["購物"].children[0].is_active is False
    assert named["娛樂"].is_active is False and named["娛樂"].budget is not None


def test_past_month_uses_the_budget_version_in_force_then(seeded_db):
    budgets.set_budget(seeded_db, acc(seeded_db, "5100"), OCT, 5000)
    budgets.set_budget(seeded_db, acc(seeded_db, "5100"), NOV, 8000)
    assert row(report(seeded_db, OCT), "餐飲").budget.progress.budget == 5000
    assert row(report(seeded_db, NOV), "餐飲").budget.progress.budget == 8000
    assert report(seeded_db, SEP).expense_rows == ()  # 第一個版本之前沒有預算


def test_invalid_alert_pct_only_breaks_budget_column(seeded_db):
    budgets.set_budget(seeded_db, acc(seeded_db, "5100"), OCT, 5000)
    spend(seeded_db, TODAY, 1000, "5101")
    earn(seeded_db, TODAY, 3000)
    seeded_db.get(Setting, "budget_alert_pct").value = "abc"
    seeded_db.commit()
    rep = report(seeded_db)
    assert rep.budget_error and "budget_alert_pct" in rep.budget_error
    food = row(rep, "餐飲")
    assert food.budget is None and food.excluded_amount == 0
    assert (rep.income, rep.expense, food.actual) == (3000, 1000, 1000)  # 其他部分照常


# --- 帳戶月初／月底、淨資產 ------------------------------------------------------------------


def balances(rep):
    return {r.name: (r.start, r.end) for r in rep.accounts}


def test_account_start_is_previous_month_end_and_end_follows_month(seeded_db):
    earn(seeded_db, D(2026, 9, 10), 1000, to="1101")
    earn(seeded_db, D(2026, 10, 5), 200, to="1101")
    earn(seeded_db, D(2026, 11, 2), 30, to="1101")
    sep, octo, nov = (report(seeded_db, m) for m in (SEP, OCT, NOV))
    assert balances(sep)["現金"] == (0, 1000)
    assert balances(octo)["現金"] == (1000, 1200)
    assert balances(nov)["現金"] == (1200, 1230)
    assert balances(octo)["現金"][0] == balances(sep)["現金"][1]


def test_all_asset_and_liability_leaf_accounts_are_listed_not_only_home(seeded_db):
    names = [r.name for r in report(seeded_db).accounts]
    assert "勞退個人專戶" in names and "預付所得稅" in names  # show_on_home = false
    assert names.index("聯邦信用卡") > names.index("聯邦活存")  # 資產在前、負債在後
    assert "期初權益" not in names and "薪資" not in names


def test_only_leaf_accounts_are_listed(seeded_db):
    parent = account_service.create_account(
        seeded_db, code="1110", name="證券", account_class="ASSET", subtype="BANK"
    )
    account_service.create_account(
        seeded_db, code="1111", name="證券戶", parent_id=parent.id, subtype="BANK"
    )
    post_manual(seeded_db, TODAY, ("1111", 500), ("4201", -500))
    rep = report(seeded_db)
    names = [r.name for r in rep.accounts]
    assert "證券戶" in names and "證券" not in names
    assert balances(rep)["證券戶"] == (0, 500)


def test_credit_card_liability_is_normal_direction_debt(seeded_db):
    spend(seeded_db, TODAY, 1000, method="聯邦信用卡")
    card = next(r for r in report(seeded_db).accounts if r.name == "聯邦信用卡")
    assert (card.start, card.end) == (0, 1000)
    assert card.end_is_debt and not card.start_is_debt
    assert card.class_label == "負債"


def test_overpaid_card_is_negative_and_not_debt(seeded_db):
    earn(seeded_db, TODAY, 5000, to="1103")
    form = CardPaymentForm(TODAY, 700, acc(seeded_db, "2101"), acc(seeded_db, "1103"))
    transactions.record_card_payment(seeded_db, form, confirmed=ALL)
    card = next(r for r in report(seeded_db).accounts if r.name == "聯邦信用卡")
    assert card.end == -700 and not card.end_is_debt


def test_inactive_account_listed_only_with_a_balance(seeded_db):
    post_manual(seeded_db, TODAY, ("1201", 300), ("4201", -300))
    account_service.deactivate_account(seeded_db, acc(seeded_db, "1201"))
    account_service.deactivate_account(seeded_db, acc(seeded_db, "1301"))
    rep = report(seeded_db)
    pension = next(r for r in rep.accounts if r.name == "勞退個人專戶")
    assert pension.is_active is False and (pension.start, pension.end) == (0, 300)
    assert "預付所得稅" not in [r.name for r in rep.accounts]


def test_net_worth_matches_rows_without_double_counting(seeded_db):
    parent = account_service.create_account(
        seeded_db, code="1110", name="證券", account_class="ASSET", subtype="BANK"
    )
    account_service.create_account(
        seeded_db, code="1111", name="證券戶", parent_id=parent.id, subtype="BANK"
    )
    earn(seeded_db, D(2026, 9, 10), 4000, to="1101")
    post_manual(seeded_db, D(2026, 10, 2), ("1111", 500), ("4201", -500))
    spend(seeded_db, TODAY, 1000, method="聯邦信用卡")
    spend(seeded_db, TODAY, 100, method="現金")
    rep = report(seeded_db)
    assets_end = sum(r.end for r in rep.accounts if r.account_class == "ASSET")
    debts_end = sum(r.end for r in rep.accounts if r.account_class == "LIABILITY")
    assert rep.net_worth_end == assets_end - debts_end == 4000 + 500 - 100 - 1000
    assets_start = sum(r.start for r in rep.accounts if r.account_class == "ASSET")
    debts_start = sum(r.start for r in rep.accounts if r.account_class == "LIABILITY")
    assert rep.net_worth_start == assets_start - debts_start == 4000


def test_net_worth_change_equals_balance_plus_adjustment_without_opening(seeded_db):
    earn(seeded_db, D(2026, 9, 10), 4000, to="1101")
    earn(seeded_db, D(2026, 10, 3), 10000)
    spend(seeded_db, TODAY, 2500)
    adj = AdjustmentForm(TODAY, acc(seeded_db, "1101"), -150, "盤點短少")
    transactions.record_adjustment(seeded_db, adj, confirmed=ALL)
    rep = report(seeded_db)
    assert rep.adjustment_total == -150
    assert rep.net_worth_end - rep.net_worth_start == rep.balance + rep.adjustment_total


def test_opening_month_is_the_known_exception_to_the_identity(seeded_db):
    open_form = OpeningForm(D(2026, 10, 1), (OpeningLine(acc(seeded_db, "1101"), 5000),))
    transactions.record_opening(seeded_db, open_form)
    earn(seeded_db, TODAY, 100)
    rep = report(seeded_db)
    change = rep.net_worth_end - rep.net_worth_start
    assert change == rep.balance + rep.adjustment_total + 5000  # 期初餘額讓淨資產上升


# --- 儲蓄率 -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "income, balance, expected",
    [
        (10000, 7500, "75.0"),
        (10000, -500, "-5.0"),
        (3, 1, "33.3"),
        (3, 2, "66.7"),
        (2000, 1, "0.1"),  # 0.05 → half up
        (1000, 3, "0.3"),
        (100000, 1, "0.0"),
        (100000, -1, "0.0"),  # 不出現 -0.0
        (10000, 10000, "100.0"),
    ],
)
def test_saving_rate_rounding(income, balance, expected):
    rate = reports.saving_rate(income, balance)
    assert isinstance(rate, Decimal)
    assert str(rate) == expected


@pytest.mark.parametrize("income", [0, -5])
def test_saving_rate_is_none_without_income(income):
    assert reports.saving_rate(income, 100) is None
    assert reports.saving_rate(income, -100) is None


def test_report_saving_rate_values(seeded_db):
    earn(seeded_db, TODAY, 10000)
    spend(seeded_db, TODAY, 2500)
    assert report(seeded_db).saving_rate == Decimal("75.0")


def test_report_saving_rate_negative_when_overspent(seeded_db):
    earn(seeded_db, TODAY, 1000)
    spend(seeded_db, TODAY, 1500)
    assert report(seeded_db).saving_rate == Decimal("-50.0")


def test_report_saving_rate_none_when_no_income(seeded_db):
    spend(seeded_db, TODAY, 1500)
    rep = report(seeded_db)
    assert rep.saving_rate is None and rep.balance == -1500


# --- 空月份、月份參數 --------------------------------------------------------------------------


def test_empty_month(seeded_db):
    rep = report(seeded_db, "2026-01")
    assert (rep.income, rep.expense, rep.balance, rep.adjustment_total) == (0, 0, 0, 0)
    assert rep.saving_rate is None
    assert rep.expense_rows == () and rep.budget_error is None
    assert rep.net_worth_start == rep.net_worth_end == 0
    assert rep.accounts and all(r.start == r.end == 0 for r in rep.accounts)


def test_default_month_is_the_current_month(seeded_db):
    assert reports.monthly_report(seeded_db).month == OCT
    assert reports.monthly_report(seeded_db, None).month == OCT
    assert reports.monthly_report(seeded_db, "").month == OCT


@pytest.mark.parametrize(
    "month",
    [
        "2026-13",
        "2026-00",
        "2026-1",
        "26-10",
        "abc",
        "2026/10",
        " 2026-10",
        "2026-10 ",
        "２０２６-１０",
        "20261",
        "2026-100",
        "0000-05",
        "9999-12",
        "0001-01",
    ],
)
def test_invalid_month_raises_report_month(seeded_db, month):
    with pytest.raises(RuleViolation) as exc_info:
        reports.monthly_report(seeded_db, month)
    assert exc_info.value.code == "REPORT_MONTH"


@pytest.mark.parametrize(
    "month", ["1999-12", "2000-01", "2099-12", "2100-01", "0001-02", "9999-11"]
)
def test_any_four_digit_year_is_accepted_no_product_year_limit(seeded_db, month):
    rep = reports.monthly_report(seeded_db, month)
    assert rep.month == month and rep.income == rep.expense == 0


def test_future_month_is_allowed(seeded_db):
    assert reports.monthly_report(seeded_db, "2027-03").month == "2027-03"


@pytest.mark.parametrize(
    "month, prev_month, next_month",
    [
        ("2026-10", "2026-09", "2026-11"),
        ("2026-01", "2025-12", "2026-02"),
        ("2026-12", "2026-11", "2027-01"),
        ("0001-02", None, "0001-03"),
        ("9999-11", "9999-10", None),
    ],
)
def test_prev_and_next_month(seeded_db, month, prev_month, next_month):
    rep = reports.monthly_report(seeded_db, month)
    assert (rep.prev_month, rep.next_month) == (prev_month, next_month)


def test_end_includes_future_only_when_month_ends_after_today(seeded_db):
    assert report(seeded_db, OCT).end_includes_future is True
    assert report(seeded_db, NOV).end_includes_future is True
    assert report(seeded_db, SEP).end_includes_future is False


def test_current_month_end_balance_includes_later_dated_transactions(seeded_db):
    earn(seeded_db, D(2026, 10, 25), 700, to="1101")  # 今天是 10/15
    assert balances(report(seeded_db, OCT))["現金"] == (0, 700)


# --- 架構 ------------------------------------------------------------------------------------


def test_reports_service_does_not_rewrite_active_posting_conditions():
    from pathlib import Path

    source = Path(reports.__file__).read_text(encoding="utf-8")
    for forbidden in ("deleted_at", "TxnStatus", "status ==", "Posting", "ledger_repo"):
        assert forbidden not in source
