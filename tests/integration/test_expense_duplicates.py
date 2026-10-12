"""P2 Step 2：V12 重複交易確認、V08 發票號碼、Web 一次性送出權杖（service 層）。"""

from dataclasses import replace
from datetime import date

import pytest
from sqlalchemy import func, select

from app.models import AuditLog, Merchant, PaymentMethod, Transaction
from app.services import transactions, validation
from app.services.entries import ExpenseForm
from app.services.validation import ConfirmationRequired, ValidationFailed
from tests.ledger_helpers import TODAY, acc, freeze_today, insert_raw_transaction

TOKEN = "tok_" + "x" * 28


@pytest.fixture(autouse=True)
def _today(monkeypatch):
    freeze_today(monkeypatch)


@pytest.fixture
def seven(seeded_db) -> int:
    m = Merchant(name="7-11")
    seeded_db.add(m)
    seeded_db.commit()
    return m.id


def expense(db, *, amount=85, merchant_id=None, txn_date=TODAY, **kwargs) -> ExpenseForm:
    pm = db.scalar(select(PaymentMethod.id).where(PaymentMethod.name == "LINE Pay"))
    return ExpenseForm(
        txn_date=txn_date,
        amount=amount,
        account_id=acc(db, "5102"),
        payment_method_id=pm,
        merchant_id=merchant_id,
        **kwargs,
    )


def record(db, form, **kwargs):
    return transactions.record_expense(db, form, **kwargs)


def pending_codes(exc_info) -> list[str]:
    return [i.code for i in exc_info.value.pending]


def error_codes(exc_info) -> list[tuple[str, str | None]]:
    return [(i.code, i.field) for i in exc_info.value.result.errors]


def count(db, model) -> int:
    return db.scalar(select(func.count()).select_from(model))


# --- V12 -------------------------------------------------------------------------------


def test_t20_same_day_merchant_and_amount_requires_confirmation(seeded_db, seven):
    record(seeded_db, expense(seeded_db, merchant_id=seven))
    with pytest.raises(ConfirmationRequired) as exc:
        record(seeded_db, expense(seeded_db, merchant_id=seven))
    assert pending_codes(exc) == ["V12"]
    assert exc.value.pending[0].message == "今天已經有一筆 7-11 85 元，是重複嗎？"

    record(seeded_db, expense(seeded_db, merchant_id=seven), confirmed=["V12"])
    assert count(seeded_db, Transaction) == 2


def test_v12_message_uses_date_when_not_today(seeded_db, seven):
    day = date(2026, 10, 3)
    record(seeded_db, expense(seeded_db, merchant_id=seven, txn_date=day))
    with pytest.raises(ConfirmationRequired) as exc:
        record(seeded_db, expense(seeded_db, merchant_id=seven, txn_date=day))
    assert exc.value.pending[0].message == "10/3已經有一筆 7-11 85 元，是重複嗎？"


@pytest.mark.parametrize(
    "change",
    [
        {"amount": 86},
        {"txn_date": date(2026, 10, 14)},
        {"merchant_id": None},
    ],
)
def test_v12_not_triggered_when_any_key_differs(seeded_db, seven, change):
    record(seeded_db, expense(seeded_db, merchant_id=seven))
    form = replace(expense(seeded_db, merchant_id=seven), **change)
    record(seeded_db, form)
    assert count(seeded_db, Transaction) == 2


def test_v12_not_triggered_for_different_merchant(seeded_db, seven):
    other = Merchant(name="全家")
    seeded_db.add(other)
    seeded_db.commit()
    record(seeded_db, expense(seeded_db, merchant_id=seven))
    record(seeded_db, expense(seeded_db, merchant_id=other.id))


def test_v12_compares_main_amount_only(seeded_db, seven):
    """手續費不影響「同金額」：MAIN 相同就要確認，總額相同但 MAIN 不同就不必。"""
    fee = {"fee_amount": 15, "fee_account_id": acc(seeded_db, "5501")}
    record(seeded_db, expense(seeded_db, merchant_id=seven, **fee))  # MAIN 85、總額 100
    with pytest.raises(ConfirmationRequired) as exc:
        record(seeded_db, expense(seeded_db, merchant_id=seven))  # MAIN 85
    assert pending_codes(exc) == ["V12"]
    record(seeded_db, expense(seeded_db, merchant_id=seven, amount=100))  # 總額相同、MAIN 不同


def test_v12_ignores_deleted_original(seeded_db, seven):
    first = record(seeded_db, expense(seeded_db, merchant_id=seven))
    transactions.delete_transaction(seeded_db, first.transaction_id)
    record(seeded_db, expense(seeded_db, merchant_id=seven))


def test_v12_ignores_draft(seeded_db, seven):
    txn_id = insert_raw_transaction(
        seeded_db,
        TODAY.isoformat(),
        ("5102", 85),
        ("1102", -85),
        status="DRAFT",
        txn_type="EXPENSE",
    )
    txn = seeded_db.get(Transaction, txn_id)
    txn.merchant_id = seven
    txn.postings[0].line_role = "MAIN"
    seeded_db.commit()
    record(seeded_db, expense(seeded_db, merchant_id=seven))


