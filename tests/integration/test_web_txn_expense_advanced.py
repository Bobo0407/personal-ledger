# ruff: noqa: F811  （共用 test_web_txn_new.py 的 fixture：pytest 依參數名稱注入，不是重複定義）
"""支出的進階欄位：手續費、分期利息、不計入預算（P2 Step 6，Web 層）。

帳務（分錄、交易規模、V10、V12、P12、REFUND_LOCKED）全部由 P1 判斷；
這裡確認經過 Web 之後：欄位接得上、422／409 保留進階輸入、儲存並記下一筆清空、
修改用「還原 → 重建 → 比對」的條件。外幣延後（V1.5），不在表單上。
"""

import re

import pytest
from sqlalchemy import func, select

from app.models import AuditLog, Merchant, PaymentMethod, Transaction
from app.services import transactions
from app.services.entries import ExpenseForm, RefundForm
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
    fresh_tokens,
    hidden,
    input_value,
    log_dir,
    selected_value,
    session_factory,
    submit,
    user_id,
)
from tests.ledger_helpers import TODAY, acc

ADVANCED_OPEN = re.compile(r'<details class="voucher advanced" open>')


def pm(db, name="LINE Pay") -> int:
    return db.scalar(select(PaymentMethod.id).where(PaymentMethod.name == name))


def txns(session_factory) -> list[Transaction]:
    with session_factory() as s:
        rows = s.scalars(select(Transaction).order_by(Transaction.id)).all()
        for t in rows:
            t.postings  # noqa: B018
        return rows


def postings(session_factory, txn_id) -> list[tuple]:
    with session_factory() as s:
        t = s.get(Transaction, txn_id)
        return sorted((p.account_id, p.amount, p.line_role or "") for p in t.postings)


def audits(session_factory, txn_id) -> list[tuple[str, int | None]]:
    with session_factory() as s:
        rows = s.scalars(
            select(AuditLog).where(AuditLog.entity_id == txn_id).order_by(AuditLog.id)
        ).all()
        return [(r.action, r.user_id) for r in rows]


def summary(client) -> tuple[str, str]:
    html = client.get("/txn").text
    m = re.search(r'data-expense-net="(-?\d+)" data-income-total="(-?\d+)"', html)
    return m.group(1), m.group(2)


# --- 新增 ------------------------------------------------------------------------------


def test_form_has_collapsed_advanced_section(client):
    html = client.get("/txn/new").text
    assert '<details class="voucher advanced">' in html
    assert "分期利息（分期付款才填）" in html and "不計入預算" in html
    for name in ("fee_amount", "fee_account_id", "interest_amount", "interest_account_id"):
        assert f'id="{name}"' in html
    assert 'name="original_currency"' not in html  # 外幣延後


def test_create_with_fee(client, db, session_factory):
    resp = submit(
        client,
        db,
        amount=1000,
        account_id=acc(db, "5301"),
        payment_method_id=pm(db, "聯邦信用卡"),
        fee_amount=15,
        fee_account_id=acc(db, "5502"),
    )
    assert resp.status_code == 200
    [txn] = txns(session_factory)
    assert postings(session_factory, txn.id) == sorted(
        [
            (acc(db, "5301"), 1000, "MAIN"),
            (acc(db, "5502"), 15, "FEE"),
            (acc(db, "2101"), -1015, ""),
        ]
    )
    detail = client.get(f"/txn/{txn.id}").text
    assert "1,015" in detail and ">手續費</span>" in detail
    assert summary(client)[0] == "1015"


def test_create_with_interest_on_credit_card(client, db, session_factory):
    resp = submit(
        client,
        db,
        amount=30000,
        account_id=acc(db, "5201"),
        payment_method_id=pm(db, "聯邦信用卡"),
        interest_amount=600,
        interest_account_id=acc(db, "5503"),
        confirmed="V10",
    )
    assert resp.status_code == 200
    [txn] = txns(session_factory)
    assert (acc(db, "5503"), 600, "INTEREST") in postings(session_factory, txn.id)
    assert (acc(db, "2101"), -30600, "") in postings(session_factory, txn.id)
    assert ">分期利息</span>" in client.get(f"/txn/{txn.id}").text
    assert summary(client)[0] == "30600"


