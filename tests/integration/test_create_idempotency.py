"""收入、轉帳、繳卡費的一次性送出權杖（P2 Step 5A）。

record_income／record_transfer 和 record_expense 共用 transactions._post_once：
- 有權杖：source=MANUAL、source_ref=submit:<token>；同一權杖重送只回傳第一筆，不驗證、不寫入、
  不新增稽核；並發時靠 UNIQUE(source, source_ref) 擋下，再改成重送。
- 沒有權杖：維持原本的 ledger.post（Web 才強制權杖，service 呼叫端不需要）。
"""

import pytest
from sqlalchemy import func, select

from app.models import AuditLog, PaymentMethod, Transaction
from app.services import transactions
from app.services.entries import ExpenseForm, IncomeForm, TransferForm
from app.services.validation import ConfirmationRequired, ValidationFailed
from tests.ledger_helpers import TODAY, acc, freeze_today

TOKEN = "k" * 32


@pytest.fixture(autouse=True)
def _today(monkeypatch):
    freeze_today(monkeypatch)


def income(db, *, amount=3000, **kwargs) -> IncomeForm:
    return IncomeForm(
        txn_date=kwargs.pop("txn_date", TODAY),
        amount=amount,
        account_id=acc(db, "4101"),
        receive_account_id=acc(db, "1102"),
        **kwargs,
    )


def transfer(db, *, amount=2000, to="1103", **kwargs) -> TransferForm:
    return TransferForm(
        txn_date=kwargs.pop("txn_date", TODAY),
        amount=amount,
        from_account_id=acc(db, kwargs.pop("frm", "1102")),
        to_account_id=acc(db, to),
        **kwargs,
    )


def count(db, model) -> int:
    return db.scalar(select(func.count()).select_from(model))


# --- 沒有權杖：service 呼叫端照舊可用（Web 才強制權杖） -----------------------------------


def test_record_income_without_token_posts_normally(seeded_db):
    result = transactions.record_income(seeded_db, income(seeded_db))
    txn = seeded_db.get(Transaction, result.transaction_id)
    assert (txn.txn_type, txn.source, txn.source_ref) == ("INCOME", "MANUAL", None)
    assert result.replayed is False
    # 沒有權杖就沒有防重：同樣內容再記一次是另一筆
    transactions.record_income(seeded_db, income(seeded_db))
    assert count(seeded_db, Transaction) == 2


def test_record_transfer_without_token_posts_normally(seeded_db):
    result = transactions.record_transfer(seeded_db, transfer(seeded_db))
    txn = seeded_db.get(Transaction, result.transaction_id)
    assert (txn.txn_type, txn.source_ref) == ("TRANSFER", None)
    card = transactions.record_transfer(seeded_db, transfer(seeded_db, to="2101"))
    assert seeded_db.get(Transaction, card.transaction_id).txn_type == "CARD_PAYMENT"


def test_record_expense_without_token_still_posts(seeded_db):
    """Step 2 起支出就允許不帶權杖（service 層）；抽出 _post_once 之後不能回歸。"""
    pm = seeded_db.scalar(select(PaymentMethod.id).where(PaymentMethod.name == "LINE Pay"))
    form = ExpenseForm(
        txn_date=TODAY, amount=85, account_id=acc(seeded_db, "5101"), payment_method_id=pm
    )
    first = transactions.record_expense(seeded_db, form)
    assert seeded_db.get(Transaction, first.transaction_id).source_ref is None
    assert first.replayed is False


# --- 有權杖：收入、轉帳、繳卡費都和支出一樣 ------------------------------------------------


@pytest.mark.parametrize(
    ("record", "make", "txn_type"),
    [
        (transactions.record_income, income, "INCOME"),
        (transactions.record_transfer, transfer, "TRANSFER"),
        (
            transactions.record_transfer,
            lambda db, **kw: transfer(db, to="2101", **kw),
            "CARD_PAYMENT",
        ),
    ],
    ids=["income", "transfer", "card_payment"],
)
def test_token_replay_returns_first_without_writing(seeded_db, record, make, txn_type):
    first = record(seeded_db, make(seeded_db), submission_token=TOKEN, user_id=None)
    txn = seeded_db.get(Transaction, first.transaction_id)
    # 轉入信用卡自動改成繳卡費（D9）之後，source_ref 仍是這次的送出權杖
    assert (txn.txn_type, txn.source, txn.source_ref) == (txn_type, "MANUAL", f"submit:{TOKEN}")

    # 第二次內容不同、甚至不合法，也不驗證、不寫入、不新增稽核
    second = record(seeded_db, make(seeded_db, amount=0), submission_token=TOKEN)
    assert second.replayed is True
    assert second.transaction_id == first.transaction_id
    assert second.notices == ()
    assert count(seeded_db, Transaction) == 1
    assert count(seeded_db, AuditLog) == 1


def test_token_is_shared_across_types(seeded_db):
    """權杖只認 source_ref，不分類型：同一份表單換了類型重送也不會再建一筆。"""
    first = transactions.record_income(seeded_db, income(seeded_db), submission_token=TOKEN)
    again = transactions.record_transfer(seeded_db, transfer(seeded_db), submission_token=TOKEN)
    assert again.replayed is True
    assert again.transaction_id == first.transaction_id
    assert count(seeded_db, Transaction) == 1


@pytest.mark.parametrize("to", ["1103", "2101"], ids=["transfer", "card_payment"])
def test_transfer_concurrent_duplicate_hits_unique_and_replays(seeded_db, monkeypatch, to):
    first = transactions.record_transfer(
        seeded_db, transfer(seeded_db, to=to), submission_token=TOKEN
    )
    real = transactions._replayed
    calls = {"n": 0}

    def flaky(db, ref):
        calls["n"] += 1
        return None if calls["n"] == 1 else real(db, ref)

    monkeypatch.setattr(transactions, "_replayed", flaky)
    second = transactions.record_transfer(
        seeded_db, transfer(seeded_db, to=to), submission_token=TOKEN
    )
    assert second.replayed is True
    assert second.transaction_id == first.transaction_id
    assert count(seeded_db, Transaction) == 1
    assert count(seeded_db, AuditLog) == 1


def test_income_concurrent_duplicate_hits_unique_and_replays(seeded_db, monkeypatch):
    first = transactions.record_income(seeded_db, income(seeded_db), submission_token=TOKEN)
    real = transactions._replayed
    calls = {"n": 0}

    def flaky(db, ref):
        calls["n"] += 1
        return None if calls["n"] == 1 else real(db, ref)

    monkeypatch.setattr(transactions, "_replayed", flaky)
    second = transactions.record_income(seeded_db, income(seeded_db), submission_token=TOKEN)
    assert second.replayed is True
    assert second.transaction_id == first.transaction_id
    assert count(seeded_db, AuditLog) == 1


def test_rejected_or_unconfirmed_submission_does_not_consume_token(seeded_db):
    with pytest.raises(ValidationFailed):
        transactions.record_transfer(
            seeded_db, transfer(seeded_db, amount=0), submission_token=TOKEN
        )
    with pytest.raises(ConfirmationRequired):
        transactions.record_transfer(
            seeded_db, transfer(seeded_db, amount=12000, to="2101"), submission_token=TOKEN
        )
    result = transactions.record_transfer(
        seeded_db,
        transfer(seeded_db, amount=12000, to="2101"),
        submission_token=TOKEN,
        confirmed=["V10"],
    )
    assert result.replayed is False
    assert seeded_db.get(Transaction, result.transaction_id).txn_type == "CARD_PAYMENT"
