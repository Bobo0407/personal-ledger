"""退款的一次性送出權杖與明細摘要（P2 Step 7，service 層）。

退款規則本身（V07、REFUND_DATE、REFUND_ORIGINAL、REFUND_EXISTS、還原重檢）已有
tests/integration/test_refunds.py；這裡只補 Step 7 新接上的部分。
"""

import pytest
from sqlalchemy import func, select

from app.models import AuditLog, PaymentMethod, Transaction
from app.services import transactions, txn_detail
from app.services.entries import ExpenseForm, RefundForm
from app.services.validation import ValidationFailed
from tests.ledger_helpers import TODAY, acc, freeze_today

TOKEN = "r" * 32


@pytest.fixture(autouse=True)
def _today(monkeypatch):
    freeze_today(monkeypatch)


def pm(db, name="LINE Pay") -> int:
    return db.scalar(select(PaymentMethod.id).where(PaymentMethod.name == name))


def expense(db, amount=1000, **kwargs) -> int:
    form = ExpenseForm(TODAY, amount, acc(db, "5201"), kwargs.pop("pm_id", pm(db)), **kwargs)
    return transactions.record_expense(db, form).transaction_id


def refund(db, original, amount, **kwargs):
    form = RefundForm(original, TODAY, amount, kwargs.pop("pm_id", pm(db)))
    return transactions.record_refund(db, form, **kwargs)


def count(db, model) -> int:
    return db.scalar(select(func.count()).select_from(model))


def test_record_refund_without_token_still_posts(seeded_db):
    result = refund(seeded_db, expense(seeded_db), 300)
    assert seeded_db.get(Transaction, result.transaction_id).source_ref is None
    assert result.replayed is False


def test_token_replay_returns_first_refund_without_writing(seeded_db):
    original = expense(seeded_db)
    first = refund(seeded_db, original, 300, submission_token=TOKEN)
    txn = seeded_db.get(Transaction, first.transaction_id)
    assert (txn.txn_type, txn.source, txn.source_ref) == ("REFUND", "MANUAL", f"submit:{TOKEN}")
    # 第二次即使對另一筆原交易、金額超額，也不驗證、不寫入
    other = expense(seeded_db)
    again = refund(seeded_db, other, 99999, submission_token=TOKEN)
    assert again.replayed is True and again.transaction_id == first.transaction_id
    assert count(seeded_db, Transaction) == 3  # 兩筆支出 + 一筆退款
    assert count(seeded_db, AuditLog) == 3


def test_concurrent_same_token_hits_unique_and_replays(seeded_db, monkeypatch):
    original = expense(seeded_db)
    first = refund(seeded_db, original, 300, submission_token=TOKEN)
    real = transactions._replayed
    calls = {"n": 0}

    def flaky(db, ref):
        calls["n"] += 1
        return None if calls["n"] == 1 else real(db, ref)

    monkeypatch.setattr(transactions, "_replayed", flaky)
    second = refund(seeded_db, original, 300, submission_token=TOKEN)
    assert second.replayed is True and second.transaction_id == first.transaction_id
    assert txn_detail.get_detail(seeded_db, original).refunded_amount == 300


def test_rejected_refund_does_not_consume_token(seeded_db):
    original = expense(seeded_db)
    with pytest.raises(ValidationFailed):
        refund(seeded_db, original, 5000, submission_token=TOKEN)  # V07
    assert refund(seeded_db, original, 500, submission_token=TOKEN).replayed is False


def test_detail_refund_summary_and_links(seeded_db):
    original = expense(seeded_db)
    d = txn_detail.get_detail(seeded_db, original)
    assert (d.refunded_amount, d.refundable_amount, d.refund_blocker, d.refunds) == (
        0,
        1000,
        None,
        (),
    )
    r1 = refund(seeded_db, original, 300).transaction_id
    r2 = refund(seeded_db, original, 200).transaction_id
    d = txn_detail.get_detail(seeded_db, original)
    assert (d.refunded_amount, d.refundable_amount) == (500, 500)
    assert [(r.txn_id, r.amount) for r in d.refunds] == [(r1, 300), (r2, 200)]
    # 刪除的退款不列、不計入
    transactions.delete_transaction(seeded_db, r1)
    d = txn_detail.get_detail(seeded_db, original)
    assert [r.txn_id for r in d.refunds] == [r2] and d.refundable_amount == 800
    refund(seeded_db, original, 800)
    d = txn_detail.get_detail(seeded_db, original)
    assert (d.refundable_amount, d.refund_blocker) == (0, "已全額退款")


def test_restore_refund_over_limit_is_v07(seeded_db):
    original = expense(seeded_db)
    r1 = refund(seeded_db, original, 600).transaction_id
    transactions.delete_transaction(seeded_db, r1)
    refund(seeded_db, original, 700)
    with pytest.raises(ValidationFailed) as exc:
        transactions.restore_transaction(seeded_db, r1)
    assert [i.code for i in exc.value.result.errors] == ["V07"]


def test_refund_state_defaults(seeded_db):
    original = expense(seeded_db, fee_amount=15, fee_account_id=acc(seeded_db, "5502"))
    state = txn_detail.refund_state(seeded_db, original)
    assert (state.main_amount, state.remaining, state.blocker) == (1000, 1000, None)
    assert state.default_payment_method_id == pm(seeded_db)
    # 原付款方式停用 → 不預選
    other = expense(seeded_db, pm_id=pm(seeded_db, "台灣Pay"))
    seeded_db.get(PaymentMethod, pm(seeded_db, "台灣Pay")).is_active = False
    seeded_db.commit()
    assert txn_detail.refund_state(seeded_db, other).default_payment_method_id is None


def test_non_expense_and_deleted_original_have_blockers(seeded_db):
    deleted = expense(seeded_db)
    transactions.delete_transaction(seeded_db, deleted)
    assert txn_detail.refund_state(seeded_db, deleted).blocker == "原交易已刪除或尚未入帳"
    r = refund(seeded_db, expense(seeded_db), 100).transaction_id
    assert txn_detail.refund_state(seeded_db, r).blocker == "只能對支出交易退款"
