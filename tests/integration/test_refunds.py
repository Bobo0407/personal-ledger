"""退款（Step 6；T05、T06、T07、T34、V07、REFUND_*、D16 鎖定）。"""

from dataclasses import replace
from datetime import date, timedelta

import pytest
from sqlalchemy import func, select

from app.models import Merchant, Posting, Transaction
from app.services import ledger
from app.services import ledger_queries as q
from app.services import payment_methods as pm_service
from app.services import transactions as tx
from app.services.drafts import EntryDraft, PostingDraft
from app.services.entries import ExpenseForm, IncomeForm, RefundForm
from app.services.validation import ValidationFailed
from tests.ledger_helpers import TODAY, acc, freeze_today

OCTOBER = (date(2026, 10, 1), date(2026, 10, 31))
NOVEMBER = (date(2026, 11, 1), date(2026, 11, 30))


@pytest.fixture(autouse=True)
def _today(monkeypatch):
    freeze_today(monkeypatch)


def pm(db, name: str) -> int:
    return next(p.id for p in pm_service.list_payment_methods(db, False) if p.name == name)


def lines(db, txn_id: int) -> list[tuple[str, int, str | None]]:
    txn = db.get(Transaction, txn_id)
    return [(p.account.code, p.amount, p.line_role) for p in txn.postings]


def counts(db) -> tuple[int, int]:
    return (
        db.scalar(select(func.count()).select_from(Transaction)),
        db.scalar(select(func.count()).select_from(Posting)),
    )


def expense_form(db, amount, account="5201", method="聯邦信用卡", txn_date=TODAY, **kw):
    return ExpenseForm(txn_date, amount, acc(db, account), pm(db, method), **kw)


def expense(db, amount, account="5201", method="聯邦信用卡", txn_date=TODAY, **kw) -> int:
    return tx.record_expense(
        db, expense_form(db, amount, account, method, txn_date, **kw)
    ).transaction_id


def refund_form(db, original_id, amount, method="聯邦信用卡", txn_date=TODAY, **kw):
    return RefundForm(original_id, txn_date, amount, pm(db, method), **kw)


def refund(db, original_id, amount, method="聯邦信用卡", txn_date=TODAY, **kw) -> int:
    form = refund_form(db, original_id, amount, method, txn_date, **kw)
    return tx.record_refund(db, form).transaction_id


def period(db, code, start_end=OCTOBER) -> int:
    return q.period_total(db, acc(db, code), *start_end)


def errors_of(exc_info) -> list[tuple[str, str | None]]:
    return [(i.code, i.field) for i in exc_info.value.result.errors]


# --- T05、T06、T07、T34 -------------------------------------------------------------


def test_t05_full_refund(seeded_db):
    original = expense(seeded_db, 800)  # T02：網購刷聯邦信用卡
    refund_id = refund(seeded_db, original, 800)

    txn = seeded_db.get(Transaction, refund_id)
    assert txn.txn_type == "REFUND"
    assert txn.related_txn_id == original
    assert lines(seeded_db, refund_id) == [("2101", 800, None), ("5201", -800, "MAIN")]
    assert period(seeded_db, "5201") == 0  # 網購本月淨額為 0
    assert q.balance(seeded_db, acc(seeded_db, "2101")) == 0  # 信用卡欠款為 0


def test_t06_cross_month_refund(seeded_db, monkeypatch):
    freeze_today(monkeypatch, date(2026, 11, 15))
    original = expense(seeded_db, 800, txn_date=date(2026, 10, 10))
    refund(seeded_db, original, 800, txn_date=date(2026, 11, 5))

    assert q.class_total(seeded_db, "EXPENSE", *OCTOBER) == 800
    assert q.class_total(seeded_db, "EXPENSE", *NOVEMBER) == -800  # 負數就是負數，不 abs()
    assert period(seeded_db, "5201", NOVEMBER) == -800


def test_t07_over_refund_blocked_and_nothing_written(seeded_db):
    original = expense(seeded_db, 800)
    before = counts(seeded_db)
    with pytest.raises(ValidationFailed) as exc:
        refund(seeded_db, original, 1000)
    assert errors_of(exc) == [("V07", "amount")]
    assert "最多還能退 800" in exc.value.result.errors[0].message
    assert counts(seeded_db) == before


