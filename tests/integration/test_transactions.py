"""日常交易流程 transactions.record_*（Step 4；T01–T04、T08、T09、D9、F14、P5、P12）。"""

import json
from datetime import date

import pytest
from sqlalchemy import func, select

from app.models import AuditLog, Posting, Transaction
from app.services import accounts as account_service
from app.services import ledger_queries as q
from app.services import payment_methods as pm_service
from app.services import transactions as tx
from app.services.entries import CardPaymentForm, ExpenseForm, IncomeForm, TransferForm
from app.services.validation import ConfirmationRequired, ValidationFailed
from tests.ledger_helpers import TODAY, acc, freeze_today

OCTOBER = (date(2026, 10, 1), date(2026, 10, 31))


@pytest.fixture(autouse=True)
def _today(monkeypatch):
    freeze_today(monkeypatch)


def pm(db, name: str) -> int:
    return next(p.id for p in pm_service.list_payment_methods(db, False) if p.name == name)


def lines(db, txn_id: int) -> list[tuple[str, int, str | None]]:
    """(科目代碼, 金額, line_role)，依 line_no 排序。"""
    txn = db.get(Transaction, txn_id)
    return [(p.account.code, p.amount, p.line_role) for p in txn.postings]


def counts(db) -> tuple[int, int]:
    return (
        db.scalar(select(func.count()).select_from(Transaction)),
        db.scalar(select(func.count()).select_from(Posting)),
    )


def expense(db, amount, account="5101", method="LINE Pay"):
    form = ExpenseForm(TODAY, amount, acc(db, account), pm(db, method))
    return tx.record_expense(db, form)


def balance(db, code: str) -> int:
    return q.balance(db, acc(db, code))


# --- T01–T04 ----------------------------------------------------------------------


def test_t01_lunch_with_line_pay(seeded_db):
    result = expense(seeded_db, 120)
    txn = seeded_db.get(Transaction, result.transaction_id)

    assert txn.txn_type == "EXPENSE"
    assert txn.payment_method_id == pm(seeded_db, "LINE Pay")  # 表頭保留付款管道
    assert lines(seeded_db, txn.id) == [("5101", 120, "MAIN"), ("1102", -120, None)]
    assert balance(seeded_db, "1102") == -120  # 付款方式只是管道，實際扣的是郵局


def test_t02_online_shopping_on_credit_card(seeded_db):
    expense(seeded_db, 800, "5201", "聯邦信用卡")
    assert balance(seeded_db, "2101") == 800  # 信用卡欠款增加
    assert balance(seeded_db, "1102") == 0  # 郵局不受影響
    assert q.class_total(seeded_db, "EXPENSE", *OCTOBER) == 800  # 刷卡當天認列


def test_t03_card_payment_is_not_expense(seeded_db):
    expense(seeded_db, 800, "5201", "聯邦信用卡")
    form = CardPaymentForm(TODAY, 800, acc(seeded_db, "2101"), acc(seeded_db, "1103"))
    result = tx.record_card_payment(seeded_db, form)

    assert seeded_db.get(Transaction, result.transaction_id).txn_type == "CARD_PAYMENT"
    assert lines(seeded_db, result.transaction_id) == [("2101", 800, None), ("1103", -800, None)]
    assert balance(seeded_db, "2101") == 0
    assert q.class_total(seeded_db, "EXPENSE", *OCTOBER) == 800  # 不是 1,600


def test_t04_transfer_with_fee(seeded_db):
    form = TransferForm(
        TODAY,
        5000,
        acc(seeded_db, "1102"),
        acc(seeded_db, "1103"),
        fee_amount=15,
        fee_account_id=acc(seeded_db, "5501"),
    )
    result = tx.record_transfer(seeded_db, form)
    txn = seeded_db.get(Transaction, result.transaction_id)

    assert txn.txn_type == "TRANSFER"
    assert txn.payment_method_id is None
    assert lines(seeded_db, txn.id) == [
        ("1103", 5000, None),
        ("5501", 15, "FEE"),
        ("1102", -5015, None),
    ]
    assert sum(p.amount for p in txn.postings) == 0
    assert q.class_total(seeded_db, "EXPENSE", *OCTOBER) == 15  # 只有手續費計入支出


# --- T08、T09 ----------------------------------------------------------------------


def test_t08_foreign_card_purchase_with_fee(seeded_db):
    form = ExpenseForm(
        TODAY,
        1000,
        acc(seeded_db, "5301"),
        pm(seeded_db, "聯邦信用卡"),
        fee_amount=15,
        fee_account_id=acc(seeded_db, "5502"),
        original_currency="JPY",
        original_amount_minor=4800,
    )
    txn_id = tx.record_expense(seeded_db, form).transaction_id
    txn = seeded_db.get(Transaction, txn_id)

    assert balance(seeded_db, "2101") == 1015
    main, fee, card = txn.postings
    assert (main.amount, main.line_role, main.original_currency, main.original_amount_minor) == (
        1000,
        "MAIN",
        "JPY",
        4800,
    )
    assert (fee.amount, fee.line_role, fee.original_currency) == (15, "FEE", None)
    assert card.amount == -1015