def test_create_with_fee_and_interest(client, db, session_factory):
    resp = submit(
        client,
        db,
        amount=1000,
        payment_method_id=pm(db, "聯邦信用卡"),
        fee_amount=15,
        fee_account_id=acc(db, "5502"),
        interest_amount=60,
        interest_account_id=acc(db, "5503"),
    )
    assert resp.status_code == 200
    [txn] = txns(session_factory)
    assert len(txn.postings) == 4
    assert summary(client)[0] == "1075"


def test_interest_with_non_credit_card_is_structure_error(client, db, session_factory):
    resp = submit(client, db, interest_amount=60, interest_account_id=acc(db, "5503"))
    assert resp.status_code == 422
    assert banner_codes(resp.text) == ["TXN_STRUCTURE"]
    assert ADVANCED_OPEN.search(resp.text)  # 進階欄位有值 → 展開
    assert txns(session_factory) == []


@pytest.mark.parametrize("prefix", ["fee", "interest"])
def test_amount_without_account_is_v05(client, db, session_factory, prefix):
    resp = submit(client, db, payment_method_id=pm(db, "聯邦信用卡"), **{f"{prefix}_amount": 15})
    assert resp.status_code == 422
    assert field_error_codes(resp.text, f"{prefix}_account_id") == ["V05"]
    assert ADVANCED_OPEN.search(resp.text)
    assert input_value(resp.text, f"{prefix}_amount") == "15"


def test_exclude_from_budget_create_and_detail(client, db, session_factory):
    resp = submit(client, db, amount=500, exclude_from_budget="1")
    assert resp.status_code == 200
    [txn] = txns(session_factory)
    assert txn.exclude_from_budget is True
    assert 'data-flag="exclude-from-budget">是<' in client.get(f"/txn/{txn.id}").text
    assert summary(client)[0] == "500"  # 只是預算旗標，不影響支出合計
    submit(client, db, amount=80)
    other = txns(session_factory)[1]
    assert other.exclude_from_budget is False
    assert "不計入預算" not in client.get(f"/txn/{other.id}").text.split("<h2")[0]


def test_parse_error_keeps_advanced_input_and_token(client, db):
    csrf, token = fresh_tokens(client)
    resp = submit(
        client,
        db,
        fee_amount="1.5",
        fee_account_id=acc(db, "5502"),
        exclude_from_budget="1",
        submission_token=token,
    )
    assert resp.status_code == 422
    assert field_error_codes(resp.text, "fee_amount") == [None]
    assert ADVANCED_OPEN.search(resp.text)
    assert input_value(resp.text, "fee_amount") == "1.5"
    assert selected_value(resp.text, "fee_account_id") == str(acc(db, "5502"))
    assert re.search(r'id="exclude_from_budget"[^>]*checked', resp.text)
    assert TOKEN_INPUT.findall(resp.text) == [token]


def test_v10_uses_full_magnitude_and_confirm_keeps_advanced(client, db, session_factory):
    """MAIN 9,900 + 手續費 150 = 10,050 > 10,000：V10 看完整交易規模。"""
    csrf, token = fresh_tokens(client)
    resp = submit(
        client,
        db,
        amount=9900,
        fee_amount=150,
        fee_account_id=acc(db, "5502"),
        exclude_from_budget="1",
        submission_token=token,
    )
    assert resp.status_code == 409 and confirm_codes(resp.text) == ["V10"]
    for name, value in (
        ("fee_amount", "150"),
        ("fee_account_id", str(acc(db, "5502"))),
        ("exclude_from_budget", "1"),
        ("submission_token", token),
    ):
        assert hidden(resp.text, name) == value
    back = submit(
        client,
        db,
        amount=9900,
        fee_amount=150,
        fee_account_id=acc(db, "5502"),
        exclude_from_budget="1",
        submission_token=token,
        action="edit",
    )
    assert input_value(back.text, "fee_amount") == "150" and ADVANCED_OPEN.search(back.text)
    assert txns(session_factory) == []


def test_v12_compares_main_only(client, db, session_factory):
    m = Merchant(name="7-11")
    db.add(m)
    db.commit()
    assert submit(client, db, merchant_name="7-11", amount=100).status_code == 200
    # 同 MAIN、不同手續費 → 仍問 V12
    resp = submit(
        client, db, merchant_name="7-11", amount=100, fee_amount=5, fee_account_id=acc(db, "5502")
    )
    assert resp.status_code == 409 and confirm_codes(resp.text) == ["V12"]
    # 不同 MAIN、總額相同（95 + 5）→ 不問
    resp = submit(
        client, db, merchant_name="7-11", amount=95, fee_amount=5, fee_account_id=acc(db, "5502")
    )
    assert resp.status_code == 200


