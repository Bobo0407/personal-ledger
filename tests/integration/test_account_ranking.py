"""快速記帳主科目的「常用」排序（P2 Step 11，UF01）：使用次數與選單分組。

使用次數 = 有效交易中 txn_type 為 EXPENSE、MAIN 分錄落在該科目的交易筆數（全期累計）。
這裡直接驗證 repositories/ledger.expense_main_usage_counts() 與
services/entry_options.ranked_expense_account_groups()；Web 層在 test_web_home.py。
"""

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.models import PaymentMethod, Posting, Transaction
from app.repositories import ledger as ledger_repo
from app.services import accounts, entry_options, transactions
from app.services.entries import ExpenseForm, RefundForm
from tests.ledger_helpers import TODAY, acc, freeze_today


@pytest.fixture(autouse=True)
def _today(monkeypatch):
    freeze_today(monkeypatch)


def pm(db, name="LINE Pay") -> int:
    return db.scalar(select(PaymentMethod.id).where(PaymentMethod.name == name))


def spend(db, code: str, times: int = 1, **kwargs) -> list[int]:
    ids = []
    for _ in range(times):
        form = ExpenseForm(
            txn_date=TODAY,
            amount=120,
            account_id=acc(db, code),
            payment_method_id=pm(db),
            **kwargs,
        )
        result = transactions.record_expense(db, form, confirmed=["V10", "V11", "V12"])
        ids.append(result.transaction_id)
    return ids


def raw(db, txn_type, lines, *, status="POSTED", deleted=False) -> int:
    """直接寫入交易（不經過 builder）：lines = [(科目代碼, 金額, line_role)]。"""
    txn = Transaction(txn_date=TODAY.isoformat(), txn_type=txn_type, status=status)
    if deleted:
        txn.deleted_at = "2026-10-15T12:00:00+08:00"
    txn.postings = [
        Posting(line_no=i, account_id=acc(db, code), amount=amount, line_role=role)
        for i, (code, amount, role) in enumerate(lines, start=1)
    ]
    db.add(txn)
    db.commit()
    return txn.id


def counts(db, *codes) -> dict[str, int]:
    by_id = ledger_repo.expense_main_usage_counts(db, [acc(db, c) for c in codes])
    return {c: by_id[acc(db, c)] for c in codes if acc(db, c) in by_id}


def ranked(db) -> list[tuple[str | None, list[str]]]:
    return [
        (label, [a.code for a in items])
        for label, items in entry_options.ranked_expense_account_groups(db)
    ]


def plain(db) -> list[tuple[str | None, list[str]]]:
    return [
        (label, [a.code for a in items])
        for label, items in entry_options.expense_account_groups(db)
    ]


def flat_codes(db) -> list[str]:
    return [code for _, items in ranked(db) for code in items]


# --- 沒有使用紀錄 ------------------------------------------------------------------------


def test_no_usage_keeps_existing_order_and_has_no_frequent_group(seeded_db):
    assert ranked(seeded_db) == plain(seeded_db)
    assert all(label != entry_options.FREQUENT_LABEL for label, _ in ranked(seeded_db))


def test_usage_counts_empty_input(seeded_db):
    assert ledger_repo.expense_main_usage_counts(seeded_db, []) == {}


# --- 排序 --------------------------------------------------------------------------------


def test_used_account_moves_to_frequent_group_on_top(seeded_db):
    spend(seeded_db, "5201")
    groups = ranked(seeded_db)
    assert groups[0] == (entry_options.FREQUENT_LABEL, ["5201"])
    # 從原本的上層分組移出，不重複；沒用過的維持原順序
    assert ("購物", ["5201"]) not in groups
    assert [g for g in groups if g[0] == "購物"] == []
    assert groups[1:] == [g for g in plain(seeded_db) if g[0] != "購物"]


def test_used_accounts_rank_above_unused_even_in_later_groups(seeded_db):
    spend(seeded_db, "5602")  # 最後一個分組裡的科目
    assert flat_codes(seeded_db)[0] == "5602"


def test_higher_usage_count_first(seeded_db):
    spend(seeded_db, "5102", 2)
    spend(seeded_db, "5201", 3)
    spend(seeded_db, "5301", 1)
    assert ranked(seeded_db)[0] == (entry_options.FREQUENT_LABEL, ["5201", "5102", "5301"])


def test_tie_falls_back_to_existing_sort_order_and_code(seeded_db):
    spend(seeded_db, "5303")
    spend(seeded_db, "5102")
    spend(seeded_db, "5201")
    existing = [a.code for a in accounts.leaf_accounts(seeded_db)]
    expected = sorted(["5303", "5102", "5201"], key=existing.index)
    assert ranked(seeded_db)[0] == (entry_options.FREQUENT_LABEL, expected)


def test_each_account_appears_exactly_once(seeded_db):
    spend(seeded_db, "5101", 2)
    spend(seeded_db, "5303")
    codes = flat_codes(seeded_db)
    assert len(codes) == len(set(codes)) == len(plain_codes(seeded_db))
    assert sorted(codes) == sorted(plain_codes(seeded_db))


def plain_codes(db) -> list[str]:
    return [code for _, items in plain(db) for code in items]


