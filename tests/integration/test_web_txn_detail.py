# ruff: noqa: F811  （共用 test_web_txn_new.py 的 fixture：pytest 依參數名稱注入，不是重複定義）
"""GET /txn/{id}、POST /txn/{id}/delete、POST /txn/{id}/restore（P2 Step 4A，Web 層）。"""

import re

from sqlalchemy import func, select

from app.models import AuditLog, PaymentMethod, Transaction
from app.services import audit, transactions
from app.services.entries import ExpenseForm, OpeningForm, OpeningLine, RefundForm
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
from tests.ledger_helpers import TODAY, acc, post_manual


def pm(db, name="LINE Pay") -> int:
    return db.scalar(select(PaymentMethod.id).where(PaymentMethod.name == name))


def expense(db, amount=120, **kwargs) -> int:
    form = ExpenseForm(
        txn_date=TODAY,
        amount=amount,
        account_id=acc(db, "5101"),
        **{"payment_method_id": pm(db), **kwargs},
    )
    return transactions.record_expense(db, form, confirmed=["V10", "V11", "V12"]).transaction_id


def post(client, url, **data):
    token = csrf_from(client, "/txn/new")
    return client.post(url, data={"csrf_token": token, **data}, headers=HX)


def state(session_factory, txn_id) -> str | None:
    with session_factory() as s:
        return s.get(Transaction, txn_id).deleted_at


def audits(session_factory, txn_id) -> list[tuple[str, int | None]]:
    with session_factory() as s:
        rows = s.scalars(
            select(AuditLog).where(AuditLog.entity_id == txn_id).order_by(AuditLog.id)
        ).all()
        return [(r.action, r.user_id) for r in rows]


def banner_codes(html: str) -> list[str]:
    m = re.search(r'<div class="alert" role="alert">(.*?)</div>', html, re.S)
    return re.findall(r'data-code="([^"]+)"', m.group(1)) if m else []


# --- 明細 -------------------------------------------------------------------------------


def test_detail_requires_login(anon, db):
    txn_id = expense(db)
    resp = anon.get(f"/txn/{txn_id}")
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/login?next=%2Ftxn%2F{txn_id}"


def test_detail_page(client, db):
    txn_id = expense(db, description="午餐")
    resp = client.get(f"/txn/{txn_id}")
    assert resp.status_code == 200
    html = resp.text
    assert '<span class="badge" data-type="EXPENSE">支出</span>' in html
    assert "<dd>午餐</dd>" in html
    assert '<span class="posting-effect" data-effect="增加">增加 120</span>' in html
    assert '<span class="posting-effect" data-effect="減少">減少 120</span>' in html
    assert f'href="/txn/{txn_id}/edit"' in html
    assert f'hx-post="/txn/{txn_id}/delete"' in html
    assert "還原這筆交易" not in html


def test_list_rows_link_to_detail(client, db):
    txn_id = expense(db)
    assert f'<a class="txn-link" href="/txn/{txn_id}">' in client.get("/txn").text


def test_not_found_is_404(client):
    resp = client.get("/txn/999")
    assert resp.status_code == 404
    assert "找不到這筆交易" in resp.text
    for url in ("/txn/999/delete", "/txn/999/restore", "/txn/999/edit"):
        resp = post(client, url)
        assert resp.status_code == 404, url
        assert resp.headers["HX-Redirect"] == "/txn/999"
    assert client.get("/txn/999/edit").status_code == 404


def test_new_route_is_not_shadowed(client):
    assert client.get("/txn/new").status_code == 200


# --- 刪除 -------------------------------------------------------------------------------


def test_delete_then_deleted_detail(client, db, session_factory):
    txn_id = expense(db)
    resp = post(client, f"/txn/{txn_id}/delete")
    assert resp.status_code == 200
    assert resp.headers["HX-Redirect"] == f"/txn/{txn_id}"
    assert state(session_factory, txn_id) is not None
    assert audits(session_factory, txn_id) == [("CREATE", None), ("DELETE", user_id(db))]

    html = client.get(f"/txn/{txn_id}").text
    assert 'data-state="deleted"' in html
    assert "還原這筆交易" in html
    assert f'href="/txn/{txn_id}/edit"' not in html
    assert "確定刪除" not in html

    listing = client.get("/txn").text
    assert f'data-txn-id="{txn_id}"' not in listing
    assert 'data-count="0"' in listing


def test_delete_without_csrf_is_rejected(client, db, session_factory):
    txn_id = expense(db)
    resp = client.post(f"/txn/{txn_id}/delete", headers=HX)
    assert resp.status_code == 403
    assert state(session_factory, txn_id) is None


def test_delete_expense_with_refund_is_blocked(client, db, session_factory):
    original = expense(db, 1000)
    transactions.record_refund(db, RefundForm(original, TODAY, 600, pm(db)))
    html = client.get(f"/txn/{original}").text
    assert "這筆支出已退 600 元，請先刪除退款" in html
    assert "確定刪除" not in html

    resp = post(client, f"/txn/{original}/delete")  # 直接 POST 仍由 P1 擋下
    assert resp.status_code == 422
    assert banner_codes(resp.text) == ["REFUND_EXISTS"]
    assert "<html" not in resp.text
    assert state(session_factory, original) is None


def test_delete_twice_is_422(client, db):
    txn_id = expense(db)
    post(client, f"/txn/{txn_id}/delete")
    resp = post(client, f"/txn/{txn_id}/delete")
    assert resp.status_code == 422
    assert banner_codes(resp.text) == ["TXN_DELETED"]