def test_save_next_clears_advanced_and_collapses(client, db):
    resp = submit(
        client,
        db,
        payment_method_id=pm(db, "聯邦信用卡"),
        fee_amount=15,
        fee_account_id=acc(db, "5502"),
        interest_amount=60,
        interest_account_id=acc(db, "5503"),
        exclude_from_budget="1",
    )
    html = resp.text
    assert resp.status_code == 200
    for name in ("fee_amount", "interest_amount"):
        assert input_value(html, name) == ""
    for name in ("fee_account_id", "interest_account_id"):
        assert selected_value(html, name) is None
    assert not re.search(r'id="exclude_from_budget"[^>]*checked', html)
    assert not ADVANCED_OPEN.search(html)
    assert selected_value(html, "payment_method_id") == str(pm(db, "聯邦信用卡"))


def test_replay_does_not_duplicate_advanced_postings(client, db, session_factory):
    csrf, token = fresh_tokens(client)
    data = dict(fee_amount=15, fee_account_id=acc(db, "5502"), submission_token=token)
    submit(client, db, **data)
    again = submit(client, db, **data)
    assert 'data-replayed="true"' in again.text
    [txn] = txns(session_factory)
    assert len(txn.postings) == 3
    with session_factory() as s:
        assert s.scalar(select(func.count()).select_from(AuditLog)) == 1


def test_transfer_and_card_payment_semantics_unchanged(client, db):
    from tests.integration.test_web_txn_income_transfer import post

    post(client, db, "transfer", fee_amount="15", fee_account_id=acc(db, "5501"))
    post(client, db, "transfer", to_account_id=acc(db, "2101"))
    assert summary(client) == ("15", "0")


# --- 修改 ------------------------------------------------------------------------------


def record(db, **kwargs) -> int:
    base = {
        "txn_date": TODAY,
        "amount": 1000,
        "account_id": acc(db, "5301"),
        "payment_method_id": pm(db, "聯邦信用卡"),
    }
    form = ExpenseForm(**{**base, **kwargs})
    return transactions.record_expense(db, form).transaction_id


def edit(client, txn_id, **overrides):
    """以「目前的值」送出修改表單；overrides 覆寫欄位。"""
    html = client.get(f"/txn/{txn_id}/edit").text
    data = {"csrf_token": csrf_from(client, "/txn/new"), "action": "save"}
    for name in (
        "txn_date",
        "amount",
        "description",
        "merchant_name",
        "invoice_no",
        "fee_amount",
        "interest_amount",
    ):
        data[name] = input_value(html, name)
    for name in (
        "account_id",
        "payment_method_id",
        "voucher_type",
        "fee_account_id",
        "interest_account_id",
    ):
        data[name] = selected_value(html, name) or ""
    if re.search(r'id="exclude_from_budget"[^>]*checked', html):
        data["exclude_from_budget"] = "1"
    data.update({k: str(v) for k, v in overrides.items()})
    return client.post(f"/txn/{txn_id}/edit", data=data, headers=HX)


def ok(resp) -> bool:
    return resp.status_code == 200 and "已儲存修改。" in resp.text


def test_edit_prefills_advanced_and_opens_section(client, db):
    txn_id = record(
        db,
        fee_amount=15,
        fee_account_id=acc(db, "5502"),
        interest_amount=60,
        interest_account_id=acc(db, "5503"),
        exclude_from_budget=True,
    )
    html = client.get(f"/txn/{txn_id}/edit").text
    assert ADVANCED_OPEN.search(html)
    assert input_value(html, "fee_amount") == "15"
    assert selected_value(html, "fee_account_id") == str(acc(db, "5502"))
    assert input_value(html, "interest_amount") == "60"
    assert selected_value(html, "interest_account_id") == str(acc(db, "5503"))
    assert re.search(r'id="exclude_from_budget"[^>]*checked', html)
    assert TOKEN_INPUT.search(html) is None