def test_v12_on_update_excludes_self_and_only_rechecks_changes(seeded_db, seven):
    a = record(seeded_db, expense(seeded_db, merchant_id=seven)).transaction_id
    b = record(seeded_db, expense(seeded_db, merchant_id=seven, amount=90)).transaction_id

    # 只改備註：不重問（自己不算重複）
    transactions.update_transaction(
        seeded_db, a, expense(seeded_db, merchant_id=seven, description="午餐")
    )
    # 把 B 改成和 A 一樣的金額：要確認
    with pytest.raises(ConfirmationRequired) as exc:
        transactions.update_transaction(seeded_db, b, expense(seeded_db, merchant_id=seven))
    assert pending_codes(exc) == ["V12"]
    transactions.update_transaction(
        seeded_db, b, expense(seeded_db, merchant_id=seven), confirmed=["V12"]
    )


def test_restore_does_not_rerun_v12(seeded_db, seven):
    a = record(seeded_db, expense(seeded_db, merchant_id=seven)).transaction_id
    transactions.delete_transaction(seeded_db, a)
    record(seeded_db, expense(seeded_db, merchant_id=seven))
    transactions.restore_transaction(seeded_db, a)  # 不丟 ConfirmationRequired


# --- V08 -------------------------------------------------------------------------------


@pytest.mark.parametrize("invoice", ["ab12345678", "AB1234567", "AB-12345678", "A112345678"])
def test_v08_format(seeded_db, invoice):
    with pytest.raises(ValidationFailed) as exc:
        record(seeded_db, expense(seeded_db, invoice_no=invoice))
    assert error_codes(exc) == [("V08", "invoice_no")]
    assert count(seeded_db, Transaction) == 0


def test_v08_duplicate_on_create(seeded_db):
    record(seeded_db, expense(seeded_db, invoice_no="AB12345678", txn_date=date(2026, 10, 3)))
    with pytest.raises(ValidationFailed) as exc:
        record(seeded_db, expense(seeded_db, invoice_no="AB12345678", amount=99))
    assert error_codes(exc) == [("V08", "invoice_no")]
    assert exc.value.result.errors[0].message == "這張發票已記在 10/3 的交易"


def test_t21_deleted_transaction_releases_invoice(seeded_db):
    first = record(seeded_db, expense(seeded_db, invoice_no="AB12345678"))
    transactions.delete_transaction(seeded_db, first.transaction_id)
    record(seeded_db, expense(seeded_db, invoice_no="AB12345678", amount=99))
    assert count(seeded_db, Transaction) == 2


def test_v08_update_keeps_own_invoice(seeded_db):
    txn_id = record(seeded_db, expense(seeded_db, invoice_no="AB12345678")).transaction_id
    transactions.update_transaction(
        seeded_db, txn_id, expense(seeded_db, invoice_no="AB12345678", description="改備註")
    )


def test_v08_update_to_another_transactions_invoice(seeded_db):
    record(seeded_db, expense(seeded_db, invoice_no="AB12345678"))
    other = record(seeded_db, expense(seeded_db, invoice_no="CD12345678", amount=99))
    with pytest.raises(ValidationFailed) as exc:
        transactions.update_transaction(
            seeded_db, other.transaction_id, expense(seeded_db, invoice_no="AB12345678", amount=99)
        )
    assert error_codes(exc) == [("V08", "invoice_no")]
    assert seeded_db.get(Transaction, other.transaction_id).invoice_no == "CD12345678"


def test_restore_invoice_collision_still_v08(seeded_db):
    first = record(seeded_db, expense(seeded_db, invoice_no="AB12345678"))
    transactions.delete_transaction(seeded_db, first.transaction_id)
    record(seeded_db, expense(seeded_db, invoice_no="AB12345678", amount=99))
    with pytest.raises(ValidationFailed) as exc:
        transactions.restore_transaction(seeded_db, first.transaction_id)
    assert error_codes(exc) == [("V08", "invoice_no")]


@pytest.fixture
def skip_invoice_precheck(monkeypatch):
    """模擬兩個請求同時通過 validate() 的發票檢查（只剩資料庫索引這道防線）。"""
    monkeypatch.setattr(validation, "_check_invoice", lambda *args, **kwargs: None)


def test_v08_db_collision_on_post_becomes_v08(seeded_db, skip_invoice_precheck):
    record(seeded_db, expense(seeded_db, invoice_no="AB12345678"))
    with pytest.raises(ValidationFailed) as exc:
        record(seeded_db, expense(seeded_db, invoice_no="AB12345678", amount=99))
    assert error_codes(exc) == [("V08", "invoice_no")]
    assert count(seeded_db, Transaction) == 1
    assert count(seeded_db, AuditLog) == 1


def test_v08_db_collision_on_update_becomes_v08(seeded_db, skip_invoice_precheck):
    record(seeded_db, expense(seeded_db, invoice_no="AB12345678"))
    other = record(seeded_db, expense(seeded_db, invoice_no="CD12345678", amount=99))
    with pytest.raises(ValidationFailed) as exc:
        transactions.update_transaction(
            seeded_db, other.transaction_id, expense(seeded_db, invoice_no="AB12345678", amount=99)
        )
    assert error_codes(exc) == [("V08", "invoice_no")]
    assert seeded_db.get(Transaction, other.transaction_id).invoice_no == "CD12345678"


