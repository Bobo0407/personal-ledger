"""services/txn_detail.py：交易明細與「快速記帳格式支出」的修改條件（P2 Step 4A）。"""

import pytest
from sqlalchemy import select

from app.models import Merchant, PaymentMethod, Transaction
from app.services import transactions, txn_detail, txn_edit
from app.services.entries import CardPaymentForm, ExpenseForm, IncomeForm, RefundForm
from app.services.errors import NotFound, RuleViolation
from tests.ledger_helpers import TODAY, acc, freeze_today, insert_raw_transaction


@pytest.fixture(autouse=True)
def _today(monkeypatch):
    freeze_today(monkeypatch)


def pm(db, name="LINE Pay") -> int:
    return db.scalar(select(PaymentMethod.id).where(PaymentMethod.name == name))


def quick(db, **kwargs) -> ExpenseForm:
    base = {
        "txn_date": TODAY,
        "amount": 120,
        "account_id": acc(db, "5101"),
        "payment_method_id": pm(db),
    }
    return ExpenseForm(**{**base, **kwargs})


def record(db, form) -> int:
    return transactions.record_expense(db, form, confirmed=["V10", "V11", "V12"]).transaction_id


def lines(detail) -> list[tuple[str, str, int, str | None]]:
    return [(x.account_name, x.effect, x.amount, x.role_label) for x in detail.lines]


# --- 明細 -------------------------------------------------------------------------------


def test_expense_detail(seeded_db):
    m = Merchant(name="7-11")
    seeded_db.add(m)
    seeded_db.commit()
    txn_id = record(
        seeded_db,
        quick(
            seeded_db,
            merchant_id=m.id,
            description="午餐",
            voucher_type="EINVOICE",
            invoice_no="AB12345678",
        ),
    )
    d = txn_detail.get_detail(seeded_db, txn_id)
    assert (d.txn_date, d.txn_type, d.type_label, d.is_deleted) == (TODAY, "EXPENSE", "支出", False)
    assert (d.merchant_name, d.payment_method_name, d.description) == ("7-11", "LINE Pay", "午餐")
    assert (d.voucher_label, d.invoice_no, d.magnitude) == ("載具電子發票", "AB12345678", 120)
    assert lines(d) == [("正餐", "增加", 120, None), ("郵局", "減少", 120, None)]
    assert d.edit_blockers == ()
    assert d.delete_blockers == ()


def test_effects_follow_normal_balance_direction(seeded_db):
    card = record(seeded_db, quick(seeded_db, payment_method_id=pm(seeded_db, "聯邦信用卡")))
    assert lines(txn_detail.get_detail(seeded_db, card))[1] == ("聯邦信用卡", "增加", 120, None)

    payment = transactions.record_card_payment(
        seeded_db, CardPaymentForm(TODAY, 2000, acc(seeded_db, "2101"), acc(seeded_db, "1103"))
    ).transaction_id
    assert lines(txn_detail.get_detail(seeded_db, payment)) == [
        ("聯邦信用卡", "減少", 2000, None),
        ("聯邦活存", "減少", 2000, None),
    ]

    income = transactions.record_income(
        seeded_db, IncomeForm(TODAY, 3000, acc(seeded_db, "4201"), acc(seeded_db, "1102"))
    ).transaction_id
    assert sorted(lines(txn_detail.get_detail(seeded_db, income))) == [
        ("利息收入", "增加", 3000, None),
        ("郵局", "增加", 3000, None),
    ]


def test_fee_line_has_role_label(seeded_db):
    txn_id = record(
        seeded_db, quick(seeded_db, fee_amount=15, fee_account_id=acc(seeded_db, "5501"))
    )
    d = txn_detail.get_detail(seeded_db, txn_id)
    assert ("其他手續費", "增加", 15, "手續費") in lines(d)
    assert d.magnitude == 135


def test_deleted_detail_and_refund_links(seeded_db):
    original = record(seeded_db, quick(seeded_db, amount=1000))
    refund = transactions.record_refund(
        seeded_db, RefundForm(original, TODAY, 600, pm(seeded_db))
    ).transaction_id
    d = txn_detail.get_detail(seeded_db, original)
    assert d.refunded_amount == 600
    assert d.delete_blockers == ("這筆支出已退 600 元，請先刪除退款",)
    assert txn_detail.get_detail(seeded_db, refund).related_txn_id == original

    transactions.delete_transaction(seeded_db, refund)
    d = txn_detail.get_detail(seeded_db, refund)
    assert d.is_deleted and d.deleted_at is not None
    assert d.edit_blockers == ("這類交易目前不提供修改",)
    assert txn_detail.get_detail(seeded_db, original).delete_blockers == ()


