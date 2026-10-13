"""收入、轉帳、繳卡費的修改條件、預填值與 P1 修改行為（P2 Step 5B，service 層）。

修改條件：還原成表單 → P1 builder 重建 → 和既有分錄完整比對（科目、金額、角色、原幣、memo）。
帳務規則仍全部由 P1 的 update_transaction 判斷，這裡只確認 5B 依賴的行為。
"""

import pytest
from sqlalchemy import select

from app.models import AuditLog, Merchant, Posting, Transaction
from app.services import audit, ledger, transactions, txn_detail, txn_edit
from app.services.entries import IncomeForm, TransferForm
from app.services.errors import RuleViolation
from app.services.validation import ValidationFailed
from tests.ledger_helpers import TODAY, acc, freeze_today, insert_raw_transaction


@pytest.fixture(autouse=True)
def _today(monkeypatch):
    freeze_today(monkeypatch)


def income(db, **kwargs) -> int:
    form = IncomeForm(TODAY, 3000, acc(db, "4201"), acc(db, "1102"), **kwargs)
    return transactions.record_income(db, form).transaction_id


def transfer(db, *, to="1103", amount=2000, **kwargs) -> int:
    form = TransferForm(TODAY, amount, acc(db, "1102"), acc(db, to), **kwargs)
    return transactions.record_transfer(db, form).transaction_id


def fee_transfer(db) -> int:
    return transfer(db, amount=5000, fee_amount=15, fee_account_id=acc(db, "5501"))


def blockers(db, txn_id) -> tuple[str, ...]:
    return txn_detail.get_detail(db, txn_id).edit_blockers


def errors_of(exc_info) -> list[tuple[str, str | None]]:
    return [(i.code, i.field) for i in exc_info.value.result.errors]


# --- 可以修改的格式與預填值 ---------------------------------------------------------------


def test_income_prefill(seeded_db):
    txn_id = income(seeded_db, description="利息")
    assert blockers(seeded_db, txn_id) == ()
    assert txn_detail.edit_values(seeded_db, txn_id) == {
        "txn_date": TODAY.isoformat(),
        "amount": "3000",
        "account_id": str(acc(seeded_db, "4201")),
        "receive_account_id": str(acc(seeded_db, "1102")),
        "description": "利息",
    }


def test_transfer_prefill_with_and_without_fee(seeded_db):
    plain = transfer(seeded_db)
    assert txn_detail.edit_values(seeded_db, plain) == {
        "txn_date": TODAY.isoformat(),
        "amount": "2000",
        "from_account_id": str(acc(seeded_db, "1102")),
        "to_account_id": str(acc(seeded_db, "1103")),
        "fee_amount": "",
        "fee_account_id": "",
        "description": "",
    }
    with_fee = txn_detail.edit_values(seeded_db, fee_transfer(seeded_db))
    assert (with_fee["amount"], with_fee["fee_amount"]) == ("5000", "15")
    assert with_fee["fee_account_id"] == str(acc(seeded_db, "5501"))


def test_card_payment_prefill_uses_transfer_fields(seeded_db):
    txn_id = transfer(seeded_db, to="2101")
    assert seeded_db.get(Transaction, txn_id).txn_type == "CARD_PAYMENT"
    values = txn_detail.edit_values(seeded_db, txn_id)
    assert values["to_account_id"] == str(acc(seeded_db, "2101"))
    assert values["from_account_id"] == str(acc(seeded_db, "1102"))
    assert (values["amount"], values["fee_amount"], values["fee_account_id"]) == ("2000", "", "")


# --- 不能完整重建 → 不開放修改 -----------------------------------------------------------


def test_income_with_merchant_is_not_editable(seeded_db):
    merchant = Merchant(name="公司")
    seeded_db.add(merchant)
    seeded_db.commit()
    txn_id = income(seeded_db, merchant_id=merchant.id)
    assert blockers(seeded_db, txn_id) == (txn_edit.INCOME_WITH_MERCHANT,)
    with pytest.raises(RuleViolation) as exc:
        txn_detail.require_editable(seeded_db, txn_id)
    assert exc.value.code == "NOT_EDITABLE"


