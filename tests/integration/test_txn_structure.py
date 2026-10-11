"""TXN_STRUCTURE：繞過 builder 手寫草稿時的第二道防線（Step 4 決策 P4、P5、P6、P7、P12、D9）。"""

import pytest

from app.services import accounts as account_service
from app.services import ledger
from app.services import payment_methods as pm_service
from app.services.drafts import EntryDraft, PostingDraft
from app.services.validation import ValidationFailed, validate
from tests.ledger_helpers import TODAY, acc, freeze_today


@pytest.fixture(autouse=True)
def _today(monkeypatch):
    freeze_today(monkeypatch)


def pm(db, name: str) -> int:
    return next(p.id for p in pm_service.list_payment_methods(db, False) if p.name == name)


def draft(db, txn_type, *lines, payment_method=None) -> EntryDraft:
    """lines：(科目代碼, 金額, line_role)。"""
    return EntryDraft(
        TODAY,
        txn_type,
        tuple(PostingDraft(acc(db, code), amount, role) for code, amount, role in lines),
        payment_method_id=pm(db, payment_method) if payment_method else None,
    )


def structure_errors(db, d: EntryDraft) -> list[str]:
    return [i.message for i in validate(db, d).errors if i.code == "TXN_STRUCTURE"]


VALID = {
    "EXPENSE": (("5101", 120, "MAIN"), ("1102", -120, None)),
    "INCOME": (("1103", 120, None), ("4201", -120, "MAIN")),
    "TRANSFER": (("1103", 100, None), ("1102", -100, None)),
    "CARD_PAYMENT": (("2101", 100, None), ("1103", -100, None)),
}


@pytest.mark.parametrize("txn_type", list(VALID))
def test_valid_structures_pass(seeded_db, txn_type):
    method = "LINE Pay" if txn_type == "EXPENSE" else None
    assert (
        structure_errors(
            seeded_db, draft(seeded_db, txn_type, *VALID[txn_type], payment_method=method)
        )
        == []
    )


@pytest.mark.parametrize(
    ("lines", "method"),
    [
        # 兩筆 MAIN
        ((("5101", 60, "MAIN"), ("5102", 60, "MAIN"), ("1102", -120, None)), "LINE Pay"),
        # 沒有 MAIN
        ((("5101", 120, None), ("1102", -120, None)), "LINE Pay"),
        # MAIN 記在資產
        ((("1101", 120, "MAIN"), ("1102", -120, None)), "LINE Pay"),
        # 付款帳戶與付款方式不一致（LINE Pay → 郵局，但扣了現金）
        ((("5101", 120, "MAIN"), ("1101", -120, None)), "LINE Pay"),
        # 沒有付款方式
        ((("5101", 120, "MAIN"), ("1102", -120, None)), None),
        # 不允許的 line_role
        ((("5101", 100, "MAIN"), ("5601", 20, "TAX"), ("1102", -120, None)), "LINE Pay"),
        # 分期利息但不是刷信用卡（P12）
        ((("5201", 100, "MAIN"), ("5503", 20, "INTEREST"), ("1101", -120, None)), "現金"),
        # 兩筆付款分錄
        ((("5101", 120, "MAIN"), ("1102", -60, None), ("1102", -60, None)), "LINE Pay"),
    ],
)
def test_expense_structure_violations(seeded_db, lines, method):
    assert structure_errors(seeded_db, draft(seeded_db, "EXPENSE", *lines, payment_method=method))


@pytest.mark.parametrize(
    ("lines", "method"),
    [
        ((("1103", 120, None), ("4201", -120, None)), None),  # 沒有 MAIN
        ((("1103", 120, None), ("5101", -120, "MAIN")), None),  # MAIN 不是收入
        ((("1103", 120, None), ("4201", -120, "MAIN")), "聯邦轉帳"),  # 收入不用付款方式（P5）
        ((("5101", 120, None), ("4201", -120, "MAIN")), None),  # 收款不是資產負債
    ],
)
def test_income_structure_violations(seeded_db, lines, method):
    assert structure_errors(seeded_db, draft(seeded_db, "INCOME", *lines, payment_method=method))


@pytest.mark.parametrize(
    "lines",
    [
        (("2101", 100, None), ("1103", -100, None)),  # 轉入信用卡（D9）
        (("1101", 100, None), ("2101", -100, None)),  # 從信用卡轉出（P6）
        (("1103", 100, None), ("4201", -100, None)),  # 收入科目當轉出
        (("1103", 100, "MAIN"), ("1102", -100, None)),  # 轉帳不標 MAIN（P9）
    ],
)
def test_transfer_structure_violations(seeded_db, lines):
    assert structure_errors(seeded_db, draft(seeded_db, "TRANSFER", *lines))


def test_transfer_with_fee_passes(seeded_db):
    lines = (("1103", 100, None), ("5501", 15, "FEE"), ("1102", -115, None))
    assert structure_errors(seeded_db, draft(seeded_db, "TRANSFER", *lines)) == []


def test_transfer_fee_must_be_expense(seeded_db):
    lines = (("1103", 100, None), ("1101", 15, "FEE"), ("1102", -115, None))
    assert structure_errors(seeded_db, draft(seeded_db, "TRANSFER", *lines))


@pytest.mark.parametrize(
    "lines",
    [
        # 繳卡費夾帶費用分錄：繳款不能計入支出
        (("2101", 100, None), ("5201", 20, None), ("1103", -120, None)),
        (("2101", 100, None), ("4201", -100, None)),  # 用收入去繳
        (("2101", 100, None), ("3201", -100, None)),  # 用（非系統）權益科目去繳
        (("1102", 100, None), ("1103", -100, None)),  # 正數那筆不是信用卡
        (("2101", 100, None), ("1201", -100, None)),  # 扣款帳戶不是 CASH／BANK（P7）
        (("2101", 100, "MAIN"), ("1103", -100, None)),  # 不帶 line_role
    ],
)
def test_card_payment_structure_violations(seeded_db, lines):
    # 非系統的權益科目（系統科目會先被 SYSTEM_ACCOUNT 擋下，測不到結構規則）
    account_service.create_account(seeded_db, code="3201", name="其他權益", account_class="EQUITY")
    assert structure_errors(seeded_db, draft(seeded_db, "CARD_PAYMENT", *lines))


def test_structure_violation_writes_nothing(seeded_db):
    bad = draft(
        seeded_db, "CARD_PAYMENT", ("2101", 100, None), ("5201", 20, None), ("1103", -120, None)
    )
    with pytest.raises(ValidationFailed):
        ledger.post(seeded_db, bad)


def test_manual_has_no_structure_rules(seeded_db):
    # MANUAL 是自由多分錄，不受這些結構限制
    lines = (("5101", 100, None), ("4201", -100, None))
    assert structure_errors(seeded_db, draft(seeded_db, "MANUAL", *lines)) == []
