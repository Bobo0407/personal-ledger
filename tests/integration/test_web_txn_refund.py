# ruff: noqa: F811  （共用 test_web_txn_new.py 的 fixture：pytest 依參數名稱注入，不是重複定義）
"""GET/POST /txn/{id}/refund：退款（P2 Step 7，UF05，Web 層）。

退款規則（V07、REFUND_DATE、REFUND_ORIGINAL、只沖 MAIN、從原交易帶入科目與商家）全部由 P1 判斷；
這裡確認經過 Web 之後：欄位接得上、預填剩餘可退、權杖、422／409、回到原交易明細與連結。
"""

import re
from datetime import date, timedelta

from sqlalchemy import func, select

from app.models import AuditLog, Merchant, PaymentMethod, Transaction
from app.services import transactions
from app.services.entries import ExpenseForm, IncomeForm
from tests.integration.test_web_txn_new import (  # noqa: F401
    HX,
    TOKEN_INPUT,
    _today,
    anon,
    banner_codes,
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
from tests.ledger_helpers import TODAY, acc


def pm(db, name="LINE Pay") -> int:
    return db.scalar(select(PaymentMethod.id).where(PaymentMethod.name == name))


def expense(db, amount=1000, day=TODAY, **kwargs) -> int:
    form = ExpenseForm(day, amount, acc(db, "5201"), kwargs.pop("pm_id", pm(db)), **kwargs)
    return transactions.record_expense(db, form, confirmed=["V10", "V11"]).transaction_id


def page(client, txn_id) -> str:
    resp = client.get(f"/txn/{txn_id}/refund")
    assert resp.status_code == 200
    return resp.text


def post_refund(client, txn_id, *, token=None, from_page=None, **overrides):
    """以退款頁的預填值送出；overrides 覆寫欄位。"""
    html = page(client, from_page or txn_id)
    data = {
        "csrf_token": csrf_from(client, "/txn/new"),
        "submission_token": token or TOKEN_INPUT.search(html).group(1),
        "txn_date": input_value(html, "txn_date"),
        "amount": input_value(html, "amount"),
        "payment_method_id": selected_value(html, "payment_method_id") or "",
        "description": "",
        "action": "save",
    }
    data.update({k: str(v) for k, v in overrides.items()})
    return client.post(f"/txn/{txn_id}/refund", data=data, headers=HX)


def refunds_of(session_factory, original) -> list[Transaction]:
    with session_factory() as s:
        rows = s.scalars(
            select(Transaction)
            .where(Transaction.related_txn_id == original)
            .order_by(Transaction.id)
        ).all()
        for t in rows:
            t.postings  # noqa: B018
        return rows


def txn_count(session_factory) -> int:
    with session_factory() as s:
        return s.scalar(select(func.count()).select_from(Transaction))


def audit_count(session_factory) -> int:
    with session_factory() as s:
        return s.scalar(select(func.count()).select_from(AuditLog))


def expense_net(client) -> int:
    html = client.get("/txn", params={"start": "2026-01-01", "end": "2026-12-31"}).text
    return int(re.search(r'data-expense-net="(-?\d+)"', html).group(1))


def remaining(html) -> int:
    return int(re.search(r'data-remaining="(-?\d+)"', html).group(1))


# --- 入口與表單 --------------------------------------------------------------------------


def test_detail_shows_refund_button_and_remaining(client, db):
    original = expense(db)
    html = client.get(f"/txn/{original}").text
    assert f'href="/txn/{original}/refund"' in html
    assert remaining(html) == 1000


def test_refund_page_prefills(client, db):
    original = expense(db, merchant_id=None)
    html = page(client, original)
    assert "<h1>退款</h1>" in html
    assert input_value(html, "amount") == "1000"
    assert input_value(html, "txn_date") == TODAY.isoformat()
    assert re.search(rf'<input id="txn_date"[^>]*min="{TODAY.isoformat()}"', html)
    assert selected_value(html, "payment_method_id") == str(pm(db))
    assert len(TOKEN_INPUT.findall(html)) == 1
    for name in ("account_id", "merchant_name", "original_txn_id", "invoice_no"):
        assert f'name="{name}"' not in html


def test_inactive_original_payment_method_not_preselected(client, db, session_factory):
    original = expense(db, pm_id=pm(db, "台灣Pay"))
    with session_factory() as s:
        s.get(PaymentMethod, pm(s, "台灣Pay")).is_active = False
        s.commit()
    html = page(client, original)
    assert selected_value(html, "payment_method_id") is None
    assert "台灣Pay" not in html


# --- 退款 ------------------------------------------------------------------------------


def test_full_refund_returns_to_original_detail(client, db, session_factory):
    original = expense(db)
    resp = post_refund(client, original)
    assert resp.status_code == 200
    assert resp.headers["HX-Push-Url"] == f"/txn/{original}"
    assert "已退款 1,000 元。" in resp.text
    [r] = refunds_of(session_factory, original)
    assert r.txn_type == "REFUND" and r.source_ref.startswith("submit:")
    assert f'href="/txn/{r.id}"' in resp.text  # 退款連結
    assert 'data-blocker="refund">已全額退款' in resp.text
    assert remaining(resp.text) == 0
    assert expense_net(client) == 0
    with session_factory() as s:
        log = s.scalars(select(AuditLog).where(AuditLog.entity_id == r.id)).one()
        assert log.user_id == user_id(db)
    # 退款明細：原交易連結、類型、退款方式
    detail = client.get(f"/txn/{r.id}").text
    assert f'href="/txn/{original}"' in detail and ">退款</span>" in detail and "LINE Pay" in detail
    assert "這類交易目前不提供修改" in detail


def test_partial_then_second_partial_then_over_refund(client, db, session_factory):
    original = expense(db)
    assert post_refund(client, original, amount=300).status_code == 200
    assert input_value(page(client, original), "amount") == "700"  # 預填剩餘可退
    assert post_refund(client, original, amount=200).status_code == 200
    resp = post_refund(client, original, amount=600)
    assert resp.status_code == 422
    assert field_error_codes(resp.text, "amount") == ["V07"]
    assert input_value(resp.text, "amount") == "600"
    assert len(refunds_of(session_factory, original)) == 2
    assert expense_net(client) == 500


def test_refund_only_main_fee_and_interest_stay(client, db, session_factory):
    """T34：1,000 + 手續費 15，退 1,000 後支出淨額剩 15；分期利息同理。"""
    fee = expense(db, fee_amount=15, fee_account_id=acc(db, "5502"))
    assert input_value(page(client, fee), "amount") == "1000"
    post_refund(client, fee)
    assert expense_net(client) == 15
    card = pm(db, "聯邦信用卡")
    interest = expense(db, pm_id=card, interest_amount=600, interest_account_id=acc(db, "5503"))
    post_refund(client, interest)
    assert expense_net(client) == 15 + 600
    [r] = refunds_of(session_factory, interest)
    assert sorted((p.account_id, p.amount, p.line_role) for p in r.postings) == sorted(
        [(acc(db, "5201"), -1000, "MAIN"), (acc(db, "2101"), 1000, None)]
    )


def test_foreign_currency_original_refunds_twd_only(client, db, session_factory):
    original = expense(db, original_currency="JPY", original_amount_minor=4800)
    assert post_refund(client, original).status_code == 200
    [r] = refunds_of(session_factory, original)
    assert all(p.original_currency is None for p in r.postings)


def test_refund_to_different_payment_method(client, db, session_factory):
    original = expense(db, pm_id=pm(db, "聯邦信用卡"))
    post_refund(client, original, payment_method_id=pm(db, "現金"))
    [r] = refunds_of(session_factory, original)
    assert (acc(db, "1101"), 1000, None) in [
        (p.account_id, p.amount, p.line_role) for p in r.postings
    ]


def test_refund_date_before_original_is_p1_error(client, db, session_factory):
    original = expense(db)
    resp = post_refund(client, original, txn_date=(TODAY - timedelta(days=1)).isoformat())
    assert resp.status_code == 422
    assert field_error_codes(resp.text, "txn_date") == ["REFUND_DATE"]
    assert refunds_of(session_factory, original) == []


def test_cross_month_refund(client, db, session_factory):
    original = expense(db, day=date(2026, 9, 20))
    assert post_refund(client, original).status_code == 200
    assert refunds_of(session_factory, original)[0].txn_date == TODAY.isoformat()


def test_deleted_or_non_expense_original(client, db, session_factory):
    deleted = expense(db)
    transactions.delete_transaction(db, deleted)
    assert client.get(f"/txn/{deleted}/refund").status_code == 303
    resp = post_refund(client, deleted, from_page=expense(db))
    assert resp.status_code == 422 and banner_codes(resp.text) == ["REFUND_ORIGINAL"]
    income = transactions.record_income(
        db, IncomeForm(TODAY, 100, acc(db, "4201"), acc(db, "1102"))
    ).transaction_id
    assert client.get(f"/txn/{income}/refund").status_code == 303
    assert 'href="/txn/{}/refund"'.format(income) not in client.get(f"/txn/{income}").text
    resp = post_refund(client, income, from_page=expense(db))
    assert resp.status_code == 422 and banner_codes(resp.text) == ["REFUND_ORIGINAL"]
    assert txn_count(session_factory) == 4  # 沒有新增退款


def test_fully_refunded_has_no_button_and_page_redirects(client, db):
    original = expense(db)
    post_refund(client, original)
    assert f'href="/txn/{original}/refund"' not in client.get(f"/txn/{original}").text
    assert client.get(f"/txn/{original}/refund").status_code == 303


def test_v10_v11_confirm_keep_fields_v12_not_triggered(client, db, session_factory):
    m = Merchant(name="家樂福")
    db.add(m)
    db.commit()
    original = expense(db, amount=20000, merchant_id=m.id)
    html = page(client, original)
    token = TOKEN_INPUT.search(html).group(1)
    resp = post_refund(client, original, token=token, description="退貨")
    assert resp.status_code == 409 and confirm_codes(resp.text) == ["V10"]
    for name, value in (
        ("amount", "20000"),
        ("payment_method_id", str(pm(db))),
        ("description", "退貨"),
        ("submission_token", token),
    ):
        assert hidden(resp.text, name) == value
    assert f'hx-post="/txn/{original}/refund"' in resp.text
    back = post_refund(client, original, token=token, description="退貨", action="edit")
    assert input_value(back.text, "description") == "退貨"
    assert TOKEN_INPUT.findall(back.text) == [token]
    future = (TODAY + timedelta(days=2)).isoformat()
    resp = post_refund(client, original, token=token, amount=100, txn_date=future)
    assert resp.status_code == 409 and confirm_codes(resp.text) == ["V11"]  # 沒有 V12
    ok = post_refund(client, original, token=token, amount=100, confirmed="V10")
    assert ok.status_code == 200
    assert len(refunds_of(session_factory, original)) == 1


# --- 權杖 ------------------------------------------------------------------------------


def test_web_refund_requires_token(client, db, session_factory):
    original = expense(db)
    resp = post_refund(client, original, submission_token="")
    assert resp.status_code == 422
    assert refunds_of(session_factory, original) == []
    [new] = TOKEN_INPUT.findall(resp.text)
    assert new


def test_replay_creates_nothing(client, db, session_factory):
    original = expense(db)
    token = TOKEN_INPUT.search(page(client, original)).group(1)
    post_refund(client, original, token=token, amount=300)
    before = audit_count(session_factory)
    again = post_refund(client, original, token=token, amount=300)
    assert again.status_code == 200 and "沒有重複建立" in again.text
    assert len(refunds_of(session_factory, original)) == 1
    assert audit_count(session_factory) == before


def test_same_token_against_another_original_replays_first(client, db, session_factory):
    first = expense(db)
    second = expense(db)
    token = TOKEN_INPUT.search(page(client, first)).group(1)
    post_refund(client, first, token=token, amount=300)
    before = audit_count(session_factory)
    resp = post_refund(client, second, token=token, amount=300, from_page=second)
    assert resp.status_code == 200
    assert resp.headers["HX-Push-Url"] == f"/txn/{first}"  # 對應第一次退款的原交易
    assert "沒有重複建立" in resp.text
    assert refunds_of(session_factory, second) == []
    assert len(refunds_of(session_factory, first)) == 1
    assert audit_count(session_factory) == before


# --- 刪除與還原（沿用 P1、4A） ----------------------------------------------------------


def test_delete_restore_lifecycle(client, db, session_factory):
    original = expense(db)
    post_refund(client, original, amount=600)
    [r] = refunds_of(session_factory, original)
    html = client.get(f"/txn/{original}").text
    assert 'data-blocker="delete"' in html  # REFUND_EXISTS
    csrf = csrf_from(client, "/txn/new")
    client.post(f"/txn/{r.id}/delete", data={"csrf_token": csrf}, headers=HX)
    html = client.get(f"/txn/{original}").text
    assert remaining(html) == 1000 and f'href="/txn/{r.id}"' not in html
    client.post(f"/txn/{r.id}/restore", data={"csrf_token": csrf}, headers=HX)
    assert remaining(client.get(f"/txn/{original}").text) == 400
    # 刪除後另退 700，再還原 600 → 超額，P1 V07 擋下
    client.post(f"/txn/{r.id}/delete", data={"csrf_token": csrf}, headers=HX)
    post_refund(client, original, amount=700)
    resp = client.post(f"/txn/{r.id}/restore", data={"csrf_token": csrf}, headers=HX)
    assert resp.status_code == 422 and 'data-code="V07"' in resp.text
    assert remaining(client.get(f"/txn/{original}").text) == 300


def test_refund_cannot_be_edited(client, db, session_factory):
    original = expense(db)
    post_refund(client, original, amount=100)
    [r] = refunds_of(session_factory, original)
    assert client.get(f"/txn/{r.id}/edit").status_code == 303