@pytest.mark.parametrize(
    ("txn_type", "lines"),
    [
        # 沒有 MAIN 標記的收入（例如資料庫裡手寫的）
        ("INCOME", (("1102", 100), ("4201", -100))),
        # 兩筆轉入
        ("TRANSFER", (("1103", 60), ("1101", 40), ("1102", -100))),
        # 繳卡費三筆分錄
        ("CARD_PAYMENT", (("2101", 60), ("1101", -40), ("1102", -20))),
    ],
)
def test_raw_shapes_that_builder_cannot_rebuild(seeded_db, txn_type, lines):
    txn_id = insert_raw_transaction(seeded_db, TODAY.isoformat(), *lines, txn_type=txn_type)
    assert blockers(seeded_db, txn_id) == (txn_edit.NOT_REBUILDABLE,)


def test_memo_or_foreign_currency_cannot_be_rebuilt(seeded_db):
    with_memo = transfer(seeded_db)
    foreign = income(seeded_db)
    seeded_db.scalars(select(Posting).where(Posting.transaction_id == with_memo)).first().memo = "x"
    p = seeded_db.scalars(select(Posting).where(Posting.transaction_id == foreign)).first()
    p.original_currency, p.original_amount_minor = "USD", 9999
    seeded_db.commit()
    assert blockers(seeded_db, with_memo) == (txn_edit.NOT_REBUILDABLE,)
    assert blockers(seeded_db, foreign) == (txn_edit.NOT_REBUILDABLE,)


def test_extra_header_fields_cannot_be_edited(seeded_db):
    txn_id = transfer(seeded_db)
    seeded_db.get(Transaction, txn_id).invoice_no = "AB12345678"
    seeded_db.commit()
    assert blockers(seeded_db, txn_id) == (txn_edit.EXTRA_HEADER,)


def test_deleted_and_draft(seeded_db):
    txn_id = transfer(seeded_db)
    transactions.delete_transaction(seeded_db, txn_id)
    assert blockers(seeded_db, txn_id) == ("已刪除的交易要先還原才能修改",)
    draft = insert_raw_transaction(
        seeded_db,
        TODAY.isoformat(),
        ("1103", 50),
        ("1102", -50),
        txn_type="TRANSFER",
        status="DRAFT",
    )
    assert blockers(seeded_db, draft) == ("草稿交易不提供修改",)


# --- P1 修改行為（5B 依賴） ----------------------------------------------------------------


def test_card_payment_edited_to_non_card_is_type_immutable(seeded_db):
    """和 P1 既有的「轉帳改成轉入信用卡」對稱：繳卡費改成非信用卡 → 草稿變轉帳 → 擋下。"""
    txn_id = transfer(seeded_db, to="2101")
    before = ledger.transaction_snapshot(seeded_db, seeded_db.get(Transaction, txn_id))
    form = TransferForm(TODAY, 2000, acc(seeded_db, "1102"), acc(seeded_db, "1103"))
    with pytest.raises(ValidationFailed) as exc:
        transactions.update_transaction(seeded_db, txn_id, form)
    assert errors_of(exc) == [("TXN_TYPE_IMMUTABLE", None)]
    seeded_db.expire_all()
    assert ledger.transaction_snapshot(seeded_db, seeded_db.get(Transaction, txn_id)) == before


def test_card_payment_with_fee_is_structure_error(seeded_db):
    txn_id = transfer(seeded_db, to="2101")
    form = TransferForm(
        TODAY, 2000, acc(seeded_db, "1102"), acc(seeded_db, "2101"), 15, acc(seeded_db, "5501")
    )
    with pytest.raises(ValidationFailed) as exc:
        transactions.update_transaction(seeded_db, txn_id, form)
    assert errors_of(exc) == [("TXN_STRUCTURE", "fee_amount")]


def test_rollback_when_audit_fails_on_fee_removal(seeded_db, monkeypatch):
    """3 筆分錄（含手續費）改成 2 筆時稽核失敗：原本 3 筆分錄、表頭、稽核全部還原。"""
    txn_id = fee_transfer(seeded_db)
    before = ledger.transaction_snapshot(seeded_db, seeded_db.get(Transaction, txn_id))
    assert len(before["postings"]) == 3
    logs = seeded_db.query(AuditLog).count()

    def boom(*args, **kwargs):
        raise RuntimeError("audit failed")

    monkeypatch.setattr(audit, "record", boom)
    form = TransferForm(
        TODAY, 4000, acc(seeded_db, "1102"), acc(seeded_db, "1103"), description="x"
    )
    with pytest.raises(RuntimeError):
        transactions.update_transaction(seeded_db, txn_id, form)

    seeded_db.expire_all()
    txn = seeded_db.get(Transaction, txn_id)
    assert ledger.transaction_snapshot(seeded_db, txn) == before
    assert len(txn.postings) == 3
    assert seeded_db.query(AuditLog).count() == logs
