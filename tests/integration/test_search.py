"""services/search.py：交易列表與搜尋（P2 Step 3）。

金額 = 交易規模 magnitude（正數分錄加總）；摘要 = 全部符合條件的結果：
expense_net = SUM(EXPENSE 科目分錄)、income_total = -SUM(INCOME 科目分錄)。
"""

from datetime import date

import pytest
from sqlalchemy import func, select

from app.models import Merchant, MerchantAlias, PaymentMethod, Transaction
from app.services import search, transactions
from app.services.entries import (
    AdjustmentForm,
    CardPaymentForm,
    ExpenseForm,
    IncomeForm,
    RefundForm,
    TransferForm,
)
from app.services.search import SearchFilters
from tests.ledger_helpers import TODAY, acc, freeze_today, insert_raw_transaction, post_manual

OCT_1, OCT_31 = date(2026, 10, 1), date(2026, 10, 31)


@pytest.fixture(autouse=True)
def _today(monkeypatch):
    freeze_today(monkeypatch)


def pm(db, name="LINE Pay") -> int:
    return db.scalar(select(PaymentMethod.id).where(PaymentMethod.name == name))


def expense(db, amount, *, day=TODAY, merchant_id=None, fee=None, description=None) -> int:
    form = ExpenseForm(
        txn_date=day,
        amount=amount,
        account_id=acc(db, "5101"),
        payment_method_id=pm(db),
        merchant_id=merchant_id,
        fee_amount=fee,
        fee_account_id=acc(db, "5501") if fee else None,
        description=description,
    )
    return transactions.record_expense(db, form, confirmed=["V10", "V11", "V12"]).transaction_id


def income(db, amount, day=TODAY) -> int:
    form = IncomeForm(day, amount, acc(db, "4201"), acc(db, "1102"))
    return transactions.record_income(db, form, confirmed=["V10"]).transaction_id


def transfer(db, amount, fee=None, day=TODAY) -> int:
    form = TransferForm(
        day,
        amount,
        acc(db, "1102"),
        acc(db, "1103"),
        fee_amount=fee,
        fee_account_id=acc(db, "5501") if fee else None,
    )
    return transactions.record_transfer(db, form, confirmed=["V10"]).transaction_id


def card_payment(db, amount, day=TODAY) -> int:
    form = CardPaymentForm(day, amount, acc(db, "2101"), acc(db, "1103"))
    return transactions.record_card_payment(db, form, confirmed=["V10"]).transaction_id


def refund(db, original_id, amount, day=TODAY) -> int:
    form = RefundForm(original_id, day, amount, pm(db))
    return transactions.record_refund(db, form).transaction_id


def run(db, page=1, **filters) -> search.Page:
    return search.search(db, SearchFilters(**filters), page=page)


def ids(result) -> list[int]:
    return [r.id for r in result.rows]


# --- 排序與有效交易 ---------------------------------------------------------------------


def test_newest_first_and_same_day_by_id_desc(seeded_db):
    a = expense(seeded_db, 100, day=date(2026, 10, 3))
    b = expense(seeded_db, 100, day=date(2026, 10, 5))
    c = expense(seeded_db, 200, day=date(2026, 10, 3))
    assert ids(run(seeded_db)) == [b, c, a]


def test_deleted_and_draft_are_excluded(seeded_db):
    keep = expense(seeded_db, 100)
    gone = expense(seeded_db, 200)
    transactions.delete_transaction(seeded_db, gone)
    insert_raw_transaction(
        seeded_db, TODAY.isoformat(), ("5101", 300), ("1102", -300), status="DRAFT"
    )
    result = run(seeded_db)
    assert ids(result) == [keep]
    assert result.total_count == 1
    assert result.expense_net == 100


# --- 交易規模 magnitude ------------------------------------------------------------------


def test_magnitude_for_each_type(seeded_db):
    post_manual(seeded_db, TODAY, ("1101", 1000), ("4201", -1000))  # MANUAL
    original = expense(seeded_db, 1000, fee=15)
    amounts = {
        "EXPENSE": original,
        "INCOME": income(seeded_db, 3000),
        "TRANSFER": transfer(seeded_db, 5000, fee=15),
        "CARD_PAYMENT": card_payment(seeded_db, 2000),
        "REFUND": refund(seeded_db, original, 600),
        "ADJUSTMENT": transactions.record_adjustment(
            seeded_db, AdjustmentForm(TODAY, acc(seeded_db, "1101"), -200, "盤點短少")
        ).transaction_id,
    }
    rows = {r.id: r for r in run(seeded_db).rows}
    expected = {
        "EXPENSE": 1015,
        "INCOME": 3000,
        "TRANSFER": 5015,
        "CARD_PAYMENT": 2000,
        "REFUND": 600,
        "ADJUSTMENT": 200,
    }
    for txn_type, txn_id in amounts.items():
        assert rows[txn_id].txn_type == txn_type
        assert rows[txn_id].amount == expected[txn_type], txn_type
        assert rows[txn_id].amount > 0
    assert {r.type_label for r in rows.values()} >= {
        "支出",
        "收入",
        "轉帳",
        "繳卡費",
        "退款",
        "調整",
        "多分錄",
    }


