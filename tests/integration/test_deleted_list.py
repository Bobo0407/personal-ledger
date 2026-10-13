"""已刪除交易列表（services/search.deleted_page；P2 Step 4B）與有效／已刪除的隔離。"""

from datetime import datetime

import pytest
from sqlalchemy import select

from app import clock
from app.models import PaymentMethod
from app.services import search, transactions
from app.services.entries import ExpenseForm, IncomeForm
from app.services.search import SearchFilters
from tests.ledger_helpers import TODAY, acc, freeze_today


@pytest.fixture(autouse=True)
def _today(monkeypatch):
    freeze_today(monkeypatch)


def expense(db, amount, **kwargs) -> int:
    pm = db.scalar(select(PaymentMethod.id).where(PaymentMethod.name == "LINE Pay"))
    form = ExpenseForm(
        txn_date=TODAY, amount=amount, account_id=acc(db, "5101"), payment_method_id=pm, **kwargs
    )
    return transactions.record_expense(db, form, confirmed=["V10", "V11", "V12"]).transaction_id


def delete_at(db, monkeypatch, txn_id, hhmm: str) -> None:
    h, m = map(int, hhmm.split(":"))
    fixed = datetime(2026, 10, 15, h, m, tzinfo=clock.APP_TIMEZONE)
    monkeypatch.setattr(clock, "now", lambda: fixed)
    transactions.delete_transaction(db, txn_id)


def ids(page) -> list[int]:
    return [item.row.id for item in page.rows]


def test_only_deleted_most_recent_first(seeded_db, monkeypatch):
    a, b, c = expense(seeded_db, 100), expense(seeded_db, 200), expense(seeded_db, 300)
    delete_at(seeded_db, monkeypatch, a, "10:00")
    delete_at(seeded_db, monkeypatch, c, "09:00")
    page = search.deleted_page(seeded_db)
    assert ids(page) == [a, c]
    assert page.total_count == 2
    assert [i.deleted_at.strftime("%H:%M") for i in page.rows] == ["10:00", "09:00"]
    assert b not in ids(page)


def test_same_deleted_at_ordered_by_id_desc(seeded_db):
    a, b = expense(seeded_db, 100), expense(seeded_db, 200)
    transactions.delete_transaction(seeded_db, a)
    transactions.delete_transaction(seeded_db, b)  # clock 固定：同一個 deleted_at
    assert ids(search.deleted_page(seeded_db)) == [b, a]


def test_row_magnitude_is_display_only(seeded_db):
    txn_id = expense(seeded_db, 1000, fee_amount=15, fee_account_id=acc(seeded_db, "5501"))
    transactions.delete_transaction(seeded_db, txn_id)
    item = search.deleted_page(seeded_db).rows[0]
    assert (item.row.amount, item.row.type_label) == (1015, "支出")
    assert not hasattr(search.deleted_page(seeded_db), "expense_net")
    assert not hasattr(search.deleted_page(seeded_db), "income_total")


def test_pagination(seeded_db):
    made = [expense(seeded_db, 10 + i) for i in range(51)]
    for txn_id in made:
        transactions.delete_transaction(seeded_db, txn_id)
    first, second = search.deleted_page(seeded_db), search.deleted_page(seeded_db, page=2)
    assert (first.total_count, first.page_count, len(first.rows), len(second.rows)) == (
        51,
        2,
        50,
        1,
    )
    assert ids(second) == [made[0]]


def test_deleted_transactions_do_not_change_active_search(seeded_db):
    keep = expense(seeded_db, 100)
    transactions.record_income(
        seeded_db, IncomeForm(TODAY, 3000, acc(seeded_db, "4201"), acc(seeded_db, "1102"))
    )
    before = search.search(seeded_db, SearchFilters())

    gone_expense = expense(seeded_db, 999)
    gone_income = transactions.record_income(
        seeded_db, IncomeForm(TODAY, 5000, acc(seeded_db, "4201"), acc(seeded_db, "1102"))
    ).transaction_id
    transactions.delete_transaction(seeded_db, gone_expense)
    transactions.delete_transaction(seeded_db, gone_income)

    after = search.search(seeded_db, SearchFilters())
    assert (
        (after.total_count, after.expense_net, after.income_total)
        == (
            before.total_count,
            before.expense_net,
            before.income_total,
        )
        == (2, 100, 3000)
    )
    assert keep in [r.id for r in after.rows]
    assert {gone_expense, gone_income} == set(ids(search.deleted_page(seeded_db)))


def test_restore_moves_back_to_active(seeded_db):
    txn_id = expense(seeded_db, 100)
    transactions.delete_transaction(seeded_db, txn_id)
    assert ids(search.deleted_page(seeded_db)) == [txn_id]
    transactions.restore_transaction(seeded_db, txn_id)
    assert ids(search.deleted_page(seeded_db)) == []
    assert [r.id for r in search.search(seeded_db, SearchFilters()).rows] == [txn_id]


def test_empty(seeded_db):
    page = search.deleted_page(seeded_db)
    assert (page.rows, page.total_count, page.page, page.page_count) == ((), 0, 1, 1)
