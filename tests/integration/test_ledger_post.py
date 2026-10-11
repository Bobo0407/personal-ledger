"""LedgerService.post() 與共用驗證規則（P1 Step 3；T11、T12）。"""

import json
from dataclasses import replace
from datetime import date, timedelta

import pytest
from sqlalchemy import func, select

from app.models import AuditLog, Merchant, Posting, Setting, Transaction
from app.services import accounts as account_service
from app.services import audit, ledger
from app.services import payment_methods as pm_service
from app.services.drafts import EntryDraft, PostingDraft
from app.services.entries import ManualEntry
from app.services.validation import (
    ConfirmationRequired,
    InvalidSetting,
    ValidationFailed,
    validate,
)
from tests.ledger_helpers import TODAY, acc, freeze_today, manual_form, post_manual


@pytest.fixture(autouse=True)
def _today(monkeypatch):
    freeze_today(monkeypatch)


def counts(db) -> tuple[int, int, int]:
    return tuple(
        db.scalar(select(func.count()).select_from(m)) for m in (Transaction, Posting, AuditLog)
    )


def draft_of(db, *lines, txn_date=TODAY, **kwargs) -> EntryDraft:
    return ManualEntry.build(manual_form(db, txn_date, *lines, **kwargs))


def error_codes(db, draft) -> list[str]:
    return [i.code for i in validate(db, draft).errors]


def lunch(db, amount=120) -> EntryDraft:
    return draft_of(db, ("5101", amount), ("1102", -amount))


# --- 成功寫入 ---------------------------------------------------------------------


def test_post_writes_transaction_and_postings(seeded_db):
    result = ledger.post(seeded_db, lunch(seeded_db), user_id=None)

    txn = seeded_db.get(Transaction, result.transaction_id)
    assert txn.txn_date == "2026-10-15"
    assert (txn.txn_type, txn.status, txn.source) == ("MANUAL", "POSTED", "MANUAL")
    assert [(p.line_no, p.account_id, p.amount) for p in txn.postings] == [
        (1, acc(seeded_db, "5101"), 120),
        (2, acc(seeded_db, "1102"), -120),
    ]


def test_post_writes_create_audit_log_in_same_transaction(seeded_db):
    result = ledger.post(seeded_db, lunch(seeded_db))
    log = seeded_db.scalar(select(AuditLog))

    assert (log.entity, log.entity_id, log.action) == (
        "transaction",
        result.transaction_id,
        "CREATE",
    )
    assert log.before_json is None
    after = json.loads(log.after_json)
    assert after["txn_date"] == "2026-10-15"
    assert [(p["line_no"], p["account_code"], p["amount"]) for p in after["postings"]] == [
        (1, "5101", 120),
        (2, "1102", -120),
    ]


def test_same_account_can_appear_twice(seeded_db):
    # Q10：同一筆交易可以有多筆同科目的分錄
    draft = draft_of(seeded_db, ("5601", 350), ("5601", 350), ("1102", -700))
    txn_id = ledger.post(seeded_db, draft).transaction_id
    assert len(seeded_db.get(Transaction, txn_id).postings) == 3


def test_original_currency_is_stored(seeded_db):
    base = draft_of(seeded_db, ("5301", 1000), ("5502", 15), ("2101", -1015))
    main = replace(base.postings[0], original_currency="JPY", original_amount_minor=4800)
    draft = replace(base, postings=(main, *base.postings[1:]))
    txn = seeded_db.get(Transaction, ledger.post(seeded_db, draft).transaction_id)
    assert (txn.postings[0].original_currency, txn.postings[0].original_amount_minor) == (
        "JPY",
        4800,
    )


# --- 擋下：完全不寫入 ---------------------------------------------------------------


def test_t11_unbalanced_reports_difference(seeded_db):
    draft = draft_of(seeded_db, ("5101", 100), ("1102", -90))
    with pytest.raises(ValidationFailed) as exc_info:
        ledger.post(seeded_db, draft)
    v02 = [i for i in exc_info.value.result.errors if i.code == "V02"]
    assert v02 and "差額 10 元" in v02[0].message
    assert counts(seeded_db) == (0, 0, 0)


def test_t12_non_leaf_account_rejected(seeded_db):
    draft = draft_of(seeded_db, ("5100", 120), ("1102", -120))
    with pytest.raises(ValidationFailed) as exc_info:
        ledger.post(seeded_db, draft)
    issue = exc_info.value.result.errors[0]
    assert (issue.code, issue.line) == ("V03", 1)
    assert "請選擇「餐飲」底下的子科目" in issue.message
    assert counts(seeded_db) == (0, 0, 0)


