# ruff: noqa: F811  （共用 test_web_txn_new.py 的 fixture：pytest 依參數名稱注入，不是重複定義）
"""P2 Step 8A 回歸：既有的 Web 修改流程（4A 支出、5B 收入／轉帳、6 進階支出）不傳 tag_ids，
修改後原本的標籤必須保留（tag_ids=None = 不處理標籤）。標籤 UI 在 8B。"""

from sqlalchemy import select

from app.models import PaymentMethod, Tag
from app.repositories import tags as tag_repo
from app.services import transactions
from app.services.entries import ExpenseForm, TransferForm
from tests.integration.test_web_txn_new import (  # noqa: F401
    HX,
    _today,
    anon,
    client,
    csrf_from,
    db,
    input_value,
    log_dir,
    selected_value,
    session_factory,
)
from tests.ledger_helpers import TODAY, acc


def tag(db, name) -> int:
    row = Tag(name=name)
    db.add(row)
    db.commit()
    return row.id


def test_expense_edit_keeps_tags(client, db, session_factory):
    trip = tag(db, "旅行")
    pm = db.scalar(select(PaymentMethod.id).where(PaymentMethod.name == "LINE Pay"))
    txn_id = transactions.record_expense(
        db, ExpenseForm(TODAY, 120, acc(db, "5101"), pm), tag_ids=[trip]
    ).transaction_id
    html = client.get(f"/txn/{txn_id}/edit").text
    resp = client.post(
        f"/txn/{txn_id}/edit",
        data={
            "csrf_token": csrf_from(client, "/txn/new"),
            "txn_date": input_value(html, "txn_date"),
            "amount": "150",
            "account_id": selected_value(html, "account_id"),
            "payment_method_id": selected_value(html, "payment_method_id"),
            "description": "改",
            "action": "save",
        },
        headers=HX,
    )
    assert resp.status_code == 200
    with session_factory() as s:
        assert tag_repo.ids_for_transaction(s, txn_id) == (trip,)


def test_transfer_edit_keeps_tags(client, db, session_factory):
    trip = tag(db, "旅行")
    txn_id = transactions.record_transfer(
        db, TransferForm(TODAY, 500, acc(db, "1102"), acc(db, "1103")), tag_ids=[trip]
    ).transaction_id
    html = client.get(f"/txn/{txn_id}/edit").text
    resp = client.post(
        f"/txn/{txn_id}/edit",
        data={
            "csrf_token": csrf_from(client, "/txn/new"),
            "txn_date": input_value(html, "txn_date"),
            "amount": "600",
            "from_account_id": selected_value(html, "from_account_id"),
            "to_account_id": selected_value(html, "to_account_id"),
            "action": "save",
        },
        headers=HX,
    )
    assert resp.status_code == 200
    with session_factory() as s:
        assert tag_repo.ids_for_transaction(s, txn_id) == (trip,)
