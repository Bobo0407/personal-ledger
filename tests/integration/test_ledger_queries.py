"""有效分錄查詢入口與最小餘額查詢（規格 5.4、8.1；決策 D15、Q5 與修正一）。"""

from datetime import date, timedelta

import pytest
from sqlalchemy import select

from app.models import Posting
from app.repositories.ledger import active_postings
from app.services import accounts as account_service
from app.services import ledger_queries as q
from app.services.errors import NotFound
from tests.ledger_helpers import TODAY, acc, freeze_today, insert_raw_transaction, post_manual


@pytest.fixture(autouse=True)
def _today(monkeypatch):
    freeze_today(monkeypatch)


# --- active_postings() ------------------------------------------------------------


def test_active_postings_excludes_deleted_and_draft(seeded_db):
    posted = insert_raw_transaction(seeded_db, "2026-10-01", ("5101", 100), ("1101", -100))
    insert_raw_transaction(seeded_db, "2026-10-01", ("5101", 200), ("1101", -200), deleted=True)
    insert_raw_transaction(seeded_db, "2026-10-01", ("5101", 300), ("1101", -300), status="DRAFT")
    insert_raw_transaction(
        seeded_db, "2026-10-01", ("5101", 400), ("1101", -400), status="DRAFT", deleted=True
    )

    rows = seeded_db.scalars(active_postings()).all()
    assert {p.transaction_id for p in rows} == {posted}
    assert sorted(p.amount for p in rows) == [-100, 100]


def test_balance_ignores_deleted_and_draft(seeded_db):
    insert_raw_transaction(seeded_db, "2026-10-01", ("5101", 100), ("1101", -100))
    insert_raw_transaction(seeded_db, "2026-10-01", ("5101", 200), ("1101", -200), deleted=True)
    insert_raw_transaction(seeded_db, "2026-10-01", ("5101", 300), ("1101", -300), status="DRAFT")
    assert q.balance(seeded_db, acc(seeded_db, "1101")) == -100
    assert q.balance(seeded_db, acc(seeded_db, "5101")) == 100


def test_active_postings_supports_extra_conditions(seeded_db):
    post_manual(seeded_db, TODAY, ("5101", 120), ("1102", -120))
    stmt = active_postings().where(Posting.account_id == acc(seeded_db, "5101"))
    assert [p.amount for p in seeded_db.scalars(stmt)] == [120]


# --- balance() --------------------------------------------------------------------


def test_balance_normal_direction(seeded_db):
    post_manual(seeded_db, TODAY, ("1102", 30_000), ("4101", -30_000), confirmed={"V10"})
    post_manual(seeded_db, TODAY, ("5201", 800), ("2101", -800))

    assert q.balance(seeded_db, acc(seeded_db, "1102")) == 30_000  # 資產 +SUM
    assert q.balance(seeded_db, acc(seeded_db, "2101")) == 800  # 負債 −SUM，欠款為正
    assert q.balance(seeded_db, acc(seeded_db, "4101")) == 30_000  # 收入 −SUM
    assert q.balance(seeded_db, acc(seeded_db, "5201")) == 800  # 費用 +SUM


def test_balance_defaults_to_today_and_excludes_future(seeded_db):
    post_manual(seeded_db, TODAY, ("1101", 100), ("4201", -100))
    post_manual(
        seeded_db, TODAY + timedelta(days=1), ("1101", 50), ("4201", -50), confirmed={"V11"}
    )
    cash = acc(seeded_db, "1101")
    assert q.balance(seeded_db, cash) == 100
    assert q.balance(seeded_db, cash, as_of=TODAY + timedelta(days=1)) == 150


def test_balance_as_of_includes_that_day(seeded_db):
    post_manual(seeded_db, date(2026, 10, 1), ("1101", 100), ("4201", -100))
    post_manual(seeded_db, date(2026, 10, 2), ("1101", 50), ("4201", -50))
    cash = acc(seeded_db, "1101")
    assert q.balance(seeded_db, cash, as_of=date(2026, 9, 30)) == 0
    assert q.balance(seeded_db, cash, as_of=date(2026, 10, 1)) == 100
    assert q.balance(seeded_db, cash, as_of=date(2026, 10, 2)) == 150


def test_group_balance_includes_children(seeded_db):
    # T17 的資料：正餐 1,000、飲料 300 → 餐飲 1,300
    post_manual(seeded_db, TODAY, ("5101", 1000), ("5102", 300), ("1101", -1300))
    assert q.balance(seeded_db, acc(seeded_db, "5100")) == 1300


def test_accounts_balance_is_the_same_function(seeded_db):
    # 規格 7.1：accounts.balance()
    assert account_service.balance is q.balance


