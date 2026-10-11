"""帳平檢查 integrity.check_all()（規格 5.4、T14；決策 Q9）。

測試直接寫入資料庫，製造 LedgerService 不會產生的壞資料。
"""

import pytest

from app.models import Transaction
from app.services import accounts as account_service
from app.services.integrity import check_all
from tests.ledger_helpers import TODAY, acc, freeze_today, insert_raw_transaction, post_manual


@pytest.fixture(autouse=True)
def _today(monkeypatch):
    freeze_today(monkeypatch)


def problem_codes(db) -> list[tuple[int, str]]:
    return [(p.transaction_id, p.code) for p in check_all(db)]


def test_clean_ledger_has_no_problems(seeded_db):
    post_manual(seeded_db, TODAY, ("5101", 120), ("1102", -120))
    assert check_all(seeded_db) == []


def test_t14_unbalanced_transaction_written_directly(seeded_db):
    bad = insert_raw_transaction(seeded_db, "2026-10-01", ("5101", 100), ("1101", -90))
    problems = check_all(seeded_db)
    assert [(p.transaction_id, p.code) for p in problems] == [(bad, "UNBALANCED")]
    assert "差額 10 元" in problems[0].detail


@pytest.mark.parametrize(
    ("status", "deleted"), [("DRAFT", False), ("POSTED", True), ("DRAFT", True)]
)
def test_checks_draft_and_deleted_transactions(seeded_db, status, deleted):
    bad = insert_raw_transaction(
        seeded_db, "2026-10-01", ("5101", 100), ("1101", -1), status=status, deleted=deleted
    )
    [problem] = check_all(seeded_db)
    assert (problem.transaction_id, problem.code) == (bad, "UNBALANCED")
    assert (problem.status, problem.is_deleted) == (status, deleted)


def test_single_posting(seeded_db):
    bad = insert_raw_transaction(seeded_db, "2026-10-01", ("5101", 100))
    assert problem_codes(seeded_db) == [(bad, "TOO_FEW_POSTINGS"), (bad, "UNBALANCED")]


def test_transaction_without_postings(seeded_db):
    txn = Transaction(txn_date="2026-10-01", txn_type="MANUAL")
    seeded_db.add(txn)
    seeded_db.commit()
    assert problem_codes(seeded_db) == [(txn.id, "TOO_FEW_POSTINGS")]


def test_posting_on_non_leaf_account(seeded_db):
    bad = insert_raw_transaction(seeded_db, "2026-10-01", ("5100", 100), ("1101", -100))
    assert problem_codes(seeded_db) == [(bad, "NON_LEAF_ACCOUNT")]


def test_inactive_account_is_not_a_problem(seeded_db):
    post_manual(seeded_db, TODAY, ("5303", 100), ("1101", -100))
    account_service.deactivate_account(seeded_db, acc(seeded_db, "5303"))
    assert check_all(seeded_db) == []
