"""首頁帳戶餘額與帳平檢查（P3-1；規格 8.1、F26、5.4）。

餘額沿用 ledger_queries.balance()（active_postings()），這裡只驗證首頁的取捨：
哪些帳戶、正負號與欠款、停用帳戶、已刪除與未來交易、帳平問題的合併。
"""

from datetime import timedelta

import pytest

from app.models import Account
from app.services import transactions
from app.services.home import home_balances, home_summary, integrity_status
from tests.ledger_helpers import TODAY, acc, freeze_today, insert_raw_transaction, post_manual


@pytest.fixture(autouse=True)
def _today(monkeypatch):
    freeze_today(monkeypatch)


def amounts(db) -> dict[str, int]:
    return {row.name: row.amount for row in home_balances(db)}


def set_flags(db, code: str, **values) -> None:
    """測試用：直接改主資料旗標（不是在測科目管理規則）。"""
    account = db.get(Account, acc(db, code))
    for key, value in values.items():
        setattr(account, key, value)
    db.commit()


# --- 帳戶餘額 ----------------------------------------------------------------------------


def test_only_show_on_home_accounts_in_sort_order(seeded_db):
    names = [row.name for row in home_balances(seeded_db)]
    assert names == ["現金", "郵局", "聯邦活存", "聯邦信用卡"]


def test_no_transactions_shows_zero(seeded_db):
    assert set(amounts(seeded_db).values()) == {0}


def test_asset_balance_and_card_debt(seeded_db):
    post_manual(seeded_db, TODAY, ("5101", 120), ("1102", -120))
    post_manual(seeded_db, TODAY, ("5201", 500), ("2101", -500))
    rows = {row.name: row for row in home_balances(seeded_db)}
    assert rows["郵局"].amount == -120
    assert rows["郵局"].is_debt is False
    assert rows["聯邦信用卡"].amount == 500
    assert rows["聯邦信用卡"].is_debt is True


def test_overpaid_card_is_negative_without_debt_label(seeded_db):
    post_manual(seeded_db, TODAY, ("2101", 300), ("1103", -300))
    card = next(row for row in home_balances(seeded_db) if row.name == "聯邦信用卡")
    assert card.amount == -300
    assert card.is_debt is False


def test_deleted_transaction_excluded_and_restored_counts_again(seeded_db):
    txn_id = post_manual(seeded_db, TODAY, ("5101", 120), ("1101", -120)).transaction_id
    transactions.delete_transaction(seeded_db, txn_id)
    assert amounts(seeded_db)["現金"] == 0
    transactions.restore_transaction(seeded_db, txn_id)
    assert amounts(seeded_db)["現金"] == -120


def test_future_dated_transaction_not_counted(seeded_db):
    post_manual(
        seeded_db, TODAY + timedelta(days=1), ("5101", 120), ("1101", -120), confirmed={"V11"}
    )
    assert amounts(seeded_db)["現金"] == 0


def test_inactive_account_still_shown_with_real_balance(seeded_db):
    post_manual(seeded_db, TODAY, ("1101", 800), ("4201", -800))
    set_flags(seeded_db, "1101", is_active=False)
    cash = next(row for row in home_balances(seeded_db) if row.name == "現金")
    assert (cash.amount, cash.is_active) == (800, False)


def test_non_asset_liability_accounts_skipped(seeded_db):
    set_flags(seeded_db, "5101", show_on_home=True)
    set_flags(seeded_db, "3101", show_on_home=True)
    assert "正餐" not in amounts(seeded_db)
    assert "期初權益" not in amounts(seeded_db)


# --- 帳平檢查 ----------------------------------------------------------------------------


def test_clean_ledger_is_ok(seeded_db):
    post_manual(seeded_db, TODAY, ("5101", 120), ("1102", -120))
    status = integrity_status(seeded_db)
    assert status.ok
    assert status.items == ()


def test_unbalanced_transaction_listed(seeded_db):
    bad = insert_raw_transaction(seeded_db, "2026-10-01", ("5101", 100), ("1101", -90))
    status = integrity_status(seeded_db)
    assert not status.ok
    [item] = status.items
    assert (item.transaction_id, item.labels, item.is_deleted) == (bad, ("借貸不平衡",), False)


def test_deleted_and_draft_problems_listed(seeded_db):
    deleted = insert_raw_transaction(
        seeded_db, "2026-10-01", ("5101", 100), ("1101", -1), deleted=True
    )
    draft = insert_raw_transaction(
        seeded_db, "2026-10-02", ("5101", 100), ("1101", -1), status="DRAFT"
    )
    items = {item.transaction_id: item for item in integrity_status(seeded_db).items}
    assert (items[deleted].is_deleted, items[deleted].is_draft) == (True, False)
    assert (items[draft].is_deleted, items[draft].is_draft) == (False, True)


def test_multiple_problems_on_one_transaction_merged(seeded_db):
    bad = insert_raw_transaction(seeded_db, "2026-10-01", ("5101", 100))
    [item] = integrity_status(seeded_db).items
    assert item.transaction_id == bad
    assert item.labels == ("分錄不足兩筆", "借貸不平衡")


def test_home_summary_combines_both(seeded_db):
    post_manual(seeded_db, TODAY, ("5101", 120), ("1102", -120))
    summary = home_summary(seeded_db)
    assert [row.name for row in summary.balances][1] == "郵局"
    assert summary.integrity.ok