def test_needs_at_least_two_postings(seeded_db):
    assert "V02" in error_codes(seeded_db, draft_of(seeded_db, ("5101", 100)))


def test_zero_lines_skipped_then_too_few(seeded_db):
    assert "V02" in error_codes(seeded_db, draft_of(seeded_db, ("5101", 0), ("1102", 0)))


@pytest.mark.parametrize("amount", [0, True, 12.5, "100", None])
def test_posting_amount_invariant(seeded_db, amount):
    # 分錄金額的不變條件（不是規格 V01）：整數、非 bool、不為 0
    draft = EntryDraft(
        TODAY,
        "MANUAL",
        (PostingDraft(acc(seeded_db, "5101"), amount), PostingDraft(acc(seeded_db, "1102"), -100)),
    )
    issues = validate(seeded_db, draft).errors
    assert ("POSTING_AMOUNT", 1) in [(i.code, i.line) for i in issues]


def test_missing_date(seeded_db):
    assert "V05" in error_codes(seeded_db, lunch_with_date(seeded_db, None))


def lunch_with_date(db, txn_date):
    return replace(lunch(db), txn_date=txn_date)


def test_missing_account_points_to_line(seeded_db):
    draft = draft_of(seeded_db, ("5101", 100), (None, -100))
    issues = validate(seeded_db, draft).errors
    assert [(i.code, i.line) for i in issues] == [("V05", 2)]


def test_unknown_account(seeded_db):
    draft = EntryDraft(
        TODAY, "MANUAL", (PostingDraft(999_999, 100), PostingDraft(acc(seeded_db, "1102"), -100))
    )
    assert [(i.code, i.line) for i in validate(seeded_db, draft).errors] == [("V05", 1)]


def test_inactive_account_rejected(seeded_db):
    account_service.deactivate_account(seeded_db, acc(seeded_db, "5303"))
    assert "V04" in error_codes(seeded_db, draft_of(seeded_db, ("5303", 100), ("1101", -100)))


def test_inactive_payment_method_rejected(seeded_db):
    pm = next(p for p in pm_service.list_payment_methods(seeded_db) if p.name == "台灣Pay")
    pm_service.deactivate_payment_method(seeded_db, pm.id)
    draft = replace(lunch(seeded_db), payment_method_id=pm.id)
    assert "V04" in error_codes(seeded_db, draft)


def test_inactive_merchant_rejected(seeded_db):
    merchant = Merchant(name="7-11", is_active=False)
    seeded_db.add(merchant)
    seeded_db.commit()
    draft = replace(lunch(seeded_db), merchant_id=merchant.id)
    assert "V04" in error_codes(seeded_db, draft)


@pytest.mark.parametrize(
    ("currency", "minor", "fragment"),
    [
        ("JPY", None, "原幣金額"),
        (None, 4800, "原幣別"),
        ("EUR", 100, "幣別"),
        ("JPY", 0, "原幣金額"),
    ],
)
def test_v09_original_currency(seeded_db, currency, minor, fragment):
    base = lunch(seeded_db)
    first = replace(base.postings[0], original_currency=currency, original_amount_minor=minor)
    issues = validate(seeded_db, replace(base, postings=(first, base.postings[1]))).errors
    assert [i.code for i in issues] == ["V09"]
    assert fragment in issues[0].message


@pytest.mark.parametrize(
    ("code", "txn_type"),
    [("3101", "MANUAL"), ("3102", "MANUAL"), ("3101", "ADJUSTMENT"), ("3102", "OPENING")],
)
def test_system_accounts_only_in_their_txn_type(seeded_db, code, txn_type):
    draft = replace(draft_of(seeded_db, ("1101", 100), (code, -100)), txn_type=txn_type)
    assert "SYSTEM_ACCOUNT" in error_codes(seeded_db, draft)


@pytest.mark.parametrize(("code", "txn_type"), [("3101", "OPENING"), ("3102", "ADJUSTMENT")])
def test_system_accounts_allowed_in_their_txn_type(seeded_db, code, txn_type):
    draft = replace(draft_of(seeded_db, ("1101", 100), (code, -100)), txn_type=txn_type)
    assert "SYSTEM_ACCOUNT" not in error_codes(seeded_db, draft)