# --- 還原 -------------------------------------------------------------------------------


def test_restore_back_to_active_detail(client, db, session_factory):
    txn_id = expense(db)
    post(client, f"/txn/{txn_id}/delete")
    resp = post(client, f"/txn/{txn_id}/restore")
    assert resp.status_code == 200
    assert "HX-Redirect" not in resp.headers  # 直接回傳明細（換掉 #txn-detail），才能顯示提醒
    assert "已還原。" in resp.text
    assert 'data-state="deleted"' not in resp.text
    assert "<html" not in resp.text and "<title>" not in resp.text
    assert state(session_factory, txn_id) is None
    uid = user_id(db)
    assert audits(session_factory, txn_id) == [("CREATE", None), ("DELETE", uid), ("RESTORE", uid)]
    html = client.get(f"/txn/{txn_id}").text
    assert 'data-state="deleted"' not in html
    assert f'data-txn-id="{txn_id}"' in client.get("/txn").text


def test_restore_active_transaction_is_422(client, db):
    txn_id = expense(db)
    resp = post(client, f"/txn/{txn_id}/restore")
    assert resp.status_code == 422
    assert banner_codes(resp.text) == ["TXN_NOT_DELETED"]


def test_restore_blocked_by_invoice_collision(client, db, session_factory):
    first = expense(db, invoice_no="AB12345678")
    post(client, f"/txn/{first}/delete")
    expense(db, 99, invoice_no="AB12345678")
    resp = post(client, f"/txn/{first}/restore")
    assert resp.status_code == 422
    assert banner_codes(resp.text) == ["V08"]
    assert 'data-state="deleted"' in resp.text
    assert state(session_factory, first) is not None


def test_restore_refund_blocked_when_original_deleted(client, db):
    original = expense(db, 1000)
    refund = transactions.record_refund(db, RefundForm(original, TODAY, 600, pm(db))).transaction_id
    post(client, f"/txn/{refund}/delete")
    post(client, f"/txn/{original}/delete")
    resp = post(client, f"/txn/{refund}/restore")
    assert resp.status_code == 422
    assert banner_codes(resp.text) == ["REFUND_ORIGINAL"]


def test_restore_refund_blocked_by_v07(client, db):
    original = expense(db, 1000)
    refund = transactions.record_refund(db, RefundForm(original, TODAY, 600, pm(db))).transaction_id
    post(client, f"/txn/{refund}/delete")
    transactions.record_refund(db, RefundForm(original, TODAY, 500, pm(db)))
    resp = post(client, f"/txn/{refund}/restore")
    assert resp.status_code == 422
    assert banner_codes(resp.text) == ["V07"]


def test_restore_opening_blocked_by_d12(client, db):
    def opening(amount):
        form = OpeningForm(TODAY, (OpeningLine(acc(db, "1101"), amount),))
        return transactions.record_opening(db, form).transaction_id

    first = opening(1000)
    post(client, f"/txn/{first}/delete")
    opening(2000)
    resp = post(client, f"/txn/{first}/restore")
    assert resp.status_code == 422
    assert banner_codes(resp.text) == ["OPENING_EXISTS"]


# --- 原子性 ------------------------------------------------------------------------------


def test_delete_and_restore_are_atomic(client, db, session_factory, monkeypatch):
    txn_id = expense(db)

    def boom(*args, **kwargs):
        raise RuntimeError("audit write failed")

    monkeypatch.setattr(audit, "record", boom)
    resp = post(client, f"/txn/{txn_id}/delete")
    assert resp.status_code == 500
    assert state(session_factory, txn_id) is None

    monkeypatch.undo()
    post(client, f"/txn/{txn_id}/delete")
    monkeypatch.setattr(audit, "record", boom)
    resp = post(client, f"/txn/{txn_id}/restore")
    assert resp.status_code == 500
    assert state(session_factory, txn_id) is not None
    with session_factory() as s:
        assert s.scalar(select(func.count()).select_from(AuditLog)) == 2  # CREATE、DELETE


# --- 還原後的提醒（V13） ---------------------------------------------------------------------


def test_restore_shows_v13_notice(client, db, session_factory):
    """還原後現金餘額變負：還原成功，明細顯示 V13 提醒（P1 回傳的 notices 不被吞掉）。"""
    post_manual(db, TODAY, ("1101", 1000), ("4201", -1000))
    lunch = expense(db, 120, payment_method_id=pm(db, "現金"))  # 現金 880
    post(client, f"/txn/{lunch}/delete")  # 現金 1,000
    expense(db, 1000, payment_method_id=pm(db, "現金"))  # 現金 0
    with session_factory() as s:
        audits_before = s.scalar(select(func.count()).select_from(AuditLog))

    resp = post(client, f"/txn/{lunch}/restore")
    assert resp.status_code == 200
    notice = re.search(r'<p class="notice notice-warn" data-code="V13">([^<]+)</p>', resp.text)
    assert notice, "還原後應顯示 V13 提醒"
    assert "現金餘額變成 −120" in notice.group(1)
    assert state(session_factory, lunch) is None
    with session_factory() as s:
        assert s.scalar(select(func.count()).select_from(AuditLog)) == audits_before + 1
        assert s.scalar(select(func.count()).select_from(Transaction)) == 3
    assert audits(session_factory, lunch)[-1] == ("RESTORE", user_id(db))
