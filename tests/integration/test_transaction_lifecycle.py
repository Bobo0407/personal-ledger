"""交易生命週期：修改、軟刪除、還原與稽核（Step 6；T24、T25、U1–U6、U14、D14）。"""

import json
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.models import AuditLog, Posting, Transaction
from app.services import accounts as account_service
from app.services import audit, integrity, ledger
from app.services import ledger_queries as q
from app.services import payment_methods as pm_service
from app.services import transactions as tx
from app.services.entries import (
    AdjustmentForm,
    ExpenseForm,
    IncomeForm,
    ManualForm,
    ManualLine,
    OpeningForm,
    OpeningLine,
    TransferForm,
)
from app.services.errors import NotFound
from app.services.ledger import SessionNotClean
from app.services.validation import ConfirmationRequired, ValidationFailed
from tests.ledger_helpers import TODAY, acc, freeze_today, post_manual

FUTURE = TODAY + timedelta(days=5)


@pytest.fixture(autouse=True)
def _today(monkeypatch):
    freeze_today(monkeypatch)


def pm(db, name: str) -> int:
    return next(p.id for p in pm_service.list_payment_methods(db, False) if p.name == name)


def lines(db, txn_id: int) -> list[tuple[int, str, int, str | None]]:
    txn = db.get(Transaction, txn_id)
    return [(p.line_no, p.account.code, p.amount, p.line_role) for p in txn.postings]


def expense_form(db, amount, account="5101", method="LINE Pay", txn_date=TODAY, **kw):
    return ExpenseForm(txn_date, amount, acc(db, account), pm(db, method), **kw)


def expense(db, amount, account="5101", method="LINE Pay", txn_date=TODAY, **kw) -> int:
    form = expense_form(db, amount, account, method, txn_date, **kw)
    return tx.record_expense(db, form, confirmed={"V10", "V11"}).transaction_id


def audits(db, txn_id: int) -> list[AuditLog]:
    stmt = select(AuditLog).where(AuditLog.entity_id == txn_id).order_by(AuditLog.id)
    return list(db.scalars(stmt))


def errors_of(exc_info) -> list[tuple[str, str | None]]:
    return [(i.code, i.field) for i in exc_info.value.result.errors]


def balance(db, code: str) -> int:
    return q.balance(db, acc(db, code))


# --- T24、T25 ---------------------------------------------------------------------


def test_t24_update_with_audit(seeded_db):
    txn_id = expense(seeded_db, 120)
    tx.update_transaction(seeded_db, txn_id, expense_form(seeded_db, 150), user_id=None)

    assert balance(seeded_db, "1102") == -150
    assert lines(seeded_db, txn_id) == [(1, "5101", 150, "MAIN"), (2, "1102", -150, None)]
    assert seeded_db.query(Posting).count() == 2  # 舊分錄被替換，不是累加

    logs = audits(seeded_db, txn_id)
    assert [log.action for log in logs] == ["CREATE", "UPDATE"]
    before, after = json.loads(logs[1].before_json), json.loads(logs[1].after_json)
    assert [p["amount"] for p in before["postings"]] == [120, -120]
    assert [p["amount"] for p in after["postings"]] == [150, -150]
    assert after["postings"][0]["account_code"] == "5101"
    assert integrity.check_all(seeded_db) == []


def test_t25_soft_delete_and_restore(seeded_db):
    txn_id = expense(seeded_db, 120)
    tx.delete_transaction(seeded_db, txn_id)

    txn = seeded_db.get(Transaction, txn_id)
    assert txn.deleted_at is not None
    assert len(txn.postings) == 2  # 分錄保留
    assert balance(seeded_db, "1102") == 0  # 已刪除的交易不計入

    result = tx.restore_transaction(seeded_db, txn_id)
    assert result.transaction_id == txn_id
    assert seeded_db.get(Transaction, txn_id).deleted_at is None
    assert balance(seeded_db, "1102") == -120

    logs = audits(seeded_db, txn_id)
    assert [log.action for log in logs] == ["CREATE", "DELETE", "RESTORE"]
    assert json.loads(logs[1].before_json)["deleted_at"] is None
    assert json.loads(logs[1].after_json)["deleted_at"] is not None
    assert json.loads(logs[2].after_json)["deleted_at"] is None


def test_update_keeps_source_and_status(seeded_db):
    txn_id = expense(seeded_db, 120)
    tx.update_transaction(
        seeded_db, txn_id, expense_form(seeded_db, 120, description="午餐", invoice_no="AB12345678")
    )
    txn = seeded_db.get(Transaction, txn_id)
    assert (txn.status, txn.source, txn.description, txn.invoice_no) == (
        "POSTED",
        "MANUAL",
        "午餐",
        "AB12345678",
    )


