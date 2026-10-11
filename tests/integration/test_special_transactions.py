"""特殊交易（Step 5）：T13 期初、T33 帳務調整、T10 薪資，與 D12、V15、R1–R11。"""

from datetime import date, timedelta

import pytest
from sqlalchemy import func, select

from app.models import Account, Posting, Transaction
from app.services import ledger
from app.services import ledger_queries as q
from app.services import transactions as tx
from app.services.drafts import EntryDraft, PostingDraft
from app.services.entries import (
    AdjustmentForm,
    ManualForm,
    ManualLine,
    OpeningForm,
    OpeningLine,
)
from app.services.errors import MissingSystemAccount
from app.services.validation import ConfirmationRequired, ValidationFailed, validate
from tests.ledger_helpers import TODAY, acc, freeze_today, post_manual

OCTOBER = (date(2026, 10, 1), date(2026, 10, 31))
OPEN_DATE = date(2026, 10, 1)


@pytest.fixture(autouse=True)
def _today(monkeypatch):
    freeze_today(monkeypatch)


def lines(db, txn_id):
    return [(p.account.code, p.amount, p.line_role) for p in db.get(Transaction, txn_id).postings]


def errors(exc_info):
    return [(i.code, i.field) for i in exc_info.value.result.errors]


def counts(db):
    return (
        db.scalar(select(func.count()).select_from(Transaction)),
        db.scalar(select(func.count()).select_from(Posting)),
    )


def opening(db, *pairs, txn_date=OPEN_DATE):
    form = OpeningForm(txn_date, tuple(OpeningLine(acc(db, c), b) for c, b in pairs))
    return tx.record_opening(db, form)


def adjust(db, code, delta, note="盤點短少", txn_date=TODAY, confirmed=()):
    form = AdjustmentForm(txn_date, acc(db, code), delta, note)
    return tx.record_adjustment(db, form, confirmed=confirmed)


def payroll(db, *pairs, confirmed=()):
    form = ManualForm(TODAY, tuple(ManualLine(acc(db, c), a) for c, a in pairs), "10 月薪資")
    return tx.record_payroll(db, form, confirmed=confirmed)


# --- T13 期初餘額 ---------------------------------------------------------------------


def test_t13_opening_balances(seeded_db):
    result = opening(seeded_db, ("1101", 2000), ("1102", 50_000), ("2101", 3000))
    txn = seeded_db.get(Transaction, result.transaction_id)

    assert txn.txn_type == "OPENING"
    assert lines(seeded_db, txn.id) == [
        ("1101", 2000, None),
        ("1102", 50_000, None),
        ("2101", -3000, None),
        ("3101", -49_000, None),  # 對方科目為期初權益
    ]
    assert q.net_worth(seeded_db) == 49_000
    assert q.balance(seeded_db, acc(seeded_db, "2101")) == 3000  # 欠款以正數顯示
    assert q.balance(seeded_db, acc(seeded_db, "3101")) == 49_000


def test_opening_is_not_income_or_expense(seeded_db):
    opening(seeded_db, ("1101", 2000), ("2101", 3000))
    assert q.class_total(seeded_db, "INCOME", *OCTOBER) == 0
    assert q.class_total(seeded_db, "EXPENSE", *OCTOBER) == 0


def test_opening_is_exempt_from_v10(seeded_db):
    # R4：期初本來就常超過 10,000，不跳「多打一個 0」確認
    assert opening(seeded_db, ("1102", 500_000)).transaction_id


def test_opening_negative_balance_and_zero_skipped(seeded_db):
    result = opening(seeded_db, ("1101", 0), ("1102", -500))  # 透支 500
    assert lines(seeded_db, result.transaction_id) == [("1102", -500, None), ("3101", 500, None)]
    assert [i.code for i in result.notices] == ["V13"]  # 郵局截至今天為負


def test_opening_without_equity_when_balanced(seeded_db):
    result = opening(seeded_db, ("1101", 3000), ("2101", 3000))
    assert lines(seeded_db, result.transaction_id) == [("1101", 3000, None), ("2101", -3000, None)]
    assert q.net_worth(seeded_db) == 0


