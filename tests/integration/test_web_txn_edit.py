# ruff: noqa: F811  （共用 test_web_txn_new.py 的 fixture：pytest 依參數名稱注入，不是重複定義）
"""GET/POST /txn/{id}/edit：只修改快速記帳格式的支出（P2 Step 4A，Web 層）。

帳務規則（V08、V10–V12、REFUND_LOCKED、TXN_DELETED）全部由 P1 判斷；
Web 只多一個「是否為快速記帳格式的支出」的檢查。
"""

import re
from datetime import date

from sqlalchemy import func, select

from app.models import AuditLog, Merchant, PaymentMethod, Transaction
from app.services import audit, transactions
from app.services.entries import ExpenseForm, RefundForm
from tests.integration.test_web_txn_new import (  # noqa: F401
    HX,
    TOKEN_INPUT,
    _today,
    anon,
    client,
    confirm_codes,
    csrf_from,
    db,
    field_error_codes,
    hidden,
    input_value,
    log_dir,
    selected_value,
    session_factory,
    user_id,
)
from tests.ledger_helpers import TODAY, acc, post_manual


def pm(db, name="LINE Pay") -> int:
    return db.scalar(select(PaymentMethod.id).where(PaymentMethod.name == name))


def expense(db, amount=120, day=TODAY, **kwargs) -> int:
    form = ExpenseForm(
        txn_date=day,
        amount=amount,
        account_id=acc(db, "5101"),
        **{"payment_method_id": pm(db), **kwargs},
    )
    return transactions.record_expense(db, form, confirmed=["V10", "V11", "V12"]).transaction_id


def edit(client, db, txn_id, **overrides):
    """以「目前的值」送出修改表單；overrides 覆寫欄位。"""
    html = client.get(f"/txn/{txn_id}/edit").text
    data = {
        "csrf_token": csrf_from(client, "/txn/new"),
        "txn_date": input_value(html, "txn_date"),
        "amount": input_value(html, "amount"),
        "account_id": selected_value(html, "account_id") or "",
        "payment_method_id": selected_value(html, "payment_method_id") or "",
        "description": input_value(html, "description"),
        "merchant_name": input_value(html, "merchant_name"),
        "voucher_type": selected_value(html, "voucher_type") or "",
        "invoice_no": input_value(html, "invoice_no"),
        "action": "save",
    }
    data.update({k: str(v) for k, v in overrides.items()})
    return client.post(f"/txn/{txn_id}/edit", data=data, headers=HX)


def snapshot(session_factory, txn_id):
    with session_factory() as s:
        t = s.get(Transaction, txn_id)
        return (
            t.txn_date,
            t.merchant_id,
            t.description,
            t.invoice_no,
            t.payment_method_id,
            [(p.line_no, p.account_id, p.amount, p.line_role) for p in t.postings],
        )


def audit_actions(session_factory, txn_id) -> list[tuple[str, int | None]]:
    with session_factory() as s:
        rows = s.scalars(
            select(AuditLog).where(AuditLog.entity_id == txn_id).order_by(AuditLog.id)
        ).all()
        return [(r.action, r.user_id) for r in rows]


def merchant_count(session_factory) -> int:
    with session_factory() as s:
        return s.scalar(select(func.count()).select_from(Merchant))


# --- 表單 -------------------------------------------------------------------------------


def test_edit_form_prefilled_without_submission_token(client, db):
    m = Merchant(name="7-11")
    db.add(m)
    db.commit()
    txn_id = expense(db, merchant_id=m.id, description="午餐", invoice_no="AB12345678")
    resp = client.get(f"/txn/{txn_id}/edit")
    assert resp.status_code == 200
    html = resp.text
    assert input_value(html, "amount") == "120"
    assert input_value(html, "merchant_name") == "7-11"
    assert input_value(html, "description") == "午餐"
    assert input_value(html, "invoice_no") == "AB12345678"
    assert selected_value(html, "account_id") == str(acc(db, "5101"))
    assert f'hx-post="/txn/{txn_id}/edit"' in html
    assert TOKEN_INPUT.search(html) is None
    assert "儲存修改" in html and "儲存並記下一筆" not in html


def test_new_entry_still_requires_submission_token(client, db, session_factory):
    """修改不使用權杖，但 POST /txn/new 不帶權杖仍然 422（Step 2 規則不變）。"""
    resp = client.post(
        "/txn/new",
        data={
            "csrf_token": csrf_from(client, "/txn/new"),
            "txn_date": TODAY.isoformat(),
            "amount": "120",
            "account_id": str(acc(db, "5101")),
            "payment_method_id": str(pm(db)),
        },
        headers=HX,
    )
    assert resp.status_code == 422
    assert "表單已失效，請重新整理後再試。" in resp.text
    with session_factory() as s:
        assert s.scalar(select(func.count()).select_from(Transaction)) == 0