def test_t34_refund_only_reverses_main_of_multi_line_expense(seeded_db):
    # T08：演唱會轉播 1,000 + 海外交易手續費 15，刷聯邦信用卡
    form = expense_form(
        seeded_db, 1000, "5301", fee_amount=15, fee_account_id=acc(seeded_db, "5502")
    )
    original = tx.record_expense(seeded_db, form).transaction_id
    refund_id = refund(seeded_db, original, 1000)

    assert lines(seeded_db, refund_id) == [("2101", 1000, None), ("5301", -1000, "MAIN")]
    assert period(seeded_db, "5301") == 0
    assert period(seeded_db, "5502") == 15  # 手續費維持為支出
    with pytest.raises(ValidationFailed) as exc:
        refund(seeded_db, original, 1)
    assert errors_of(exc) == [("V07", "amount")]


# --- V07 與原交易 ----------------------------------------------------------------------


def test_partial_refunds_accumulate(seeded_db):
    original = expense(seeded_db, 800)
    refund(seeded_db, original, 300)
    refund(seeded_db, original, 500)
    with pytest.raises(ValidationFailed) as exc:
        refund(seeded_db, original, 1)
    assert "已退 800 元" in exc.value.result.errors[0].message
    assert period(seeded_db, "5201") == 0


def test_deleted_refund_does_not_count(seeded_db):
    original = expense(seeded_db, 800)
    first = refund(seeded_db, original, 800)
    tx.delete_transaction(seeded_db, first)
    refund(seeded_db, original, 800)  # 已刪除的退款不算已退
    assert period(seeded_db, "5201") == 0


def test_refund_date_cannot_precede_original(seeded_db):
    original = expense(seeded_db, 800)
    with pytest.raises(ValidationFailed) as exc:
        refund(seeded_db, original, 100, txn_date=TODAY - timedelta(days=1))
    assert errors_of(exc) == [("REFUND_DATE", "txn_date")]
    refund(seeded_db, original, 100, txn_date=TODAY)  # 同一天可以


def test_refund_original_must_be_active_expense(seeded_db):
    income = tx.record_income(
        seeded_db, IncomeForm(TODAY, 500, acc(seeded_db, "4201"), acc(seeded_db, "1102"))
    ).transaction_id
    deleted = expense(seeded_db, 800)
    tx.delete_transaction(seeded_db, deleted)

    for original_id, message in ((income, "只能對支出"), (deleted, "已刪除"), (9999, "找不到")):
        with pytest.raises(ValidationFailed) as exc:
            refund(seeded_db, original_id, 100)
        assert errors_of(exc) == [("REFUND_ORIGINAL", "original_txn_id")]
        assert message in exc.value.result.errors[0].message


def test_refund_form_required_fields(seeded_db):
    with pytest.raises(ValidationFailed) as exc:
        tx.record_refund(seeded_db, RefundForm(None, TODAY, 0, None))
    assert errors_of(exc) == [
        ("V05", "original_txn_id"),
        ("V01", "amount"),
        ("V05", "payment_method_id"),
    ]


def test_refund_copies_merchant_and_budget_flag_but_payment_method_is_free(seeded_db):
    merchant = Merchant(name="蝦皮")
    seeded_db.add(merchant)
    seeded_db.commit()
    original = expense(seeded_db, 800, merchant_id=merchant.id, exclude_from_budget=True)
    refund_id = refund(seeded_db, original, 800, method="LINE Pay", description="退貨")

    txn = seeded_db.get(Transaction, refund_id)
    assert txn.merchant_id == merchant.id
    assert txn.exclude_from_budget is True
    assert txn.payment_method_id == pm(seeded_db, "LINE Pay")
    assert txn.description == "退貨"
    # 刷卡買、退到郵局：信用卡欠款仍在，郵局多了 800
    assert lines(seeded_db, refund_id) == [("1102", 800, None), ("5201", -800, "MAIN")]
    assert q.balance(seeded_db, acc(seeded_db, "2101")) == 800


