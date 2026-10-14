"""首頁帳戶餘額與帳平檢查（P3-1；規格 8.1、F26、5.4）。

餘額沿用 ledger_queries.balance()（active_postings()），這裡只驗證首頁的取捨：
哪些帳戶、正負號與欠款、停用帳戶、已刪除與未來交易、帳平問題的合併。
"""

from datetime import date, timedelta

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


# --- P3-2B 信用卡區塊 ---------------------------------------------------------------------


def card_rows(db):
    from app.services.home import home_cards

    return {row.name: row for row in home_cards(db)}


def test_card_block_uses_amount_due_service(seeded_db):
    """首頁數字必須與 credit_cards.amount_due() 完全一致（不另寫一套算法）。"""
    from app.services import credit_cards

    # 凍結 2026-10-15：結帳日 10/15 當天算本期，繳款日 11/03
    post_manual(seeded_db, TODAY - timedelta(days=5), ("5201", 700), ("2101", -700))
    row = card_rows(seeded_db)["聯邦信用卡"]
    expected = credit_cards.amount_due(seeded_db, acc(seeded_db, "2101"), TODAY)
    assert row.due == expected
    assert row.due.amount_due == 700
    assert not row.is_overdue


def test_card_without_profile_shows_unset(seeded_db):
    from app.models import CreditCardProfile

    seeded_db.delete(seeded_db.get(CreditCardProfile, acc(seeded_db, "2101")))
    seeded_db.commit()
    row = card_rows(seeded_db)["聯邦信用卡"]
    assert row.due is None
    assert not row.is_overdue and row.overdue_days == 0


def test_card_not_shown_on_home_is_hidden(seeded_db):
    set_flags(seeded_db, "2101", show_on_home=False)
    assert card_rows(seeded_db) == {}


def test_non_card_accounts_are_not_in_card_block(seeded_db):
    assert list(card_rows(seeded_db)) == ["聯邦信用卡"]


def test_inactive_card_with_profile_shown_without_profile_skipped(seeded_db):
    from app.models import CreditCardProfile
    from app.services import accounts as account_service

    set_flags(seeded_db, "2101", is_active=False)
    assert card_rows(seeded_db)["聯邦信用卡"].is_active is False
    other = account_service.create_account(
        seeded_db,
        code="2102",
        name="無設定停用卡",
        account_class="LIABILITY",
        subtype="CREDIT_CARD",
        show_on_home=True,
    )
    set_flags(seeded_db, "2102", is_active=False)
    assert other.id not in [r.account_id for r in card_rows(seeded_db).values()]
    assert seeded_db.get(CreditCardProfile, other.id) is None


def test_overpaid_card_has_zero_due_and_overpaid(seeded_db):
    from app.services.entries import CardPaymentForm

    post_manual(seeded_db, TODAY - timedelta(days=20), ("5201", 500), ("2101", -500))
    form = CardPaymentForm(TODAY, 800, acc(seeded_db, "2101"), acc(seeded_db, "1103"))
    transactions.record_card_payment(seeded_db, form)
    row = card_rows(seeded_db)["聯邦信用卡"]
    assert (row.due.amount_due, row.due.overpaid) == (0, 300)
    assert not row.is_overdue


def test_overdue_only_when_past_due_date_and_amount_due_positive(seeded_db, monkeypatch):
    post_manual(seeded_db, date(2026, 10, 10), ("5201", 700), ("2101", -700))
    # 結帳日 10/15、繳款日 11/03；11/10 仍在同一個 statement cycle（下一個結帳日是 11/15）
    freeze_today(monkeypatch, date(2026, 11, 10))
    row = card_rows(seeded_db)["聯邦信用卡"]
    assert row.due.cycle.days_until_due == -7
    assert row.is_overdue and row.overdue_days == 7
    # 繳清後不再逾期
    from app.services.entries import CardPaymentForm

    form = CardPaymentForm(date(2026, 11, 10), 700, acc(seeded_db, "2101"), acc(seeded_db, "1103"))
    transactions.record_card_payment(seeded_db, form)
    row = card_rows(seeded_db)["聯邦信用卡"]
    assert row.due.amount_due == 0 and not row.is_overdue


def test_home_summary_includes_cards(seeded_db):
    assert [c.name for c in home_summary(seeded_db).cards] == ["聯邦信用卡"]