def test_opening_single_line_after_zero_skip_is_rejected(seeded_db):
    # R2：0 元略過後只剩不到 2 筆分錄 → V02，不寫入單行期初
    form = OpeningForm(OPEN_DATE, (OpeningLine(acc(seeded_db, "1101"), 0),))
    with pytest.raises(ValidationFailed) as exc_info:
        tx.record_opening(seeded_db, form)
    assert "V02" in [c for c, _ in errors(exc_info)]
    assert counts(seeded_db) == (0, 0)


@pytest.mark.parametrize("code", ["5101", "4101"])
def test_opening_only_asset_or_liability(seeded_db, code):
    with pytest.raises(ValidationFailed) as exc_info:
        opening(seeded_db, (code, 100))
    assert [c for c, _ in errors(exc_info)] == ["TXN_STRUCTURE"]


def test_opening_leaf_only(seeded_db):
    from app.services import accounts as account_service

    group = account_service.create_account(
        seeded_db, code="1400", name="應收款項", account_class="ASSET", subtype="RECEIVABLE"
    )
    account_service.create_account(
        seeded_db, code="1401", name="應收-小明", parent_id=group.id, subtype="RECEIVABLE"
    )
    with pytest.raises(ValidationFailed) as exc_info:
        opening(seeded_db, ("1400", 100))
    assert [c for c, _ in errors(exc_info)] == ["V03"]


def test_opening_future_date_requires_v11(seeded_db):
    with pytest.raises(ConfirmationRequired) as exc_info:
        opening(seeded_db, ("1101", 100), txn_date=TODAY + timedelta(days=1))
    assert [i.code for i in exc_info.value.pending] == ["V11"]


# --- D12 OPENING_EXISTS -----------------------------------------------------------------


def test_d12_duplicate_account_in_same_opening(seeded_db):
    with pytest.raises(ValidationFailed) as exc_info:
        opening(seeded_db, ("1101", 100), ("1101", 200))
    assert [c for c, _ in errors(exc_info)] == ["OPENING_EXISTS"]


def test_d12_account_already_has_opening(seeded_db):
    opening(seeded_db, ("1101", 2000))
    with pytest.raises(ValidationFailed) as exc_info:
        opening(seeded_db, ("1102", 100), ("1101", 50))
    assert [(i.code, i.line) for i in exc_info.value.result.errors] == [("OPENING_EXISTS", 2)]
    assert opening(seeded_db, ("1102", 100)).transaction_id  # 其他帳戶仍可補期初


def test_d12_deleted_opening_does_not_count(seeded_db):
    txn_id = opening(seeded_db, ("1101", 2000)).transaction_id
    seeded_db.get(Transaction, txn_id).deleted_at = "2026-10-15T12:00:00+08:00"  # 測試：模擬軟刪除
    seeded_db.commit()
    assert opening(seeded_db, ("1101", 1800)).transaction_id
    assert q.balance(seeded_db, acc(seeded_db, "1101")) == 1800


def test_d12_other_transactions_on_account_do_not_block_opening(seeded_db):
    post_manual(seeded_db, TODAY, ("5101", 100), ("1101", -100))
    assert opening(seeded_db, ("1101", 2000)).transaction_id  # R11：不限制日期先後


# --- T33 帳務調整 ---------------------------------------------------------------------


def test_t33_cash_count_shortage(seeded_db):
    opening(seeded_db, ("1101", 2000))
    result = adjust(seeded_db, "1101", -200)
    txn = seeded_db.get(Transaction, result.transaction_id)

    assert txn.txn_type == "ADJUSTMENT"
    assert txn.description == "盤點短少"
    assert lines(seeded_db, txn.id) == [("1101", -200, None), ("3102", 200, None)]
    assert q.balance(seeded_db, acc(seeded_db, "1101")) == 1800
    assert q.class_total(seeded_db, "EXPENSE", *OCTOBER) == 0  # 不計入支出
    assert q.class_total(seeded_db, "INCOME", *OCTOBER) == 0
    assert q.adjustment_total(seeded_db, *OCTOBER) == -200  # 月報帳務調整合計
    # 預算不變的部分在 P3 有預算功能後再驗