# --- 第二道防線：手寫草稿 ---------------------------------------------------------------


def refund_draft(db, original_id, back="2101", main="5201", amount=100, **kw) -> EntryDraft:
    return EntryDraft(
        txn_date=TODAY,
        txn_type="REFUND",
        postings=(
            PostingDraft(acc(db, back), amount),
            PostingDraft(acc(db, main), -amount, "MAIN"),
        ),
        payment_method_id=pm(db, "聯邦信用卡"),
        related_txn_id=original_id,
        **kw,
    )


def test_handwritten_refund_must_reverse_original_main_account(seeded_db):
    original = expense(seeded_db, 800)
    with pytest.raises(ValidationFailed) as exc:
        ledger.post(seeded_db, refund_draft(seeded_db, original, main="5102"))
    assert errors_of(exc) == [("REFUND_ORIGINAL", "original_txn_id")]


def test_handwritten_refund_without_original(seeded_db):
    with pytest.raises(ValidationFailed) as exc:
        ledger.post(seeded_db, refund_draft(seeded_db, None))
    assert errors_of(exc) == [("REFUND_ORIGINAL", "original_txn_id")]


@pytest.mark.parametrize(
    ("draft_kwargs", "message"),
    [
        ({"main": "4201"}, "費用科目"),  # MAIN 記在收入科目
        ({"back": "5102"}, "資產或負債"),  # 退回分錄記在費用科目
    ],
)
def test_refund_structure(seeded_db, draft_kwargs, message):
    original = expense(seeded_db, 800)
    with pytest.raises(ValidationFailed) as exc:
        ledger.post(seeded_db, refund_draft(seeded_db, original, **draft_kwargs))
    errors = exc.value.result.errors
    assert {i.code for i in errors} == {"TXN_STRUCTURE"}
    assert any(message in i.message for i in errors)


def test_refund_structure_back_account_must_match_payment_method(seeded_db):
    original = expense(seeded_db, 800)
    with pytest.raises(ValidationFailed) as exc:
        ledger.post(seeded_db, refund_draft(seeded_db, original, back="1102"))
    assert [i.code for i in exc.value.result.errors] == ["TXN_STRUCTURE"]


def test_refund_structure_requires_payment_method(seeded_db):
    original = expense(seeded_db, 800)
    draft = replace(refund_draft(seeded_db, original), payment_method_id=None)
    with pytest.raises(ValidationFailed) as exc:
        ledger.post(seeded_db, draft)
    assert [i.code for i in exc.value.result.errors] == ["TXN_STRUCTURE"]


# --- 修改退款（U8、U11）-----------------------------------------------------------------


def test_update_refund_excludes_itself_from_refunded_total(seeded_db):
    original = expense(seeded_db, 800)
    refund_id = refund(seeded_db, original, 300)
    tx.update_transaction(seeded_db, refund_id, refund_form(seeded_db, original, 800))
    assert period(seeded_db, "5201") == 0
    with pytest.raises(ValidationFailed) as exc:
        tx.update_transaction(seeded_db, refund_id, refund_form(seeded_db, original, 801))
    assert errors_of(exc) == [("V07", "amount")]


def test_update_refund_cannot_switch_original(seeded_db):
    first = expense(seeded_db, 800)
    second = expense(seeded_db, 800)
    refund_id = refund(seeded_db, first, 300)
    with pytest.raises(ValidationFailed) as exc:
        tx.update_transaction(seeded_db, refund_id, refund_form(seeded_db, second, 300))
    assert ("REFUND_LOCKED", "original_txn_id") in errors_of(exc)
    assert seeded_db.get(Transaction, refund_id).related_txn_id == first


# --- D16 / U10：原交易已有退款時的鎖定 ---------------------------------------------------


@pytest.fixture
def refunded(seeded_db):
    """原交易 800（網購、刷卡、今天），已退 300（今天）。"""
    original = expense(seeded_db, 800)
    refund_id = refund(seeded_db, original, 300)
    return seeded_db, original, refund_id