# --- U1、U5：不能做的操作 -----------------------------------------------------------------


def test_txn_type_is_immutable(seeded_db):
    txn_id = expense(seeded_db, 120)
    form = IncomeForm(TODAY, 120, acc(seeded_db, "4201"), acc(seeded_db, "1102"))
    with pytest.raises(ValidationFailed) as exc:
        tx.update_transaction(seeded_db, txn_id, form)
    assert errors_of(exc) == [("TXN_TYPE_IMMUTABLE", None)]


def test_transfer_edited_into_card_becomes_card_payment_and_is_rejected(seeded_db):
    form = TransferForm(TODAY, 500, acc(seeded_db, "1102"), acc(seeded_db, "1103"))
    txn_id = tx.record_transfer(seeded_db, form).transaction_id
    to_card = TransferForm(TODAY, 500, acc(seeded_db, "1102"), acc(seeded_db, "2101"))
    with pytest.raises(ValidationFailed) as exc:
        tx.update_transaction(seeded_db, txn_id, to_card)  # D9 → CARD_PAYMENT
    assert errors_of(exc) == [("TXN_TYPE_IMMUTABLE", None)]


def test_deleted_transaction_must_be_restored_before_update(seeded_db):
    txn_id = expense(seeded_db, 120)
    tx.delete_transaction(seeded_db, txn_id)
    with pytest.raises(ValidationFailed) as exc:
        tx.update_transaction(seeded_db, txn_id, expense_form(seeded_db, 150))
    assert errors_of(exc) == [("TXN_DELETED", None)]
    with pytest.raises(ValidationFailed) as exc:
        tx.delete_transaction(seeded_db, txn_id)
    assert errors_of(exc) == [("TXN_DELETED", None)]


def test_restore_active_transaction(seeded_db):
    txn_id = expense(seeded_db, 120)
    with pytest.raises(ValidationFailed) as exc:
        tx.restore_transaction(seeded_db, txn_id)
    assert errors_of(exc) == [("TXN_NOT_DELETED", None)]


def test_missing_transaction(seeded_db):
    with pytest.raises(NotFound):
        tx.update_transaction(seeded_db, 999, expense_form(seeded_db, 1))
    with pytest.raises(NotFound):
        tx.update_transaction(seeded_db, 999, ManualForm(TODAY, ()))
    with pytest.raises(NotFound):
        tx.delete_transaction(seeded_db, 999)
    with pytest.raises(NotFound):
        tx.restore_transaction(seeded_db, 999)


def test_lifecycle_refuses_dirty_session(seeded_db):
    txn_id = expense(seeded_db, 120)
    seeded_db.get(Transaction, txn_id).description = "未提交的變更"
    with pytest.raises(SessionNotClean):
        tx.update_transaction(seeded_db, txn_id, expense_form(seeded_db, 150))
    with pytest.raises(SessionNotClean):
        tx.delete_transaction(seeded_db, txn_id)
    seeded_db.rollback()


# --- 同一個資料庫交易：任何錯誤整批回滾 ---------------------------------------------------


@pytest.mark.parametrize("operation", ["update", "delete", "restore"])
def test_rollback_when_audit_fails(seeded_db, monkeypatch, operation):
    txn_id = expense(seeded_db, 120)
    if operation == "restore":
        tx.delete_transaction(seeded_db, txn_id)
    snapshot = ledger.transaction_snapshot(seeded_db, seeded_db.get(Transaction, txn_id))
    log_count = seeded_db.query(AuditLog).count()

    def boom(*args, **kwargs):
        raise RuntimeError("audit failed")

    monkeypatch.setattr(audit, "record", boom)
    with pytest.raises(RuntimeError):
        if operation == "update":
            tx.update_transaction(seeded_db, txn_id, expense_form(seeded_db, 150))
        elif operation == "delete":
            tx.delete_transaction(seeded_db, txn_id)
        else:
            tx.restore_transaction(seeded_db, txn_id)

    seeded_db.expire_all()
    txn = seeded_db.get(Transaction, txn_id)
    assert ledger.transaction_snapshot(seeded_db, txn) == snapshot
    assert seeded_db.query(AuditLog).count() == log_count


# --- D14：只檢查被改動的分錄 --------------------------------------------------------------


