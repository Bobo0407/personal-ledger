"""期初餘額精靈的讀取端（P3-7；services/opening_setup.py）。寫入由 P1 record_opening 負責。"""

from datetime import date

from app.services import accounts as account_service
from app.services import opening_setup, transactions
from app.services.entries import OpeningForm, OpeningLine
from tests.ledger_helpers import acc, post_manual

DAY = date(2026, 10, 15)


def rows_by_name(db):
    return {r.name: r for r in opening_setup.opening_rows(db)}


def test_rows_are_only_active_leaf_assets_and_liabilities(seeded_db):
    names = set(rows_by_name(seeded_db))
    assert names == {"現金", "郵局", "聯邦活存", "勞退個人專戶", "預付所得稅", "聯邦信用卡"}
    assert "期初權益" not in names and "正餐" not in names  # 權益、費用不會出現


def test_inactive_accounts_are_not_offered(seeded_db):
    account_service.deactivate_account(seeded_db, acc(seeded_db, "1201"))
    assert "勞退個人專戶" not in rows_by_name(seeded_db)


def test_rows_report_balance_and_existing_opening(seeded_db):
    before = rows_by_name(seeded_db)
    assert all(r.balance == 0 and not r.has_opening for r in before.values())
    form = OpeningForm(
        DAY,
        (
            OpeningLine(acc(seeded_db, "1101"), 2000),
            OpeningLine(acc(seeded_db, "1102"), 50000),
            OpeningLine(acc(seeded_db, "2101"), 3000),
        ),
    )
    transactions.record_opening(seeded_db, form)
    after = rows_by_name(seeded_db)
    assert (after["現金"].balance, after["現金"].has_opening) == (2000, True)
    assert after["郵局"].balance == 50000
    assert (after["聯邦信用卡"].balance, after["聯邦信用卡"].is_liability) == (
        3000,
        True,
    )  # 欠款為正
    assert after["聯邦活存"].has_opening is False


def test_zero_balance_line_does_not_count_as_an_opening(seeded_db):
    form = OpeningForm(
        DAY, (OpeningLine(acc(seeded_db, "1101"), 100), OpeningLine(acc(seeded_db, "1102"), 0))
    )
    transactions.record_opening(seeded_db, form)
    assert rows_by_name(seeded_db)["郵局"].has_opening is False


def test_needs_setup_until_any_opening_exists(seeded_db):
    assert opening_setup.needs_setup(seeded_db) is True
    transactions.record_opening(
        seeded_db, OpeningForm(DAY, (OpeningLine(acc(seeded_db, "1101"), 1),))
    )
    assert opening_setup.needs_setup(seeded_db) is False


def test_activity_without_an_opening_is_flagged(seeded_db):
    post_manual(seeded_db, DAY, ("1101", 800), ("5101", -800))
    row = rows_by_name(seeded_db)["現金"]
    assert row.balance == 800 and row.has_activity is True
    assert rows_by_name(seeded_db)["郵局"].has_activity is False