def test_unknown_txn_type_and_line_role(seeded_db):
    base = lunch(seeded_db)
    first = replace(base.postings[0], line_role="DISCOUNT")
    draft = replace(base, txn_type="GIFT", postings=(first, base.postings[1]))
    assert set(error_codes(seeded_db, draft)) == {"TXN_TYPE", "LINE_ROLE"}


def test_all_errors_reported_together(seeded_db):
    draft = draft_of(seeded_db, ("5100", 100), (None, -90), txn_date=None)
    assert {"V05", "V03"} <= set(error_codes(seeded_db, draft))


# --- 確認：V10、V11 -----------------------------------------------------------------


def test_v10_requires_confirmation_then_posts(seeded_db):
    draft = lunch(seeded_db, 12_000)
    with pytest.raises(ConfirmationRequired) as exc_info:
        ledger.post(seeded_db, draft)
    assert [i.code for i in exc_info.value.pending] == ["V10"]
    assert "12,000" in exc_info.value.pending[0].message
    assert counts(seeded_db) == (0, 0, 0)

    assert ledger.post(seeded_db, draft, confirmed={"V10"}).transaction_id


def test_v10_threshold_is_strictly_greater(seeded_db):
    assert validate(seeded_db, lunch(seeded_db, 10_000)).confirmations == ()
    assert [i.code for i in validate(seeded_db, lunch(seeded_db, 10_001)).confirmations] == ["V10"]


def test_v10_magnitude_is_sum_of_positive_postings(seeded_db):
    # 5,000 + 15 = 5,015；門檻改成 5,010 時要確認
    seeded_db.get(Setting, "large_amount_threshold").value = "5010"
    seeded_db.commit()
    draft = draft_of(seeded_db, ("1103", 5000), ("5501", 15), ("1102", -5015))
    assert [i.code for i in validate(seeded_db, draft).confirmations] == ["V10"]


def test_v10_missing_setting_falls_back_to_spec_default(seeded_db):
    seeded_db.delete(seeded_db.get(Setting, "large_amount_threshold"))
    seeded_db.commit()
    assert validate(seeded_db, lunch(seeded_db, 10_000)).confirmations == ()
    assert [i.code for i in validate(seeded_db, lunch(seeded_db, 10_001)).confirmations] == ["V10"]


@pytest.mark.parametrize("value", ["abc", "0", "-5", "1.5", "", " 10000", "１００"])
def test_v10_invalid_setting_fails_loudly(seeded_db, value):
    # 已存在但不合法的設定是資料異常，不能靜默當成 10,000
    seeded_db.get(Setting, "large_amount_threshold").value = value
    seeded_db.commit()
    with pytest.raises(InvalidSetting):
        ledger.post(seeded_db, lunch(seeded_db))
    assert counts(seeded_db) == (0, 0, 0)


def test_v11_future_date_requires_confirmation(seeded_db):
    draft = replace(lunch(seeded_db), txn_date=TODAY + timedelta(days=3))
    issues = validate(seeded_db, draft).confirmations
    assert [i.code for i in issues] == ["V11"]
    assert "3 天後" in issues[0].message
    assert validate(seeded_db, lunch(seeded_db)).confirmations == ()


def test_partial_confirmation_still_raises(seeded_db):
    draft = replace(lunch(seeded_db, 20_000), txn_date=TODAY + timedelta(days=1))
    with pytest.raises(ConfirmationRequired) as exc_info:
        ledger.post(seeded_db, draft, confirmed={"V10"})
    assert [i.code for i in exc_info.value.pending] == ["V11"]
    assert counts(seeded_db) == (0, 0, 0)
    assert ledger.post(seeded_db, draft, confirmed={"V10", "V11"}).transaction_id


def test_no_confirmations_when_there_are_errors(seeded_db):
    draft = draft_of(seeded_db, ("5100", 20_000), ("1102", -20_000))
    result = validate(seeded_db, draft)
    assert result.errors and result.confirmations == () and result.notices == ()


# --- 提醒：V13（截至今天） -------------------------------------------------------------


def test_v13_cash_goes_negative(seeded_db):
    post_manual(seeded_db, TODAY, ("1101", 100), ("4201", -100))
    result = ledger.post(seeded_db, draft_of(seeded_db, ("5101", 300), ("1101", -300)))
    assert [i.code for i in result.notices] == ["V13"]
    assert "−200" in result.notices[0].message


def test_v13_bank_goes_negative(seeded_db):
    result = ledger.post(seeded_db, lunch(seeded_db))
    assert [i.code for i in result.notices] == ["V13"]