# --- 成功修改 ----------------------------------------------------------------------------


def test_edit_amount_date_merchant_description_invoice(client, db, session_factory):
    txn_id = expense(db)
    resp = edit(
        client,
        db,
        txn_id,
        amount=150,
        txn_date="2026-10-14",
        merchant_name="7-11",
        description="晚餐",
        voucher_type="EINVOICE",
        invoice_no="ab-12345678",
    )
    assert resp.status_code == 200
    # 不導頁：直接回傳明細主體，換掉整個 main 並把網址改成明細（才能顯示 P1 的提醒）
    assert resp.headers["HX-Retarget"] == "main.container"
    assert resp.headers["HX-Reswap"] == "innerHTML"
    assert resp.headers["HX-Push-Url"] == f"/txn/{txn_id}"
    assert "已儲存修改。" in resp.text
    assert "<title>交易明細｜記帳</title>" in resp.text
    assert '<div id="txn-detail">' in resp.text
    date_, merchant_id, desc, invoice, _, postings = snapshot(session_factory, txn_id)
    assert (date_, desc, invoice) == ("2026-10-14", "晚餐", "AB12345678")
    assert merchant_id is not None
    assert [(a, amt, role) for _, a, amt, role in postings] == [
        (acc(db, "5101"), 150, "MAIN"),
        (acc(db, "1102"), -150, None),
    ]
    assert audit_actions(session_factory, txn_id) == [("CREATE", None), ("UPDATE", user_id(db))]


def test_unchanged_edit_keeps_postings_intact(client, db, session_factory):
    txn_id = expense(db, description="午餐")
    before = snapshot(session_factory, txn_id)
    resp = edit(client, db, txn_id)
    assert resp.status_code == 200
    assert snapshot(session_factory, txn_id) == before


def test_change_payment_method(client, db, session_factory):
    txn_id = expense(db)
    edit(client, db, txn_id, payment_method_id=pm(db, "現金"))
    postings = snapshot(session_factory, txn_id)[5]
    assert postings[1][1] == acc(db, "1101")


# --- 不能修改的交易 ------------------------------------------------------------------------


def test_foreign_currency_expense_cannot_be_edited(client, db, session_factory):
    """手續費、利息、不計入預算在 Step 6 開放修改；外幣（V1.5）仍不開放。"""
    txn_id = expense(db, original_currency="USD", original_amount_minor=399)
    before = snapshot(session_factory, txn_id)

    detail = client.get(f"/txn/{txn_id}").text
    assert f'href="/txn/{txn_id}/edit"' not in detail
    assert 'data-blocker="edit"' in detail

    page = client.get(f"/txn/{txn_id}/edit")
    assert page.status_code == 303
    assert page.headers["location"] == f"/txn/{txn_id}"

    resp = client.post(
        f"/txn/{txn_id}/edit",
        data={
            "csrf_token": csrf_from(client, "/txn/new"),
            "txn_date": TODAY.isoformat(),
            "amount": "999",
            "account_id": str(acc(db, "5101")),
            "payment_method_id": str(pm(db)),
            "merchant_name": "不該被建立的店",
            "action": "save",
        },
        headers=HX,
    )
    assert resp.status_code == 422
    assert 'data-code="NOT_EDITABLE"' in resp.text
    assert snapshot(session_factory, txn_id) == before
    assert merchant_count(session_factory) == 0
    assert audit_actions(session_factory, txn_id) == [("CREATE", None)]


def test_other_types_and_deleted_cannot_be_edited(client, db, session_factory):
    # 收入、轉帳、繳卡費在 Step 5B 開放修改；退款等其他類型仍不提供
    refund = transactions.record_refund(
        db, RefundForm(expense(db, 1000), TODAY, 600, pm(db))
    ).transaction_id
    deleted = expense(db)
    transactions.delete_transaction(db, deleted)
    for txn_id in (refund, deleted):
        resp = client.post(
            f"/txn/{txn_id}/edit",
            data={"csrf_token": csrf_from(client, "/txn/new"), "amount": "1"},
            headers=HX,
        )
        assert resp.status_code == 422
        assert 'data-code="NOT_EDITABLE"' in resp.text


# --- P1 規則經過 Web 仍然有效 --------------------------------------------------------------