def test_d14_unchanged_inactive_account_can_stay(seeded_db):
    txn_id = expense(seeded_db, 120, "5102", "台灣Pay")
    account_service.deactivate_account(seeded_db, acc(seeded_db, "5102"))
    pm_service.deactivate_payment_method(seeded_db, pm(seeded_db, "台灣Pay"))

    # 只改備註：停用的科目與付款方式都沒動，可以儲存
    same = expense_form(seeded_db, 120, "5102", "台灣Pay", description="x")
    tx.update_transaction(seeded_db, txn_id, same)
    # 改金額：MAIN 分錄被改動 → V04；付款方式沒換 → 不檢查
    with pytest.raises(ValidationFailed) as exc:
        tx.update_transaction(seeded_db, txn_id, expense_form(seeded_db, 150, "5102", "台灣Pay"))
    assert [(i.code, i.line) for i in exc.value.result.errors] == [("V04", 1)]


def test_d14_changed_payment_method_must_be_active(seeded_db):
    txn_id = expense(seeded_db, 120)
    pm_service.deactivate_payment_method(seeded_db, pm(seeded_db, "台灣Pay"))
    with pytest.raises(ValidationFailed) as exc:
        tx.update_transaction(seeded_db, txn_id, expense_form(seeded_db, 120, method="台灣Pay"))
    assert errors_of(exc) == [("V04", "payment_method_id")]


def manual_lines(db, *rows) -> tuple[ManualLine, ...]:
    return tuple(ManualLine(acc(db, code), amount, memo=memo) for code, amount, memo in rows)


def test_d14_duplicate_postings_use_multiset(seeded_db):
    """回歸測試：同一身分的分錄出現兩次、舊版本只有兩次，新版本三次 → 第三筆是新分錄。"""
    rows = (("5102", 100, None), ("5102", 100, None), ("1101", -200, None))
    txn_id = post_manual(seeded_db, TODAY, *((c, a) for c, a, _ in rows)).transaction_id
    account_service.deactivate_account(seeded_db, acc(seeded_db, "5102"))

    # 只改 memo：分錄身分不含 memo，可以儲存
    memo_only = ManualForm(
        TODAY,
        manual_lines(seeded_db, ("5102", 100, "早"), ("5102", 100, "晚"), ("1101", -200, None)),
    )
    tx.update_transaction(seeded_db, txn_id, memo_only)

    # 多一筆一樣的 5102 100：用 set 比對會誤判成「沒改」，用 Counter 才會擋下第三筆
    more = ManualForm(
        TODAY,
        manual_lines(
            seeded_db,
            ("5102", 100, None),
            ("5102", 100, None),
            ("5102", 100, None),
            ("1101", -300, None),
        ),
    )
    with pytest.raises(ValidationFailed) as exc:
        tx.update_transaction(seeded_db, txn_id, more)
    assert [(i.code, i.line) for i in exc.value.result.errors] == [("V04", 3)]


# --- U3：V10、V11 只在相關欄位改變時重問 ---------------------------------------------------


def test_v10_only_when_magnitude_changes(seeded_db):
    txn_id = expense(seeded_db, 20_000)
    tx.update_transaction(seeded_db, txn_id, expense_form(seeded_db, 20_000, description="x"))
    with pytest.raises(ConfirmationRequired) as exc:
        tx.update_transaction(seeded_db, txn_id, expense_form(seeded_db, 25_000))
    assert [i.code for i in exc.value.pending] == ["V10"]
    tx.update_transaction(seeded_db, txn_id, expense_form(seeded_db, 5_000))  # 不超過門檻


def test_v11_only_when_date_changes_to_future(seeded_db):
    txn_id = expense(seeded_db, 120, txn_date=FUTURE)
    tx.update_transaction(seeded_db, txn_id, expense_form(seeded_db, 130, txn_date=FUTURE))
    with pytest.raises(ConfirmationRequired) as exc:
        tx.update_transaction(
            seeded_db, txn_id, expense_form(seeded_db, 130, txn_date=FUTURE + timedelta(days=1))
        )
    assert [i.code for i in exc.value.pending] == ["V11"]


# --- U4：V13 用淨變動與日期邊界 --------------------------------------------------------------


@pytest.fixture
def cash_100(seeded_db):
    post_manual(seeded_db, TODAY, ("1101", 100), ("4201", -100))
    return seeded_db


def v13(result) -> list[str]:
    return [i.message for i in result.notices if i.code == "V13"]


def test_v13_update_uses_net_change(cash_100):
    db = cash_100
    txn_id = expense(db, 80, method="現金")  # 現金 20
    assert v13(tx.update_transaction(db, txn_id, expense_form(db, 150, method="現金"))) == [
        "現金餘額變成 −50，可能漏記了提款或收入"
    ]
    # 已經是負數，但這次修改讓餘額變多 → 不提醒
    assert v13(tx.update_transaction(db, txn_id, expense_form(db, 120, method="現金"))) == []