def test_v13_not_for_credit_card(seeded_db):
    result = ledger.post(seeded_db, draft_of(seeded_db, ("5201", 800), ("2101", -800)))
    assert result.notices == ()


def test_v13_not_when_balance_increases(seeded_db):
    ledger.post(seeded_db, lunch(seeded_db))  # 郵局 −120
    result = ledger.post(seeded_db, draft_of(seeded_db, ("1102", 50), ("4201", -50)))
    assert result.notices == ()


def test_v13_not_when_balance_stays_non_negative(seeded_db):
    post_manual(seeded_db, TODAY, ("1101", 500), ("4201", -500))
    result = ledger.post(seeded_db, draft_of(seeded_db, ("5101", 500), ("1101", -500)))
    assert result.notices == ()


@pytest.mark.parametrize(
    ("lines", "expect_notice"),
    [
        # 現金原有 100；同一筆裡現金 −300、+250 → 淨 −50 → 剩 50，不提醒（逐筆判斷會誤報）
        ((("5101", 50), ("1101", -300), ("1101", 250)), False),
        # 現金 −300、+150 → 淨 −150 → 剩 −50，提醒
        ((("5101", 150), ("1101", -300), ("1101", 150)), True),
        # 現金 −500、+600 → 淨 +100 → 餘額增加，不提醒（其中一筆單獨看會變負）
        ((("1101", -500), ("1101", 600), ("4201", -100)), False),
    ],
)
def test_v13_uses_net_delta_per_account(seeded_db, lines, expect_notice):
    # Q10：同一科目可以出現多筆分錄；V13 依該科目在本筆交易的淨變動判斷
    post_manual(seeded_db, TODAY, ("1101", 100), ("4201", -100))
    result = ledger.post(seeded_db, draft_of(seeded_db, *lines))
    assert [i.code for i in result.notices] == (["V13"] if expect_notice else [])
    if expect_notice:
        assert "−50" in result.notices[0].message


def test_v13_future_income_does_not_hide_negative_today(seeded_db):
    # 修正一：未來的收入不能掩蓋今天已經是負數的帳戶
    post_manual(
        seeded_db, TODAY + timedelta(days=5), ("1101", 10_000), ("4201", -10_000), confirmed={"V11"}
    )
    result = ledger.post(seeded_db, draft_of(seeded_db, ("5101", 300), ("1101", -300)))
    assert [i.code for i in result.notices] == ["V13"]


def test_v13_future_dated_spending_does_not_trigger(seeded_db):
    draft = replace(lunch(seeded_db), txn_date=TODAY + timedelta(days=2))
    result = ledger.post(seeded_db, draft, confirmed={"V11"})
    assert result.notices == ()


def test_v13_backdated_spending_counts(seeded_db):
    result = ledger.post(seeded_db, replace(lunch(seeded_db), txn_date=date(2026, 10, 1)))
    assert [i.code for i in result.notices] == ["V13"]


# --- 交易邊界 -----------------------------------------------------------------------


def test_audit_failure_rolls_back_everything(seeded_db, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("audit down")

    monkeypatch.setattr(audit, "record", boom)
    with pytest.raises(RuntimeError, match="audit down"):
        ledger.post(seeded_db, lunch(seeded_db))
    assert counts(seeded_db) == (0, 0, 0)


def test_database_error_rolls_back_everything(seeded_db):
    # 驗證不檢查發票格式（V08 在 P2），資料庫 CHECK 會在寫入時擋下 → 整批回滾
    draft = replace(lunch(seeded_db), invoice_no="bad")
    with pytest.raises(Exception):
        ledger.post(seeded_db, draft)
    assert counts(seeded_db) == (0, 0, 0)
    assert not (seeded_db.new or seeded_db.dirty)


@pytest.mark.parametrize("change", ["new", "dirty", "deleted"])
def test_refuses_session_with_pending_changes(seeded_db, change):
    if change == "new":
        seeded_db.add(Setting(key="x", value="1"))
    elif change == "dirty":
        seeded_db.get(Setting, "budget_alert_pct").value = "90"
    else:
        seeded_db.delete(seeded_db.get(Setting, "budget_alert_pct"))
    with pytest.raises(ledger.SessionNotClean):
        ledger.post(seeded_db, lunch(seeded_db))
    seeded_db.rollback()
    assert counts(seeded_db) == (0, 0, 0)
