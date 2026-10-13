"""services/audit_history.py：交易異動紀錄的可讀摘要（P2 Step 4B）。"""

import json
from dataclasses import replace
from datetime import date

import pytest
from sqlalchemy import select

from app.models import AuditLog, Merchant, PaymentMethod, Transaction
from app.services import accounts as account_service
from app.services import audit_history, ledger, transactions
from app.services.drafts import draft_from_model
from app.services.entries import ExpenseForm
from tests.ledger_helpers import TODAY, acc, freeze_today


@pytest.fixture(autouse=True)
def _today(monkeypatch):
    freeze_today(monkeypatch)


def pm(db, name="LINE Pay") -> int:
    return db.scalar(select(PaymentMethod.id).where(PaymentMethod.name == name))


def form(db, **kwargs) -> ExpenseForm:
    base = {
        "txn_date": TODAY,
        "amount": 120,
        "account_id": acc(db, "5101"),
        "payment_method_id": pm(db),
    }
    return ExpenseForm(**{**base, **kwargs})


def create(db, **kwargs) -> int:
    return transactions.record_expense(
        db, form(db, **kwargs), confirmed=["V10", "V11", "V12"]
    ).transaction_id


def update(db, txn_id, **kwargs) -> None:
    transactions.update_transaction(db, txn_id, form(db, **kwargs), confirmed=["V10", "V11", "V12"])


def entries(db, txn_id):
    return audit_history.history(db, txn_id)


def last_changes(db, txn_id) -> tuple[str, ...]:
    return entries(db, txn_id)[-1].changes


# --- 動作與排序 ----------------------------------------------------------------------------


def test_create_update_delete_restore_in_order(seeded_db):
    txn_id = create(seeded_db)
    update(seeded_db, txn_id, amount=150)
    update(seeded_db, txn_id, amount=150, description="晚餐")
    transactions.delete_transaction(seeded_db, txn_id)
    transactions.restore_transaction(seeded_db, txn_id)
    got = [(e.action, e.label) for e in entries(seeded_db, txn_id)]
    assert got == [
        ("CREATE", "建立交易"),
        ("UPDATE", "修改交易"),
        ("UPDATE", "修改交易"),
        ("DELETE", "刪除交易"),
        ("RESTORE", "還原交易"),
    ]
    history = entries(seeded_db, txn_id)
    assert history[0].changes == ("金額 120",)
    assert history[1].changes == (
        "金額：120 → 150",
        "分錄：5101 正餐 增加 120、1102 郵局 減少 120 → 5101 正餐 增加 150、1102 郵局 減少 150",
    )
    assert history[2].changes == ("備註：（無） → 晚餐",)
    assert history[3].changes == ()
    assert history[4].changes == ()


def test_same_second_entries_ordered_by_id(seeded_db):
    """occurred_at 只到秒：同一秒的多筆紀錄以 id 排序（clock 固定，全部同一秒）。"""
    txn_id = create(seeded_db)
    for amount in (130, 140, 150):
        update(seeded_db, txn_id, amount=amount)
    logs = seeded_db.scalars(select(AuditLog).where(AuditLog.entity_id == txn_id)).all()
    assert len({log.occurred_at for log in logs}) == 1
    amounts = [e.changes[0] for e in entries(seeded_db, txn_id)[1:]]
    assert amounts == ["金額：120 → 130", "金額：130 → 140", "金額：140 → 150"]


def test_user_id_is_kept_in_data_but_not_in_entries(seeded_db):
    from app import security
    from app.models import User

    user = User(username="bo", password_hash=security.hash_password("pw-123456789"))
    seeded_db.add(user)
    seeded_db.commit()
    txn_id = create(seeded_db)
    transactions.delete_transaction(seeded_db, txn_id, user_id=user.id)
    stored = seeded_db.scalars(
        select(AuditLog.user_id).where(AuditLog.entity_id == txn_id).order_by(AuditLog.id)
    ).all()
    assert stored == [None, user.id]
    assert all(not hasattr(e, "user_id") for e in entries(seeded_db, txn_id))


# --- UPDATE 摘要 ---------------------------------------------------------------------------


def test_header_field_changes(seeded_db):
    seven = Merchant(name="7-11")
    seeded_db.add(seven)
    seeded_db.commit()
    txn_id = create(seeded_db)
    update(
        seeded_db,
        txn_id,
        txn_date=date(2026, 10, 14),
        merchant_id=seven.id,
        description="午餐",
        voucher_type="EINVOICE",
        invoice_no="AB12345678",
    )
    assert last_changes(seeded_db, txn_id) == (
        "日期：2026-10-15 → 2026-10-14",
        "商家：（無） → 7-11",
        "備註：（無） → 午餐",
        "憑證類型：（無） → 載具電子發票",
        "發票號碼：（無） → AB12345678",
    )