def test_v13_future_expense_moved_to_today(cash_100):
    db = cash_100
    txn_id = expense(db, 150, method="現金", txn_date=FUTURE)  # 未來日期不影響今天
    assert balance(db, "1101") == 100
    result = tx.update_transaction(db, txn_id, expense_form(db, 150, method="現金"))
    assert v13(result) == ["現金餘額變成 −50，可能漏記了提款或收入"]


def test_v13_today_expense_moved_to_future(cash_100):
    db = cash_100
    txn_id = expense(db, 150, method="現金")
    form = expense_form(db, 200, method="現金", txn_date=FUTURE)
    result = tx.update_transaction(db, txn_id, form, confirmed={"V11"})
    assert v13(result) == []  # 今天的餘額從 −50 回到 100
    assert balance(db, "1101") == 100


def test_v13_on_restore(cash_100):
    db = cash_100
    txn_id = expense(db, 150, method="現金")
    tx.delete_transaction(db, txn_id)
    assert v13(tx.restore_transaction(db, txn_id)) == ["現金餘額變成 −50，可能漏記了提款或收入"]


# --- 期初（D12、U14）與發票（U6）-----------------------------------------------------------


def opening(db, *rows) -> int:
    form = OpeningForm(TODAY, tuple(OpeningLine(acc(db, c), b) for c, b in rows))
    return tx.record_opening(db, form).transaction_id


def test_update_opening_excludes_itself(seeded_db):
    txn_id = opening(seeded_db, ("1101", 2000))
    form = OpeningForm(TODAY, (OpeningLine(acc(seeded_db, "1101"), 1800),))
    tx.update_transaction(seeded_db, txn_id, form)
    assert balance(seeded_db, "1101") == 1800


def test_restore_opening_rechecks_d12(seeded_db):
    first = opening(seeded_db, ("1101", 2000))
    tx.delete_transaction(seeded_db, first)
    opening(seeded_db, ("1101", 1800))  # 刪除期間重新設定期初
    with pytest.raises(ValidationFailed) as exc:
        tx.restore_transaction(seeded_db, first)
    assert [i.code for i in exc.value.result.errors] == ["OPENING_EXISTS"]


def test_restore_opening_with_inactive_account(seeded_db):
    txn_id = opening(seeded_db, ("1201", 5000))
    tx.delete_transaction(seeded_db, txn_id)
    account_service.deactivate_account(seeded_db, acc(seeded_db, "1201"))
    tx.restore_transaction(seeded_db, txn_id)  # 還原不重跑 V04（D14、U14）
    assert balance(seeded_db, "1201") == 5000


def test_restore_checks_invoice_in_use(seeded_db):
    first = expense(seeded_db, 120, invoice_no="AB12345678")
    tx.delete_transaction(seeded_db, first)
    expense(seeded_db, 85, invoice_no="AB12345678")  # 刪除後同號碼可以再用
    with pytest.raises(ValidationFailed) as exc:
        tx.restore_transaction(seeded_db, first)
    assert errors_of(exc) == [("V08", "invoice_no")]


# --- 其他交易類型的修改 ---------------------------------------------------------------------


def test_update_payroll_stays_payroll(seeded_db):
    form = ManualForm(
        TODAY, manual_lines(seeded_db, ("1102", 27_000, None), ("4101", -27_000, None))
    )
    txn_id = tx.record_payroll(seeded_db, form).transaction_id
    new = ManualForm(
        TODAY, manual_lines(seeded_db, ("1102", 27_500, None), ("4101", -27_500, None))
    )
    tx.update_transaction(seeded_db, txn_id, new)
    txn = seeded_db.get(Transaction, txn_id)
    assert txn.txn_type == "PAYROLL"
    assert balance(seeded_db, "1102") == 27_500


def test_update_adjustment(seeded_db):
    form = AdjustmentForm(TODAY, acc(seeded_db, "1101"), -200, "盤點短少")
    txn_id = tx.record_adjustment(seeded_db, form).transaction_id
    tx.update_transaction(
        seeded_db, txn_id, AdjustmentForm(TODAY, acc(seeded_db, "1101"), -150, "盤點短少")
    )
    assert q.adjustment_total(seeded_db, TODAY, TODAY) == -150
    with pytest.raises(ValidationFailed) as exc:
        tx.update_transaction(
            seeded_db, txn_id, AdjustmentForm(TODAY, acc(seeded_db, "1101"), -150, "")
        )
    assert errors_of(exc) == [("V15", "description")]


def test_deleted_rows_still_visible_to_integrity(seeded_db):
    txn_id = expense(seeded_db, 120)
    tx.delete_transaction(seeded_db, txn_id)
    assert integrity.check_all(seeded_db) == []
    assert seeded_db.scalar(select(Posting.id).where(Posting.transaction_id == txn_id))