def test_parse_error_is_422(client, db):
    txn_id = expense(db)
    resp = edit(client, db, txn_id, amount="abc")
    assert resp.status_code == 422
    assert field_error_codes(resp.text, "amount") == [None]


def test_v10_only_when_amount_changes(client, db, session_factory):
    big = expense(db, 12000)
    assert edit(client, db, big, description="改備註").status_code == 200  # 金額沒變：不重問

    small = expense(db, 100)
    resp = edit(client, db, small, amount=20000)
    assert resp.status_code == 409
    assert confirm_codes(resp.text) == ["V10"]
    assert f'hx-post="/txn/{small}/edit"' in resp.text
    assert snapshot(session_factory, small)[5][0][2] == 100
    resp = edit(client, db, small, amount=20000, confirmed=hidden(resp.text, "confirmed"))
    assert resp.status_code == 200
    assert snapshot(session_factory, small)[5][0][2] == 20000


def test_v11_when_date_moves_to_future(client, db):
    txn_id = expense(db)
    resp = edit(client, db, txn_id, txn_date="2026-10-20")
    assert resp.status_code == 409
    assert confirm_codes(resp.text) == ["V11"]


def test_v12_only_when_key_fields_change(client, db):
    m = Merchant(name="7-11")
    db.add(m)
    db.commit()
    expense(db, 85, merchant_id=m.id)
    other = expense(db, 90, merchant_id=m.id)
    assert edit(client, db, other, description="只改備註").status_code == 200
    resp = edit(client, db, other, amount=85)
    assert resp.status_code == 409
    assert confirm_codes(resp.text) == ["V12"]


def test_v08_excludes_self_and_blocks_other(client, db, session_factory):
    mine = expense(db, invoice_no="AB12345678")
    expense(db, 99, invoice_no="CD12345678")
    assert edit(client, db, mine, description="保留自己的發票").status_code == 200
    resp = edit(client, db, mine, invoice_no="CD12345678")
    assert resp.status_code == 422
    assert field_error_codes(resp.text, "invoice_no") == ["V08"]
    assert snapshot(session_factory, mine)[3] == "AB12345678"


def test_refund_locked_fields(client, db, session_factory):
    original = expense(db, 1000, day=date(2026, 10, 10))
    transactions.record_refund(db, RefundForm(original, date(2026, 10, 12), 600, pm(db)))
    before = snapshot(session_factory, original)

    resp = edit(client, db, original, amount=500)
    assert field_error_codes(resp.text, "amount") == ["REFUND_LOCKED"]
    resp = edit(client, db, original, account_id=acc(db, "5102"))
    assert field_error_codes(resp.text, "account_id") == ["REFUND_LOCKED"]
    resp = edit(client, db, original, txn_date="2026-10-13")
    assert field_error_codes(resp.text, "txn_date") == ["REFUND_LOCKED"]
    assert snapshot(session_factory, original) == before

    # 商家、備註仍可修改（U10）
    assert edit(client, db, original, description="部分退貨").status_code == 200


def test_edit_is_atomic_when_audit_fails(client, db, session_factory, monkeypatch):
    txn_id = expense(db)
    before = snapshot(session_factory, txn_id)

    def boom(*args, **kwargs):
        raise RuntimeError("audit write failed")

    monkeypatch.setattr(audit, "record", boom)
    resp = edit(client, db, txn_id, amount=300, description="不該留下")
    assert resp.status_code == 500
    assert snapshot(session_factory, txn_id) == before


def test_edit_shows_v13_notice(client, db, session_factory):
    """修改後現金餘額變負：修改成功，回應的明細顯示 V13 提醒。"""
    post_manual(db, TODAY, ("1101", 1000), ("4201", -1000))
    txn_id = expense(db, 100, payment_method_id=pm(db, "現金"))
    resp = edit(client, db, txn_id, amount=1200)
    assert resp.status_code == 200
    notice = re.search(r'<p class="notice notice-warn" data-code="V13">([^<]+)</p>', resp.text)
    assert notice, "修改後應顯示 V13 提醒"
    assert "現金餘額變成 −200" in notice.group(1)
    assert snapshot(session_factory, txn_id)[5][0][2] == 1200
    assert audit_actions(session_factory, txn_id) == [("CREATE", None), ("UPDATE", user_id(db))]


def test_edit_without_notice_shows_no_warning(client, db):
    txn_id = expense(db)
    post_manual(db, TODAY, ("1102", 5000), ("4201", -5000))
    resp = edit(client, db, txn_id, description="郵局餘額足夠")
    assert resp.status_code == 200
    assert "notice-warn" not in resp.text