def test_t09_installment_full_amount_plus_interest(seeded_db):
    form = ExpenseForm(
        TODAY,
        30_000,
        acc(seeded_db, "5201"),
        pm(seeded_db, "聯邦信用卡"),
        interest_amount=600,
        interest_account_id=acc(seeded_db, "5503"),
    )
    with pytest.raises(ConfirmationRequired):  # V10：30,600 > 10,000
        tx.record_expense(seeded_db, form)
    txn_id = tx.record_expense(seeded_db, form, confirmed={"V10"}).transaction_id

    assert balance(seeded_db, "2101") == 30_600
    assert balance(seeded_db, "5503") == 600  # 利息記在金融費用
    assert balance(seeded_db, "5201") == 30_000  # 本金全額入帳
    roles = [p.line_role for p in seeded_db.get(Transaction, txn_id).postings]
    assert roles == ["MAIN", "INTEREST", None]


def test_p12_interest_only_on_credit_card(seeded_db):
    form = ExpenseForm(
        TODAY,
        3000,
        acc(seeded_db, "5201"),
        pm(seeded_db, "現金"),
        interest_amount=100,
        interest_account_id=acc(seeded_db, "5503"),
    )
    with pytest.raises(ValidationFailed) as exc_info:
        tx.record_expense(seeded_db, form)
    assert [i.code for i in exc_info.value.result.errors] == ["TXN_STRUCTURE"]
    assert counts(seeded_db) == (0, 0)


def test_p12_interest_account_must_be_expense(seeded_db):
    form = ExpenseForm(
        TODAY,
        3000,
        acc(seeded_db, "5201"),
        pm(seeded_db, "聯邦信用卡"),
        interest_amount=100,
        interest_account_id=acc(seeded_db, "4201"),
    )
    with pytest.raises(ValidationFailed) as exc_info:
        tx.record_expense(seeded_db, form)
    assert {i.code for i in exc_info.value.result.errors} == {"TXN_STRUCTURE"}


# --- D9：轉入信用卡自動改成信用卡繳款 ----------------------------------------------------


def test_d9_transfer_to_credit_card_becomes_card_payment(seeded_db):
    form = TransferForm(TODAY, 500, acc(seeded_db, "1103"), acc(seeded_db, "2101"))
    txn = seeded_db.get(Transaction, tx.record_transfer(seeded_db, form).transaction_id)
    assert txn.txn_type == "CARD_PAYMENT"
    assert lines(seeded_db, txn.id) == [("2101", 500, None), ("1103", -500, None)]


def test_d9_card_payment_does_not_take_fee(seeded_db):
    form = TransferForm(
        TODAY,
        500,
        acc(seeded_db, "1103"),
        acc(seeded_db, "2101"),
        fee_amount=15,
        fee_account_id=acc(seeded_db, "5501"),
    )
    with pytest.raises(ValidationFailed) as exc_info:
        tx.record_transfer(seeded_db, form)
    assert [(i.code, i.field) for i in exc_info.value.result.errors] == [
        ("TXN_STRUCTURE", "fee_amount")
    ]
    assert counts(seeded_db) == (0, 0)


def test_p6_transfer_from_credit_card_rejected(seeded_db):
    form = TransferForm(TODAY, 500, acc(seeded_db, "2101"), acc(seeded_db, "1101"))
    with pytest.raises(ValidationFailed) as exc_info:
        tx.record_transfer(seeded_db, form)
    assert [i.code for i in exc_info.value.result.errors] == ["TXN_STRUCTURE"]


@pytest.mark.parametrize("source", ["1101", "1103"])
def test_p7_card_payment_from_cash_or_bank(seeded_db, source):
    form = CardPaymentForm(TODAY, 100, acc(seeded_db, "2101"), acc(seeded_db, source))
    assert tx.record_card_payment(seeded_db, form).transaction_id


def test_p7_card_payment_source_must_be_cash_or_bank(seeded_db):
    form = CardPaymentForm(TODAY, 100, acc(seeded_db, "2101"), acc(seeded_db, "1201"))
    with pytest.raises(ValidationFailed) as exc_info:
        tx.record_card_payment(seeded_db, form)
    assert [i.code for i in exc_info.value.result.errors] == ["TXN_STRUCTURE"]


# --- 收入（P5、P9） -------------------------------------------------------------------


def test_income_into_receive_account(seeded_db):
    form = IncomeForm(TODAY, 120, acc(seeded_db, "4201"), receive_account_id=acc(seeded_db, "1103"))
    txn = seeded_db.get(Transaction, tx.record_income(seeded_db, form).transaction_id)

    assert txn.txn_type == "INCOME"
    assert txn.payment_method_id is None
    assert lines(seeded_db, txn.id) == [("1103", 120, None), ("4201", -120, "MAIN")]
    assert balance(seeded_db, "1103") == 120
    assert q.class_total(seeded_db, "INCOME", *OCTOBER) == 120