def test_count_is_per_transaction_not_per_posting(seeded_db):
    expense(seeded_db, 1000, fee=15)  # 3 筆分錄
    transfer(seeded_db, 5000, fee=15)  # 3 筆分錄
    result = run(seeded_db)
    assert result.total_count == 2
    assert len(result.rows) == 2


# --- 合計 -------------------------------------------------------------------------------


def test_expense_net_and_income_total(seeded_db):
    original = expense(seeded_db, 1000, fee=15)  # 支出 1,015
    refund(seeded_db, original, 600)  # 沖回 600
    transfer(seeded_db, 5000, fee=15)  # 只有手續費 15 是支出
    card_payment(seeded_db, 2000)  # 不是支出
    income(seeded_db, 3000)
    result = run(seeded_db)
    assert result.total_count == 5
    assert result.expense_net == 1015 - 600 + 15
    assert result.income_total == 3000


def test_transfer_and_card_payment_alone_are_not_spending(seeded_db):
    transfer(seeded_db, 5000)
    card_payment(seeded_db, 2000)
    result = run(seeded_db)
    assert (result.total_count, result.expense_net, result.income_total) == (2, 0, 0)


def test_refund_only_gives_negative_expense_net(seeded_db):
    original = expense(seeded_db, 1000, day=date(2026, 10, 1))
    refund(seeded_db, original, 600, day=date(2026, 10, 10))
    result = run(seeded_db, date_from=date(2026, 10, 10), date_to=date(2026, 10, 10))
    assert result.total_count == 1
    assert result.expense_net == -600


def test_payroll_counts_by_account_class(seeded_db):
    from app.services.entries import ManualForm, ManualLine

    lines = (
        ManualLine(acc(seeded_db, "1102"), 28850),
        ManualLine(acc(seeded_db, "5601"), 700),
        ManualLine(acc(seeded_db, "5602"), 450),
        ManualLine(acc(seeded_db, "4101"), -30000),
    )
    transactions.record_payroll(seeded_db, ManualForm(TODAY, lines))
    result = run(seeded_db)
    assert (result.expense_net, result.income_total) == (1150, 30000)
    assert result.rows[0].amount == 30000


# --- 篩選 -------------------------------------------------------------------------------


def test_date_range_is_inclusive(seeded_db):
    a = expense(seeded_db, 100, day=date(2026, 9, 30))
    b = expense(seeded_db, 100, day=OCT_1)
    c = expense(seeded_db, 100, day=OCT_31)
    assert ids(run(seeded_db, date_from=OCT_1, date_to=OCT_31)) == [c, b]
    assert ids(run(seeded_db)) == [c, b, a]


def test_amount_range_uses_magnitude_inclusive(seeded_db):
    small = expense(seeded_db, 99)
    with_fee = expense(seeded_db, 90, fee=10)  # magnitude 100
    big = expense(seeded_db, 500)
    assert ids(run(seeded_db, min_amount=100, max_amount=500)) == [big, with_fee]
    assert ids(run(seeded_db, max_amount=99)) == [small]


def test_merchant_canonical_alias_and_fullwidth(seeded_db):
    seven = Merchant(name="7-11")
    seven.aliases.append(MerchantAlias(alias="統一超商", alias_normalized="統一超商"))
    other = Merchant(name="全家")
    seeded_db.add_all([seven, other])
    seeded_db.commit()
    mine = expense(seeded_db, 85, merchant_id=seven.id)
    expense(seeded_db, 60, merchant_id=other.id)
    expense(seeded_db, 70)
    for name in ("7-11", "統一 超商", "７－１１"):
        result = run(seeded_db, merchant_name=name)
        assert ids(result) == [mine], name
        assert result.expense_net == 85
        assert result.rows[0].merchant_name == "7-11"


def test_unknown_merchant_returns_nothing_and_creates_nothing(seeded_db):
    expense(seeded_db, 85)
    result = run(seeded_db, merchant_name="不存在的店")
    assert result.merchant_not_found
    assert (result.total_count, result.rows, result.expense_net) == (0, (), 0)
    assert seeded_db.scalar(select(func.count()).select_from(Merchant)) == 0


# --- 分頁 -------------------------------------------------------------------------------


def test_pagination_and_full_result_summary(seeded_db):
    made = [expense(seeded_db, 10 + i, day=OCT_1) for i in range(51)]
    first = run(seeded_db)
    assert (first.total_count, first.page, first.page_count, len(first.rows)) == (51, 1, 2, 50)
    second = run(seeded_db, page=2)
    assert ids(second) == [made[0]]
    assert ids(first) + ids(second) == list(reversed(made))
    expected_net = sum(10 + i for i in range(51))
    assert first.expense_net == second.expense_net == expected_net
    assert run(seeded_db, page=99).page == 2  # 超出範圍時停在最後一頁


def test_empty_result(seeded_db):
    result = run(seeded_db)
    assert (result.total_count, result.page, result.page_count, result.rows) == (0, 1, 1, ())
    assert not result.merchant_not_found
    assert seeded_db.scalar(select(func.count()).select_from(Transaction)) == 0