def test_t33_missing_note_is_v15(seeded_db):
    with pytest.raises(ValidationFailed) as exc_info:
        adjust(seeded_db, "1101", -200, note="   ")
    assert errors(exc_info) == [("V15", "description")]
    assert counts(seeded_db) == (0, 0)


def test_adjustment_raises_liability(seeded_db):
    result = adjust(seeded_db, "2101", 100, note="帳單比帳上多 100")
    assert lines(seeded_db, result.transaction_id) == [("2101", -100, None), ("3102", 100, None)]
    assert q.balance(seeded_db, acc(seeded_db, "2101")) == 100
    assert q.adjustment_total(seeded_db, *OCTOBER) == -100  # 欠款增加 → 淨資產調低


def test_adjustment_total_sign_and_net_worth_identity(seeded_db):
    adjust(seeded_db, "1101", 300, note="找到零錢")
    post_manual(seeded_db, TODAY, ("1102", 1000), ("4201", -1000))  # 本月結餘 +1,000
    assert q.adjustment_total(seeded_db, *OCTOBER) == 300  # 正數 = 帳戶餘額被調高
    surplus = q.class_total(seeded_db, "INCOME", *OCTOBER) - q.class_total(
        seeded_db, "EXPENSE", *OCTOBER
    )
    assert q.net_worth(seeded_db) == surplus + q.adjustment_total(seeded_db, *OCTOBER)


@pytest.mark.parametrize("code", ["5101", "4101"])
def test_adjustment_only_asset_or_liability(seeded_db, code):
    with pytest.raises(ValidationFailed) as exc_info:
        adjust(seeded_db, code, 100)
    assert [c for c, _ in errors(exc_info)] == ["V15"]


def test_adjustment_v13_when_cash_goes_negative(seeded_db):
    result = adjust(seeded_db, "1101", -50, note="短少")
    assert [i.code for i in result.notices] == ["V13"]


def test_adjustment_keeps_v10(seeded_db):
    with pytest.raises(ConfirmationRequired) as exc_info:
        adjust(seeded_db, "1101", 20_000)
    assert [i.code for i in exc_info.value.pending] == ["V10"]
    assert adjust(seeded_db, "1101", 20_000, confirmed={"V10"}).transaction_id


def test_v15_handwritten_drafts(seeded_db):
    adj = acc(seeded_db, "3102")
    three_lines = EntryDraft(
        TODAY,
        "ADJUSTMENT",
        (
            PostingDraft(acc(seeded_db, "1101"), -100),
            PostingDraft(acc(seeded_db, "1102"), -100),
            PostingDraft(adj, 200),
        ),
        description="x",
    )
    no_system = EntryDraft(
        TODAY,
        "ADJUSTMENT",
        (PostingDraft(acc(seeded_db, "1101"), -100), PostingDraft(acc(seeded_db, "1102"), 100)),
        description="x",
    )
    for draft in (three_lines, no_system):
        assert "V15" in [i.code for i in validate(seeded_db, draft).errors]


def test_adjustment_counterpart_is_always_system_account(seeded_db):
    # 使用者沒有地方選對方科目；系統科目也不能被其他交易類型使用（Q8）
    result = adjust(seeded_db, "1102", 10, note="利息差")
    assert lines(seeded_db, result.transaction_id)[1][0] == "3102"
    draft = EntryDraft(
        TODAY,
        "MANUAL",
        (PostingDraft(acc(seeded_db, "1101"), 10), PostingDraft(acc(seeded_db, "3102"), -10)),
    )
    assert "SYSTEM_ACCOUNT" in [i.code for i in validate(seeded_db, draft).errors]


# --- T10 薪資 ------------------------------------------------------------------------


T10_LINES = (
    ("1102", 27_050),  # 實領入郵局
    ("5601", 700),  # 勞保自付
    ("5602", 450),  # 健保自付
    ("1301", 0),  # 所得稅扣繳 0 → 不產生分錄
    ("1201", 1800),  # 勞退自提（資產）
    ("4101", -30_000),  # 本薪
)