def test_income_account_must_be_income(seeded_db):
    form = IncomeForm(TODAY, 100, acc(seeded_db, "5101"), receive_account_id=acc(seeded_db, "1101"))
    with pytest.raises(ValidationFailed) as exc_info:
        tx.record_income(seeded_db, form)
    assert [i.code for i in exc_info.value.result.errors] == ["TXN_STRUCTURE"]


# --- F14：應收／應付用轉帳記錄 -----------------------------------------------------------


@pytest.fixture
def friend_accounts(seeded_db):
    receivable = account_service.create_account(
        seeded_db, code="1401", name="應收-小明", account_class="ASSET", subtype="RECEIVABLE"
    )
    payable = account_service.create_account(
        seeded_db, code="2201", name="應付-小明", account_class="LIABILITY", subtype="PAYABLE"
    )
    return receivable.id, payable.id


def transfer(db, amount, from_id, to_id):
    return tx.record_transfer(db, TransferForm(TODAY, amount, from_id, to_id))


def test_f14_advance_and_repayment(seeded_db, friend_accounts):
    receivable, _ = friend_accounts
    post_office = acc(seeded_db, "1102")
    transfer(seeded_db, 500, post_office, receivable)  # 幫小明代墊
    assert q.balance(seeded_db, receivable) == 500
    transfer(seeded_db, 500, receivable, post_office)  # 小明還錢
    assert q.balance(seeded_db, receivable) == 0
    assert q.class_total(seeded_db, "EXPENSE", *OCTOBER) == 0
    assert q.class_total(seeded_db, "INCOME", *OCTOBER) == 0


def test_f14_borrow_and_repay(seeded_db, friend_accounts):
    _, payable = friend_accounts
    cash = acc(seeded_db, "1101")
    transfer(seeded_db, 1000, payable, cash)  # 向小明借錢
    assert q.balance(seeded_db, payable) == 1000  # 欠小明 1,000
    assert q.balance(seeded_db, cash) == 1000
    transfer(seeded_db, 1000, cash, payable)  # 還錢
    assert q.balance(seeded_db, payable) == 0
    assert q.net_worth(seeded_db) == 0


# --- 流程與錯誤 -----------------------------------------------------------------------


def test_form_errors_stop_before_building(seeded_db):
    form = ExpenseForm(TODAY, -120, acc(seeded_db, "5101"), None)
    with pytest.raises(ValidationFailed) as exc_info:
        tx.record_expense(seeded_db, form)
    assert [(i.code, i.field) for i in exc_info.value.result.errors] == [
        ("V01", "amount"),
        ("V05", "payment_method_id"),
    ]
    assert counts(seeded_db) == (0, 0)


def test_unknown_payment_method(seeded_db):
    form = ExpenseForm(TODAY, 120, acc(seeded_db, "5101"), 999_999)
    with pytest.raises(ValidationFailed) as exc_info:
        tx.record_expense(seeded_db, form)
    assert [(i.code, i.field) for i in exc_info.value.result.errors] == [
        ("V05", "payment_method_id")
    ]


def test_inactive_payment_method(seeded_db):
    taiwan_pay = pm(seeded_db, "台灣Pay")
    pm_service.deactivate_payment_method(seeded_db, taiwan_pay)
    form = ExpenseForm(TODAY, 120, acc(seeded_db, "5101"), taiwan_pay)
    with pytest.raises(ValidationFailed) as exc_info:
        tx.record_expense(seeded_db, form)
    assert [(i.code, i.field) for i in exc_info.value.result.errors] == [
        ("V04", "payment_method_id")
    ]


def test_v06_transfer_same_account(seeded_db):
    post_office = acc(seeded_db, "1102")
    with pytest.raises(ValidationFailed) as exc_info:
        transfer(seeded_db, 100, post_office, post_office)
    assert [i.code for i in exc_info.value.result.errors] == ["V06"]


def test_missing_date_reported_by_ledger_validation(seeded_db):
    form = ExpenseForm(None, 120, acc(seeded_db, "5101"), pm(seeded_db, "LINE Pay"))
    with pytest.raises(ValidationFailed) as exc_info:
        tx.record_expense(seeded_db, form)
    assert [(i.code, i.field) for i in exc_info.value.result.errors] == [("V05", "txn_date")]


def test_expense_on_non_leaf_category(seeded_db):
    with pytest.raises(ValidationFailed) as exc_info:
        expense(seeded_db, 120, "5100")
    assert [i.code for i in exc_info.value.result.errors] == ["V03"]


def test_audit_log_records_txn_type(seeded_db):
    expense(seeded_db, 800, "5201", "聯邦信用卡")
    log = seeded_db.scalar(select(AuditLog))
    assert json.loads(log.after_json)["txn_type"] == "EXPENSE"