def test_not_found(seeded_db):
    with pytest.raises(NotFound):
        txn_detail.get_detail(seeded_db, 999)
    with pytest.raises(NotFound):
        txn_detail.require_editable(seeded_db, 999)


# --- 修改條件 ---------------------------------------------------------------------------


def blockers(db, txn_id) -> tuple[str, ...]:
    return txn_detail.get_detail(db, txn_id).edit_blockers


def test_foreign_currency_expense_is_not_editable(seeded_db):
    """外幣延後到 V1.5：表單沒有外幣欄位，有原幣的支出不開放修改（P2 Step 6）。"""
    txn_id = record(seeded_db, quick(seeded_db, original_currency="USD", original_amount_minor=399))
    assert blockers(seeded_db, txn_id) == (txn_edit.FOREIGN_CURRENCY,)
    with pytest.raises(RuleViolation) as exc:
        txn_detail.require_editable(seeded_db, txn_id)
    assert exc.value.code == "NOT_EDITABLE"


def test_advanced_expense_is_editable(seeded_db):
    """P2 Step 6：手續費、分期利息、不計入預算都在表單上，可以完整修改。"""
    form = quick(
        seeded_db,
        payment_method_id=pm(seeded_db, "聯邦信用卡"),
        fee_amount=15,
        fee_account_id=acc(seeded_db, "5502"),
        interest_amount=300,
        interest_account_id=acc(seeded_db, "5503"),
        exclude_from_budget=True,
    )
    txn_id = record(seeded_db, form)
    assert blockers(seeded_db, txn_id) == ()
    values = txn_detail.edit_values(seeded_db, txn_id)
    assert (values["fee_amount"], values["fee_account_id"]) == ("15", str(acc(seeded_db, "5502")))
    assert values["interest_amount"] == "300"
    assert values["interest_account_id"] == str(acc(seeded_db, "5503"))
    assert values["exclude_from_budget"] == "1"
    assert txn_detail.get_detail(seeded_db, txn_id).exclude_from_budget is True


def test_other_types_deleted_and_drafts_are_not_editable(seeded_db):
    # 收入、轉帳、繳卡費在 Step 5B 開放修改；退款等其他類型仍不提供
    original = record(seeded_db, quick(seeded_db, amount=1000))
    refund = transactions.record_refund(
        seeded_db, RefundForm(original, TODAY, 600, pm(seeded_db))
    ).transaction_id
    assert blockers(seeded_db, refund) == ("這類交易目前不提供修改",)

    deleted = record(seeded_db, quick(seeded_db))
    transactions.delete_transaction(seeded_db, deleted)
    assert blockers(seeded_db, deleted) == ("已刪除的交易要先還原才能修改",)

    draft = insert_raw_transaction(
        seeded_db,
        TODAY.isoformat(),
        ("5101", 50),
        ("1102", -50),
        status="DRAFT",
        txn_type="EXPENSE",
    )
    assert blockers(seeded_db, draft) == ("草稿交易不提供修改",)


def test_odd_expense_shape_is_not_editable(seeded_db):
    """沒有 MAIN 標記的 EXPENSE（例如資料庫裡手寫的）也不能用表單修改。"""
    txn_id = insert_raw_transaction(
        seeded_db, TODAY.isoformat(), ("5101", 50), ("1102", -50), txn_type="EXPENSE"
    )
    assert blockers(seeded_db, txn_id) == (txn_edit.NOT_REBUILDABLE,)


def test_edit_values(seeded_db):
    m = Merchant(name="7-11")
    seeded_db.add(m)
    seeded_db.commit()
    txn_id = record(
        seeded_db,
        quick(seeded_db, merchant_id=m.id, description="午餐", voucher_type="RECEIPT"),
    )
    assert txn_detail.edit_values(seeded_db, txn_id) == {
        "txn_date": TODAY.isoformat(),
        "amount": "120",
        "account_id": str(acc(seeded_db, "5101")),
        "payment_method_id": str(pm(seeded_db)),
        "description": "午餐",
        "merchant_name": "7-11",
        "voucher_type": "RECEIPT",
        "invoice_no": "",
        "fee_amount": "",
        "fee_account_id": "",
        "interest_amount": "",
        "interest_account_id": "",
        "exclude_from_budget": "",
    }


def test_detail_does_not_change_anything(seeded_db):
    txn_id = record(seeded_db, quick(seeded_db))
    before = seeded_db.get(Transaction, txn_id).updated_at
    txn_detail.get_detail(seeded_db, txn_id)
    assert not (seeded_db.new or seeded_db.dirty or seeded_db.deleted)
    assert seeded_db.get(Transaction, txn_id).updated_at == before