def test_account_and_payment_method_changes_show_full_postings(seeded_db):
    txn_id = create(seeded_db)
    update(
        seeded_db,
        txn_id,
        amount=150,
        account_id=acc(seeded_db, "5102"),
        payment_method_id=pm(seeded_db, "現金"),
    )
    assert last_changes(seeded_db, txn_id) == (
        "金額：120 → 150",
        "付款方式：LINE Pay → 現金",
        "分錄：5101 正餐 增加 120、1102 郵局 減少 120 → 5102 飲料 增加 150、1101 現金 減少 150",
    )


def test_exclude_from_budget_change(seeded_db):
    txn_id = create(seeded_db)
    update(seeded_db, txn_id, exclude_from_budget=True)
    assert last_changes(seeded_db, txn_id) == ("不計入預算：否 → 是",)


def test_unchanged_update(seeded_db):
    txn_id = create(seeded_db)
    update(seeded_db, txn_id)
    assert last_changes(seeded_db, txn_id) == ("內容沒有變更",)


def test_fee_role_is_labelled(seeded_db):
    txn_id = create(seeded_db)
    update(seeded_db, txn_id, fee_amount=15, fee_account_id=acc(seeded_db, "5501"))
    changes = last_changes(seeded_db, txn_id)
    assert "金額：120 → 135" in changes
    assert any("5501 其他手續費 增加 15（手續費）" in c for c in changes)


# --- 只有分錄內容（memo、原幣）改變 ----------------------------------------------------------


def test_memo_only_change_is_not_reported_as_unchanged(seeded_db):
    txn_id = create(seeded_db)
    txn = seeded_db.get(Transaction, txn_id)
    draft = draft_from_model(txn)
    draft = replace(draft, postings=(replace(draft.postings[0], memo="備忘"), *draft.postings[1:]))
    seeded_db.expire_all()
    ledger.update(seeded_db, txn_id, draft)
    assert last_changes(seeded_db, txn_id) == ("分錄內容已變更",)


def test_foreign_amount_only_change_is_not_reported_as_unchanged(seeded_db):
    txn_id = create(seeded_db, original_currency="USD", original_amount_minor=399)
    update(seeded_db, txn_id, original_currency="USD", original_amount_minor=400)
    assert last_changes(seeded_db, txn_id) == ("分錄內容已變更",)


def test_reordered_postings_are_not_a_change(seeded_db):
    before = {
        "postings": [
            {"line_no": 1, "account_id": 1, "account_code": "1", "amount": 5, "line_role": "MAIN"},
            {"line_no": 2, "account_id": 2, "account_code": "2", "amount": -5, "line_role": None},
        ]
    }
    after = {
        "postings": [
            {"line_no": 1, "account_id": 2, "account_code": "2", "amount": -5, "line_role": None},
            {"line_no": 2, "account_id": 1, "account_code": "1", "amount": 5, "line_role": "MAIN"},
        ]
    }
    assert audit_history.update_changes(audit_history._Names(None), before, after) == (
        "內容沒有變更",
    )


# --- 主資料名稱：用現在的名稱，保留科目代碼，查不到時退回 ---------------------------------------


def test_renamed_account_shows_current_name_with_code(seeded_db):
    txn_id = create(seeded_db)
    update(seeded_db, txn_id, amount=150)
    account_service.rename_account(seeded_db, acc(seeded_db, "5101"), "午晚餐")
    assert "5101 午晚餐 增加 150" in last_changes(seeded_db, txn_id)[-1]


def _add_log(db, txn_id, before, after):
    db.add(
        AuditLog(
            entity="transaction",
            entity_id=txn_id,
            action="UPDATE",
            before_json=json.dumps(before, ensure_ascii=False),
            after_json=json.dumps(after, ensure_ascii=False),
        )
    )
    db.commit()


def test_missing_master_data_falls_back(seeded_db):
    txn_id = create(seeded_db)
    posting = {
        "line_no": 1,
        "account_id": 9999,
        "account_code": "9999",
        "amount": 50,
        "line_role": None,
    }
    other = {
        "line_no": 2,
        "account_id": 9998,
        "account_code": None,
        "amount": -50,
        "line_role": None,
    }
    _add_log(
        seeded_db,
        txn_id,
        {"merchant_id": None, "payment_method_id": None, "postings": []},
        {"merchant_id": 777, "payment_method_id": 888, "postings": [posting, other]},
    )
    assert last_changes(seeded_db, txn_id) == (
        "金額：0 → 50",
        "商家：（無） → #777",
        "付款方式：（無） → #888",
        "分錄：（無） → 9999 50、#9998 50",
    )


def test_history_has_no_raw_snapshot_text(seeded_db):
    txn_id = create(seeded_db)
    update(seeded_db, txn_id, amount=150)
    text = repr(entries(seeded_db, txn_id))
    for raw in ('"postings"', "account_code", "before_json", "source_ref", "user_id"):
        assert raw not in text