def test_edit_fee_change_add_remove(client, db, session_factory):
    txn_id = record(db)
    fee = acc(db, "5502")
    assert ok(edit(client, txn_id, fee_amount=15, fee_account_id=fee))
    assert (fee, 15, "FEE") in postings(session_factory, txn_id)
    assert ok(edit(client, txn_id, fee_amount=30))
    assert (fee, 30, "FEE") in postings(session_factory, txn_id)
    assert ok(edit(client, txn_id, fee_amount="", fee_account_id=""))
    assert len(postings(session_factory, txn_id)) == 2
    assert [a for a, _ in audits(session_factory, txn_id)] == ["CREATE"] + ["UPDATE"] * 3
    assert audits(session_factory, txn_id)[-1] == ("UPDATE", user_id(db))


def test_edit_interest_change_add_remove(client, db, session_factory):
    txn_id = record(db)
    interest = acc(db, "5503")
    assert ok(edit(client, txn_id, interest_amount=600, interest_account_id=interest))
    assert (interest, 600, "INTEREST") in postings(session_factory, txn_id)
    assert ok(edit(client, txn_id, interest_amount=500))
    assert (interest, 500, "INTEREST") in postings(session_factory, txn_id)
    assert ok(edit(client, txn_id, interest_amount="", interest_account_id=""))
    assert len(postings(session_factory, txn_id)) == 2


def test_edit_fee_and_interest_together(client, db, session_factory):
    txn_id = record(
        db,
        fee_amount=15,
        fee_account_id=acc(db, "5502"),
        interest_amount=60,
        interest_account_id=acc(db, "5503"),
    )
    assert ok(edit(client, txn_id, amount=2000, fee_amount=20, interest_amount=80))
    assert postings(session_factory, txn_id) == sorted(
        [
            (acc(db, "5301"), 2000, "MAIN"),
            (acc(db, "5502"), 20, "FEE"),
            (acc(db, "5503"), 80, "INTEREST"),
            (acc(db, "2101"), -2100, ""),
        ]
    )
    assert "手續費" in client.get(f"/txn/{txn_id}").text  # 異動紀錄自動顯示分錄變化


def test_edit_exclude_from_budget_toggle(client, db, session_factory):
    txn_id = record(db)
    assert ok(edit(client, txn_id, exclude_from_budget="1"))
    assert txns(session_factory)[0].exclude_from_budget is True
    assert ok(edit(client, txn_id, exclude_from_budget=""))
    assert txns(session_factory)[0].exclude_from_budget is False
    assert "不計入預算" in client.get(f"/txn/{txn_id}").text  # 異動紀錄摘要


def test_refund_locks_exclude_toggle_on_field(client, db, session_factory):
    txn_id = record(db, payment_method_id=pm(db))
    transactions.record_refund(db, RefundForm(txn_id, TODAY, 300, pm(db)))
    resp = edit(client, txn_id, exclude_from_budget="1")
    assert resp.status_code == 422
    assert field_error_codes(resp.text, "exclude_from_budget") == ["REFUND_LOCKED"]
    assert ADVANCED_OPEN.search(resp.text)
    assert txns(session_factory)[0].exclude_from_budget is False
    # 手續費可以改（P1 U10、U11）
    assert ok(edit(client, txn_id, fee_amount=10, fee_account_id=acc(db, "5502")))


def test_foreign_currency_expense_not_editable_but_readable(client, db, session_factory):
    txn_id = record(db, original_currency="JPY", original_amount_minor=4800)
    detail = client.get(f"/txn/{txn_id}")
    assert detail.status_code == 200 and 'data-blocker="edit"' in detail.text
    assert client.get(f"/txn/{txn_id}/edit").status_code == 303
    resp = client.post(
        f"/txn/{txn_id}/edit",
        data={"csrf_token": csrf_from(client, "/txn/new"), "amount": "1", "action": "save"},
        headers=HX,
    )
    assert resp.status_code == 422 and 'data-code="NOT_EDITABLE"' in resp.text
    assert audits(session_factory, txn_id) == [("CREATE", None)]


def test_inactive_current_fee_account_kept(client, db, session_factory):
    from app.models import Account

    txn_id = record(db, fee_amount=15, fee_account_id=acc(db, "5502"))
    with session_factory() as s:
        s.get(Account, acc(s, "5502")).is_active = False
        s.get(Account, acc(s, "5501")).is_active = False
        s.commit()
    html = client.get(f"/txn/{txn_id}/edit").text
    assert "海外交易手續費（已停用）" in html
    assert "其他手續費" not in html
    assert ok(edit(client, txn_id, description="只改備註"))
    assert (acc(db, "5502"), 15, "FEE") in postings(session_factory, txn_id)