def test_t10_payroll(seeded_db):
    result = payroll(seeded_db, *T10_LINES)
    txn = seeded_db.get(Transaction, result.transaction_id)

    assert txn.txn_type == "PAYROLL"  # 不是 MANUAL
    assert "1301" not in [code for code, _, _ in lines(seeded_db, txn.id)]
    assert len(txn.postings) == 5
    assert q.balance(seeded_db, acc(seeded_db, "1102")) == 27_050
    assert q.balance(seeded_db, acc(seeded_db, "1201")) == 1800  # 資產，不是費用
    assert q.net_worth(seeded_db) == 28_850
    assert q.class_total(seeded_db, "INCOME", *OCTOBER) == 30_000
    assert q.class_total(seeded_db, "EXPENSE", *OCTOBER) == 1150


def test_payroll_is_exempt_from_v10(seeded_db):
    # R4：每月薪資一定超過 10,000，不跳確認（T10 也是在沒有 confirmed 的情況下寫入）
    assert validate(seeded_db, _payroll_draft(seeded_db)).confirmations == ()


def _payroll_draft(db):
    from app.services.entries import PayrollEntry

    form = ManualForm(TODAY, tuple(ManualLine(acc(db, c), a) for c, a in T10_LINES))
    return PayrollEntry.build(form)


def test_payroll_line_roles_are_optional(seeded_db):
    form = ManualForm(
        TODAY,
        (
            ManualLine(acc(seeded_db, "1102"), 29_300),
            ManualLine(acc(seeded_db, "5601"), 700, line_role="INSURANCE"),
            ManualLine(acc(seeded_db, "4101"), -30_000, line_role="MAIN"),
        ),
    )
    txn_id = tx.record_payroll(seeded_db, form).transaction_id
    assert [p.line_role for p in seeded_db.get(Transaction, txn_id).postings] == [
        None,
        "INSURANCE",
        "MAIN",
    ]


@pytest.mark.parametrize(
    "pairs",
    [
        (("1102", 100), ("1103", -100)),  # 沒有收入分錄
        (("4101", 100), ("1102", -100)),  # 收入記在借方
    ],
)
def test_payroll_structure(seeded_db, pairs):
    with pytest.raises(ValidationFailed) as exc_info:
        payroll(seeded_db, *pairs)
    assert [c for c, _ in errors(exc_info)] == ["TXN_STRUCTURE"]


def test_payroll_rejects_equity(seeded_db):
    from app.services import accounts as account_service

    account_service.create_account(seeded_db, code="3201", name="其他權益", account_class="EQUITY")
    with pytest.raises(ValidationFailed) as exc_info:
        payroll(seeded_db, ("1102", 100), ("3201", 50), ("4101", -150))
    assert [c for c, _ in errors(exc_info)] == ["TXN_STRUCTURE"]


def test_manual_is_not_bound_by_payroll_rules(seeded_db):
    assert post_manual(seeded_db, TODAY, ("1102", 100), ("1103", -100)).transaction_id


# --- 系統科目 ------------------------------------------------------------------------


@pytest.mark.parametrize("code", ["3101", "3102"])
def test_missing_system_account_is_system_error(seeded_db, code):
    account = seeded_db.scalar(select(Account).where(Account.code == code))
    account.system_key = None  # 測試：模擬主資料缺少系統科目
    seeded_db.commit()
    with pytest.raises(MissingSystemAccount):
        if code == "3101":
            opening(seeded_db, ("1101", 100))
        else:
            adjust(seeded_db, "1101", -10)


def test_adjustment_total_without_system_account(seeded_db):
    seeded_db.scalar(select(Account).where(Account.code == "3102")).system_key = None
    seeded_db.commit()
    with pytest.raises(MissingSystemAccount):
        q.adjustment_total(seeded_db, *OCTOBER)


def test_all_special_writes_go_through_ledger(seeded_db, monkeypatch):
    calls = []
    original = ledger.post

    def spy(db, draft, **kwargs):
        calls.append(draft.txn_type)
        return original(db, draft, **kwargs)

    monkeypatch.setattr(ledger, "post", spy)
    opening(seeded_db, ("1101", 100))
    adjust(seeded_db, "1101", -10)
    payroll(seeded_db, ("1102", 100), ("4101", -100))
    assert calls == ["OPENING", "ADJUSTMENT", "PAYROLL"]