def test_balance_unknown_account(seeded_db):
    with pytest.raises(NotFound):
        q.balance(seeded_db, 999_999)


# --- 期間、大類、淨資產 ---------------------------------------------------------------


def test_period_total_is_inclusive(seeded_db):
    for day, amount in ((date(2026, 9, 30), 1), (date(2026, 10, 1), 10), (date(2026, 10, 15), 100)):
        post_manual(seeded_db, day, ("5101", amount), ("1101", -amount))
    lunch = acc(seeded_db, "5101")
    assert q.period_total(seeded_db, lunch, date(2026, 10, 1), date(2026, 10, 15)) == 110
    assert (
        q.period_total(seeded_db, acc(seeded_db, "5100"), date(2026, 9, 1), date(2026, 9, 30)) == 1
    )


def test_period_and_class_totals_use_normal_direction(seeded_db):
    # 語意固定為「正常餘額方向」（與 balance() 相同），呼叫端不需要 abs() 或自行翻號：
    # ASSET／EXPENSE = +SUM；LIABILITY／EQUITY／INCOME = −SUM
    post_manual(
        seeded_db, date(2026, 10, 5), ("1102", 30_000), ("4101", -30_000), confirmed={"V10"}
    )
    post_manual(seeded_db, date(2026, 10, 9), ("5201", 800), ("2101", -800))
    post_manual(seeded_db, date(2026, 10, 10), ("2101", 300), ("1103", -300))  # 繳卡費 300
    october = (date(2026, 10, 1), date(2026, 10, 31))

    assert q.period_total(seeded_db, acc(seeded_db, "4101"), *october) == 30_000  # 收入
    assert q.period_total(seeded_db, acc(seeded_db, "2101"), *october) == 500  # 負債淨增加
    assert q.period_total(seeded_db, acc(seeded_db, "1103"), *october) == -300  # 資產淨減少
    assert q.class_total(seeded_db, "INCOME", *october) == 30_000
    assert q.class_total(seeded_db, "LIABILITY", *october) == 500
    assert q.class_total(seeded_db, "ASSET", *october) == 29_700


def test_refund_makes_expense_period_total_negative(seeded_db):
    # T06 的語意：跨月退款讓退款月份的費用淨額為負數，不取絕對值
    post_manual(seeded_db, date(2026, 11, 2), ("2101", 800), ("5201", -800), confirmed={"V11"})
    november = (date(2026, 11, 1), date(2026, 11, 30))
    assert q.period_total(seeded_db, acc(seeded_db, "5201"), *november) == -800
    assert q.class_total(seeded_db, "EXPENSE", *november) == -800


def test_class_total_monthly_expense(seeded_db):
    post_manual(seeded_db, date(2026, 10, 3), ("5101", 120), ("1101", -120))
    post_manual(seeded_db, date(2026, 10, 9), ("5201", 800), ("2101", -800))
    post_manual(seeded_db, date(2026, 9, 30), ("5201", 999), ("2101", -999))
    post_manual(
        seeded_db, date(2026, 10, 5), ("1102", 30_000), ("4101", -30_000), confirmed={"V10"}
    )

    october = (date(2026, 10, 1), date(2026, 10, 31))
    assert q.class_total(seeded_db, "EXPENSE", *october) == 920
    assert q.class_total(seeded_db, "INCOME", *october) == 30_000


def test_net_worth(seeded_db):
    post_manual(seeded_db, date(2026, 10, 1), ("1101", 2000), ("4201", -2000))
    post_manual(seeded_db, date(2026, 10, 2), ("5201", 3000), ("2101", -3000))
    assert q.net_worth(seeded_db) == -1000
    assert q.net_worth(seeded_db, as_of=date(2026, 10, 1)) == 2000


def test_net_worth_excludes_future(seeded_db):
    post_manual(seeded_db, TODAY, ("1101", 100), ("4201", -100))
    post_manual(
        seeded_db, TODAY + timedelta(days=3), ("1101", 500), ("4201", -500), confirmed={"V11"}
    )
    assert q.net_worth(seeded_db) == 100


def test_queries_select_only_active(seeded_db):
    # 確認 active_postings() 的排除條件在加總查詢中保留下來
    insert_raw_transaction(seeded_db, "2026-10-01", ("5101", 999), ("1101", -999), deleted=True)
    october = (date(2026, 10, 1), date(2026, 10, 31))
    assert q.class_total(seeded_db, "EXPENSE", *october) == 0
    assert q.period_total(seeded_db, acc(seeded_db, "5101"), *october) == 0
    assert q.net_worth(seeded_db) == 0
    assert seeded_db.scalars(select(Posting)).all()  # 原始資料仍在