def test_other_integrity_errors_are_not_mapped_to_v08(seeded_db, skip_invoice_precheck):
    from sqlalchemy.exc import IntegrityError

    # 格式錯誤只剩資料庫 CHECK 擋：沒有任何交易占用這個號碼 → 原樣拋出
    with pytest.raises(IntegrityError):
        record(seeded_db, expense(seeded_db, invoice_no="bad"))
    with pytest.raises(IntegrityError):
        record(seeded_db, expense(seeded_db, voucher_type="PHOTO"))
    assert count(seeded_db, Transaction) == 0


# --- 一次性送出權杖 -----------------------------------------------------------------------


def test_token_second_submit_returns_first_without_writing(seeded_db):
    first = record(seeded_db, expense(seeded_db), submission_token=TOKEN, user_id=None)
    assert first.replayed is False
    txn = seeded_db.get(Transaction, first.transaction_id)
    assert (txn.source, txn.source_ref) == ("MANUAL", f"submit:{TOKEN}")

    # 第二次：內容即使不同、甚至不合法，也不驗證、不寫入，回傳第一次那筆
    second = record(seeded_db, expense(seeded_db, amount=0), submission_token=TOKEN)
    assert second.replayed is True
    assert second.transaction_id == first.transaction_id
    assert second.notices == ()
    assert count(seeded_db, Transaction) == 1
    assert count(seeded_db, AuditLog) == 1


def test_token_stays_used_after_soft_delete(seeded_db):
    first = record(seeded_db, expense(seeded_db), submission_token=TOKEN)
    transactions.delete_transaction(seeded_db, first.transaction_id)
    again = record(seeded_db, expense(seeded_db), submission_token=TOKEN)
    assert again.replayed is True
    assert again.transaction_id == first.transaction_id
    assert count(seeded_db, Transaction) == 1


def test_token_replay_skips_v12(seeded_db, seven):
    """重送不是新交易，不能跳出「是重複嗎？」。"""
    record(seeded_db, expense(seeded_db, merchant_id=seven), submission_token=TOKEN)
    assert record(seeded_db, expense(seeded_db, merchant_id=seven), submission_token=TOKEN).replayed


def test_different_tokens_create_separate_transactions(seeded_db):
    record(seeded_db, expense(seeded_db), submission_token=TOKEN)
    record(seeded_db, expense(seeded_db), submission_token=TOKEN + "2")
    assert count(seeded_db, Transaction) == 2


def test_token_not_consumed_by_rejected_submission(seeded_db):
    with pytest.raises(ValidationFailed):
        record(seeded_db, expense(seeded_db, amount=0), submission_token=TOKEN)
    with pytest.raises(ConfirmationRequired):
        record(seeded_db, expense(seeded_db, amount=12000), submission_token=TOKEN)
    result = record(
        seeded_db, expense(seeded_db, amount=12000), submission_token=TOKEN, confirmed=["V10"]
    )
    assert result.replayed is False


def test_token_concurrent_duplicate_hits_unique_and_replays(seeded_db, monkeypatch):
    first = record(seeded_db, expense(seeded_db), submission_token=TOKEN)
    real = transactions._replayed
    calls = {"n": 0}

    def flaky(db, ref):
        calls["n"] += 1
        return None if calls["n"] == 1 else real(db, ref)

    monkeypatch.setattr(transactions, "_replayed", flaky)
    second = record(seeded_db, expense(seeded_db), submission_token=TOKEN)
    assert second.replayed is True
    assert second.transaction_id == first.transaction_id
    assert count(seeded_db, Transaction) == 1
    assert count(seeded_db, AuditLog) == 1


def test_token_concurrent_duplicate_with_invoice_replays_not_v08(
    seeded_db, monkeypatch, skip_invoice_precheck
):
    """同一個權杖、同一張發票同時送出：後到的應該是「重送」，不是「發票重複」。"""
    first = record(seeded_db, expense(seeded_db, invoice_no="AB12345678"), submission_token=TOKEN)
    real = transactions._replayed
    calls = {"n": 0}

    def flaky(db, ref):
        calls["n"] += 1
        return None if calls["n"] == 1 else real(db, ref)

    monkeypatch.setattr(transactions, "_replayed", flaky)
    second = record(seeded_db, expense(seeded_db, invoice_no="AB12345678"), submission_token=TOKEN)
    assert second.replayed is True
    assert second.transaction_id == first.transaction_id


def test_find_replay_is_read_only_lookup(seeded_db):
    assert transactions.find_replay(seeded_db, TOKEN) is None
    first = record(seeded_db, expense(seeded_db), submission_token=TOKEN)
    found = transactions.find_replay(seeded_db, TOKEN)
    assert found is not None
    assert (found.transaction_id, found.replayed) == (first.transaction_id, True)
    assert not (seeded_db.new or seeded_db.dirty or seeded_db.deleted)
    assert count(seeded_db, Transaction) == 1