def test_groups_left_empty_are_dropped(seeded_db):
    spend(seeded_db, "5201")  # 「購物」只有這一個科目
    assert "購物" not in [label for label, _ in ranked(seeded_db)]


# --- 什麼算、什麼不算 ----------------------------------------------------------------------


def test_deleted_not_counted_and_restored_counts_again(seeded_db):
    [txn_id] = spend(seeded_db, "5101")
    assert counts(seeded_db, "5101") == {"5101": 1}
    transactions.delete_transaction(seeded_db, txn_id)
    assert counts(seeded_db, "5101") == {}
    assert ranked(seeded_db) == plain(seeded_db)
    transactions.restore_transaction(seeded_db, txn_id)
    assert counts(seeded_db, "5101") == {"5101": 1}


def test_draft_not_counted(seeded_db):
    raw(seeded_db, "EXPENSE", [("5101", 100, "MAIN"), ("1101", -100, None)], status="DRAFT")
    assert counts(seeded_db, "5101") == {}


def test_deleted_draft_not_counted(seeded_db):
    raw(
        seeded_db,
        "EXPENSE",
        [("5101", 100, "MAIN"), ("1101", -100, None)],
        status="DRAFT",
        deleted=True,
    )
    assert counts(seeded_db, "5101") == {}


def test_refund_does_not_count_and_original_still_counts_once(seeded_db):
    [txn_id] = spend(seeded_db, "5101")
    transactions.record_refund(
        seeded_db,
        RefundForm(
            original_txn_id=txn_id, txn_date=TODAY, amount=50, payment_method_id=pm(seeded_db)
        ),
    )
    assert counts(seeded_db, "5101") == {"5101": 1}


def test_fully_refunded_expense_still_counts(seeded_db):
    [txn_id] = spend(seeded_db, "5101")
    transactions.record_refund(
        seeded_db,
        RefundForm(
            original_txn_id=txn_id, txn_date=TODAY, amount=120, payment_method_id=pm(seeded_db)
        ),
    )
    assert counts(seeded_db, "5101") == {"5101": 1}


def test_fee_and_interest_lines_do_not_count(seeded_db):
    spend(
        seeded_db,
        "5101",
        fee_amount=15,
        fee_account_id=acc(seeded_db, "5501"),
    )
    raw(
        seeded_db,
        "EXPENSE",
        [
            ("5101", 1000, "MAIN"),
            ("5503", 50, "INTEREST"),
            ("5502", 10, "FEE"),
            ("1101", -1060, None),
        ],
    )
    assert counts(seeded_db, "5101", "5501", "5502", "5503") == {"5101": 2}


def test_manual_transaction_does_not_count(seeded_db):
    raw(seeded_db, "MANUAL", [("5101", 100, "MAIN"), ("1101", -100, None)])
    assert counts(seeded_db, "5101") == {}


def test_counts_transactions_not_postings(seeded_db):
    raw(
        seeded_db,
        "EXPENSE",
        [("5101", 60, "MAIN"), ("5101", 40, "MAIN"), ("1101", -100, None)],
    )
    assert counts(seeded_db, "5101") == {"5101": 1}


def test_modified_expense_follows_current_main_account(seeded_db):
    [txn_id] = spend(seeded_db, "5101")
    form = ExpenseForm(
        txn_date=TODAY,
        amount=120,
        account_id=acc(seeded_db, "5201"),
        payment_method_id=pm(seeded_db),
    )
    transactions.update_transaction(seeded_db, txn_id, form, confirmed=["V10", "V11", "V12"])
    assert counts(seeded_db, "5101", "5201") == {"5201": 1}
    assert ranked(seeded_db)[0] == (entry_options.FREQUENT_LABEL, ["5201"])


def test_usage_is_all_time(seeded_db):
    form = ExpenseForm(
        txn_date=TODAY - timedelta(days=800),
        amount=120,
        account_id=acc(seeded_db, "5101"),
        payment_method_id=pm(seeded_db),
    )
    transactions.record_expense(seeded_db, form, confirmed=["V10", "V11", "V12"])
    assert counts(seeded_db, "5101") == {"5101": 1}


# --- 候選科目 ----------------------------------------------------------------------------


def test_inactive_account_excluded_even_if_used(seeded_db):
    spend(seeded_db, "5101", 3)
    spend(seeded_db, "5201")
    accounts.deactivate_account(seeded_db, acc(seeded_db, "5101"))
    assert "5101" not in flat_codes(seeded_db)
    assert ranked(seeded_db)[0] == (entry_options.FREQUENT_LABEL, ["5201"])


def test_non_leaf_account_excluded(seeded_db):
    raw(seeded_db, "EXPENSE", [("5100", 100, "MAIN"), ("1101", -100, None)])  # 「餐飲」是上層科目
    assert "5100" not in flat_codes(seeded_db)
    assert ranked(seeded_db) == plain(seeded_db)


@pytest.mark.parametrize("code", ["1101", "4201"])
def test_non_expense_account_excluded(seeded_db, code):
    raw(seeded_db, "EXPENSE", [(code, 100, "MAIN"), ("5101", -100, None)])
    assert code not in flat_codes(seeded_db)
    assert ranked(seeded_db) == plain(seeded_db)
