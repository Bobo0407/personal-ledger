# ruff: noqa: F811  （共用 test_web_txn_new.py 的 fixture：pytest 依參數名稱注入，不是重複定義）
"""明細頁的異動紀錄與 GET /txn/deleted（P2 Step 4B，Web 層）。"""

import re

from sqlalchemy import select

from app.models import AuditLog, PaymentMethod
from app.services import transactions
from app.services.entries import ExpenseForm
from tests.integration.test_web_txn_new import (  # noqa: F401
    HX,
    _today,
    anon,
    client,
    csrf_from,
    db,
    log_dir,
    session_factory,
    user_id,
)
from tests.ledger_helpers import TODAY, acc


def expense(db, amount=120, **kwargs) -> int:
    pm = db.scalar(select(PaymentMethod.id).where(PaymentMethod.name == "LINE Pay"))
    form = ExpenseForm(
        txn_date=TODAY, amount=amount, account_id=acc(db, "5101"), payment_method_id=pm, **kwargs
    )
    return transactions.record_expense(db, form, confirmed=["V10", "V11", "V12"]).transaction_id


def post(client, url, **data):
    return client.post(url, data={"csrf_token": csrf_from(client, "/txn/new"), **data}, headers=HX)


def history_block(html: str) -> str:
    m = re.search(r'<details class="history">.*?</details>', html, re.S)
    assert m, "找不到異動紀錄區塊"
    return m.group(0)


def actions(html: str) -> list[str]:
    return re.findall(r'<li class="history-entry" data-action="([A-Z]+)">', html)


# --- 異動紀錄 -------------------------------------------------------------------------------


def test_history_collapsed_with_readable_summary(client, db, session_factory):
    txn_id = expense(db)
    edit = {
        "txn_date": TODAY.isoformat(),
        "amount": "150",
        "account_id": str(acc(db, "5101")),
        "payment_method_id": str(
            db.scalar(select(PaymentMethod.id).where(PaymentMethod.name == "LINE Pay"))
        ),
        "action": "save",
    }
    post(client, f"/txn/{txn_id}/edit", **edit)
    html = client.get(f"/txn/{txn_id}").text
    block = history_block(html)
    assert block.startswith('<details class="history">')  # 沒有 open：預設收合
    assert "<summary>異動紀錄（2 筆）</summary>" in block
    assert actions(block) == ["CREATE", "UPDATE"]
    assert "建立交易" in block and "修改交易" in block
    assert "<li>金額 120</li>" in block
    assert "<li>金額：120 → 150</li>" in block
    assert (
        "5101 正餐 增加 120、1102 郵局 減少 120 → 5101 正餐 增加 150、1102 郵局 減少 150" in block
    )
    assert re.search(r"<time [^>]*>\d{4}-\d{2}-\d{2} \d{2}:\d{2}</time>", block)


def test_history_shows_no_raw_json_or_user_id(client, db, session_factory):
    txn_id = expense(db)
    post(client, f"/txn/{txn_id}/delete")
    html = client.get(f"/txn/{txn_id}").text
    for raw in (
        '"postings"',
        "account_code",
        "before_json",
        "after_json",
        "source_ref",
        "user_id",
        "{&#34;",
    ):
        assert raw not in html, raw
    with session_factory() as s:
        stored = s.scalars(
            select(AuditLog.user_id).where(AuditLog.entity_id == txn_id).order_by(AuditLog.id)
        ).all()
    assert stored == [None, user_id(db)]  # user_id 有存，只是不顯示


def test_restore_flow_from_deleted_list(client, db):
    keep = expense(db, 100)
    txn_id = expense(db, 200, description="誤刪的那筆")
    post(client, f"/txn/{txn_id}/delete")

    listing = client.get("/txn").text
    assert 'href="/txn/deleted"' in listing
    assert f'data-txn-id="{txn_id}"' not in listing
    assert 'data-count="1"' in listing and 'data-expense-net="100"' in listing

    deleted = client.get("/txn/deleted").text
    assert f'<a class="txn-link" href="/txn/{txn_id}">' in deleted
    assert f'data-txn-id="{keep}"' not in deleted
    assert "共 1 筆" in deleted and "誤刪的那筆" in deleted and "刪除於" in deleted
    assert "支出淨額" not in deleted and "收入" not in deleted

    post(client, f"/txn/{txn_id}/restore")
    assert f'data-txn-id="{txn_id}"' not in client.get("/txn/deleted").text
    assert f'data-txn-id="{txn_id}"' in client.get("/txn").text
    assert actions(history_block(client.get(f"/txn/{txn_id}").text)) == [
        "CREATE",
        "DELETE",
        "RESTORE",
    ]


def test_restore_response_includes_restore_entry(client, db):
    txn_id = expense(db)
    post(client, f"/txn/{txn_id}/delete")
    resp = post(client, f"/txn/{txn_id}/restore")
    assert actions(history_block(resp.text)) == ["CREATE", "DELETE", "RESTORE"]


def test_deleted_list_empty(client):
    assert "沒有已刪除的交易。" in client.get("/txn/deleted").text


def test_deleted_list_requires_login(anon):
    resp = anon.get("/txn/deleted")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login?next=%2Ftxn%2Fdeleted"


def test_deleted_list_pagination(client, db):
    made = [expense(db, 10 + i) for i in range(51)]
    for txn_id in made:
        transactions.delete_transaction(db, txn_id)
    first = client.get("/txn/deleted").text
    assert "第 1 / 2 頁" in first and 'href="/txn/deleted?page=2"' in first
    second = client.get("/txn/deleted?page=2").text
    assert re.findall(r'data-txn-id="(\d+)"', second) == [str(made[0])]


def test_routes_not_shadowed(client):
    assert client.get("/txn/deleted").status_code == 200
    assert client.get("/txn/999").status_code == 404