def test_original_with_refund_cannot_be_deleted(refunded):
    db, original, refund_id = refunded
    with pytest.raises(ValidationFailed) as exc:
        tx.delete_transaction(db, original)
    assert errors_of(exc) == [("REFUND_EXISTS", None)]
    assert db.get(Transaction, original).deleted_at is None

    tx.delete_transaction(db, refund_id)  # 先刪退款
    tx.delete_transaction(db, original)  # 才能刪原交易


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"amount": 299}, "amount"),  # 低於已退金額
        ({"account_id": "5102"}, "account_id"),  # 換支出科目
        ({"txn_date": TODAY + timedelta(days=1)}, "txn_date"),  # 晚於最早的退款
        ({"exclude_from_budget": True}, "exclude_from_budget"),
    ],
)
def test_original_with_refund_locked_fields(refunded, changes, field):
    db, original, _ = refunded
    kwargs = dict(changes)
    if "account_id" in kwargs:
        kwargs["account_id"] = acc(db, kwargs["account_id"])
    form = replace(expense_form(db, 800), **kwargs)
    with pytest.raises(ValidationFailed) as exc:
        tx.update_transaction(db, original, form, confirmed={"V11"})
    assert errors_of(exc) == [("REFUND_LOCKED", field)]
    assert lines(db, original) == [("5201", 800, "MAIN"), ("2101", -800, None)]


def test_original_with_refund_allowed_changes(refunded):
    db, original, _ = refunded
    merchant = Merchant(name="蝦皮")
    db.add(merchant)
    db.commit()
    form = expense_form(
        db,
        300,  # 等於已退金額可以
        method="LINE Pay",  # 換付款方式
        txn_date=TODAY - timedelta(days=3),  # 提早可以
        fee_amount=15,
        fee_account_id=acc(db, "5502"),
        description="改備註",
        merchant_id=merchant.id,
    )
    tx.update_transaction(db, original, form)
    assert lines(db, original) == [
        ("5201", 300, "MAIN"),
        ("5502", 15, "FEE"),
        ("1102", -315, None),
    ]
    assert period(db, "5201") == 0
    # 不同步更新退款（U10）：退款的商家維持原樣
    refund_txn = db.scalars(select(Transaction).where(Transaction.txn_type == "REFUND")).one()
    assert refund_txn.merchant_id is None


def test_lock_released_after_refund_deleted(refunded):
    db, original, refund_id = refunded
    tx.delete_transaction(db, refund_id)
    form = expense_form(db, 100, "5102", txn_date=TODAY + timedelta(days=1))
    tx.update_transaction(db, original, form, confirmed={"V11"})
    assert lines(db, original) == [("5102", 100, "MAIN"), ("2101", -100, None)]


# --- 還原退款（U6）---------------------------------------------------------------------


def test_restore_refund_rechecks_v07_after_original_changed(refunded):
    db, original, refund_id = refunded
    tx.delete_transaction(db, refund_id)
    tx.update_transaction(db, original, expense_form(db, 200))  # 退款刪除期間改小
    with pytest.raises(ValidationFailed) as exc:
        tx.restore_transaction(db, refund_id)
    assert errors_of(exc) == [("V07", "amount")]
    assert db.get(Transaction, refund_id).deleted_at is not None


def test_restore_refund_requires_active_original(refunded):
    db, original, refund_id = refunded
    tx.delete_transaction(db, refund_id)
    tx.delete_transaction(db, original)
    with pytest.raises(ValidationFailed) as exc:
        tx.restore_transaction(db, refund_id)
    assert errors_of(exc) == [("REFUND_ORIGINAL", "original_txn_id")]


def test_restore_refund_rechecks_date(refunded):
    db, original, refund_id = refunded
    tx.delete_transaction(db, refund_id)
    later = expense_form(db, 800, txn_date=TODAY + timedelta(days=2))
    tx.update_transaction(db, original, later, confirmed={"V11"})
    with pytest.raises(ValidationFailed) as exc:
        tx.restore_transaction(db, refund_id)
    assert errors_of(exc) == [("REFUND_DATE", "txn_date")]


def test_restore_refund_ok(refunded):
    db, original, refund_id = refunded
    tx.delete_transaction(db, refund_id)
    tx.restore_transaction(db, refund_id)
    assert period(db, "5201") == 500
