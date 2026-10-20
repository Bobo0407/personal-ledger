# ruff: noqa: F811  （共用 test_web_txn_new.py 的 fixture：pytest 依參數名稱注入，不是重複定義）
"""/txn/new 的收入、轉帳與類型切換（P2 Step 5A，Web 層）。

帳務規則（V06、V10、V11、V13、TXN_STRUCTURE、D9 繳卡費）全部由 P1 判斷；
這裡只確認經過 Web 之後：類型分派正確、錯誤與確認的 HTTP 對應、權杖、
儲存並記下一筆保留／清空的欄位、類型切換。
"""

import re
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.models import Account, AuditLog, Transaction
from tests.integration.test_web_txn_new import (  # noqa: F401
    CSRF_INPUT,
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
    select_block,
    selected_value,
    session_factory,
    user_id,
)
from tests.ledger_helpers import TODAY, acc

TXN_TYPE_INPUT = re.compile(r'<input type="hidden" name="txn_type" value="([^"]*)">')


def page(client, txn_type: str, **params) -> str:
    resp = client.get("/txn/new", params={"type": txn_type, **params})
    assert resp.status_code == 200
    return resp.text


def tokens(client, txn_type: str) -> tuple[str, str]:
    html = page(client, txn_type)
    return CSRF_INPUT.search(html).group(1), TOKEN_INPUT.search(html).group(1)


def defaults(db, txn_type: str) -> dict[str, str]:
    if txn_type == "income":
        return {
            "amount": "3000",
            "account_id": str(acc(db, "4201")),
            "receive_account_id": str(acc(db, "1102")),
            "description": "",
        }
    return {
        "amount": "2000",
        "from_account_id": str(acc(db, "1102")),
        "to_account_id": str(acc(db, "1103")),
        "fee_amount": "",
        "fee_account_id": "",
        "description": "",
    }


def post(client, db, txn_type, *, token=None, posted_type=None, **overrides):
    """每次都從新開的該類型頁面取得權杖（一份新的表單）；token 指定時沿用同一份表單。"""
    csrf, fresh = tokens(client, txn_type)
    data = {
        "csrf_token": csrf,
        "txn_type": posted_type or txn_type,
        "submission_token": token or fresh,
        "txn_date": TODAY.isoformat(),
        "action": "next",
        **defaults(db, txn_type),
    }
    data.update({k: str(v) for k, v in overrides.items()})
    return client.post("/txn/new", data=data, headers=HX)


def txns(session_factory) -> list[Transaction]:
    with session_factory() as s:
        rows = s.scalars(select(Transaction).order_by(Transaction.id)).all()
        for t in rows:
            t.postings  # noqa: B018  在 Session 關閉前載入
        return rows


def audit_count(session_factory) -> int:
    with session_factory() as s:
        return s.scalar(select(func.count()).select_from(AuditLog))


def option_codes(db, html: str, name: str) -> set[str]:
    ids = {int(v) for v in re.findall(r'<option value="(\d+)"', select_block(html, name))}
    return set(db.scalars(select(Account.code).where(Account.id.in_(ids))))


def saved_message(html: str) -> str:
    match = re.search(r'<div class="notice notice-ok" role="status"[^>]*>(.*?)</div>', html, re.S)
    assert match
    return " ".join(match.group(1).split())


# --- GET：類型與選項 ------------------------------------------------------------------


def test_default_type_is_expense(client):
    html = client.get("/txn/new").text
    assert TXN_TYPE_INPUT.findall(html) == ["expense"]
    assert "記一筆支出" in html
    assert re.search(r'data-type="expense" class="active" aria-current="page"', html)


@pytest.mark.parametrize("bad", ["banana", "INCOME", "card_payment", ""])
def test_invalid_type_falls_back_to_expense(client, bad):
    html = page(client, bad)
    # hidden txn_type 一定是合法的 expense，不合法的值不會進到 POST
    assert TXN_TYPE_INPUT.findall(html) == ["expense"]
    assert "記一筆支出" in html


@pytest.mark.parametrize("txn_type", ["income", "transfer"])
def test_type_page_has_its_own_hidden_type_and_no_default_accounts(client, txn_type):
    html = page(client, txn_type)
    assert TXN_TYPE_INPUT.findall(html) == [txn_type]
    assert len(TOKEN_INPUT.findall(html)) == 1
    assert input_value(html, "txn_date") == TODAY.isoformat()
    # 不套用支出的預設付款方式語意：收款、轉出、轉入都沒有預設值
    names = (
        ["account_id", "receive_account_id"]
        if txn_type == "income"
        else [
            "from_account_id",
            "to_account_id",
            "fee_account_id",
        ]
    )
    for name in names:
        assert selected_value(html, name) is None
    assert 'name="payment_method_id"' not in html
    assert 'name="merchant_name"' not in html


def test_income_options(client, db):
    html = page(client, "income")
    assert option_codes(db, html, "account_id") == {"4101", "4201"}
    assert option_codes(db, html, "receive_account_id") == {
        "1101",
        "1102",
        "1103",
        "1201",
        "1301",
        "2101",
    }


def test_transfer_options(client, db, session_factory):
    with session_factory() as s:
        # 停用的帳戶不列
        s.get(Account, acc(s, "1103")).is_active = False
        s.commit()
    html = page(client, "transfer")
    # 轉出不列信用卡（UI 選項；P6 仍由 P1 檢查）、轉入包含信用卡；非末層（4100、5100…）不列
    assert option_codes(db, html, "from_account_id") == {"1101", "1102", "1201", "1301"}
    assert option_codes(db, html, "to_account_id") == {"1101", "1102", "1201", "1301", "2101"}
    fee = option_codes(db, html, "fee_account_id")
    assert "5501" in fee and "5500" not in fee and all(c.startswith("5") for c in fee)


# --- V1.1 提款入口（transfer + preset=withdraw） --------------------------------------------

FEE_DETAILS = re.compile(r'<details class="voucher fee"([^>]*)>')


def test_withdraw_preset_preselects_single_cash_and_opens_fee(client, db):
    html = page(client, "transfer", preset="withdraw")
    assert "<h1>記一筆提款</h1>" in html
    assert hidden(html, "txn_type") == "transfer"  # 送出仍是一般轉帳
    assert selected_value(html, "to_account_id") == str(acc(db, "1101"))
    assert selected_value(html, "from_account_id") is None
    assert selected_value(html, "fee_account_id") is None  # 手續費科目不預選
    assert FEE_DETAILS.search(html).group(1) == " open"


def test_plain_transfer_keeps_fee_closed_and_nothing_preselected(client, db):
    html = page(client, "transfer")
    assert "<h1>記一筆轉帳</h1>" in html
    assert selected_value(html, "to_account_id") is None
    assert FEE_DETAILS.search(html).group(1) == ""


@pytest.mark.parametrize("preset", ["bogus", "WITHDRAW", ""])
def test_unknown_preset_is_ignored(client, preset):
    html = page(client, "transfer", preset=preset)
    assert "<h1>記一筆轉帳</h1>" in html
    assert selected_value(html, "to_account_id") is None
    assert FEE_DETAILS.search(html).group(1) == ""


def test_withdraw_preset_only_applies_to_transfer(client):
    html = page(client, "expense", preset="withdraw")
    assert "<h1>記一筆支出</h1>" in html


def test_withdraw_preset_no_cash_account_selects_nothing(client, db, session_factory):
    with session_factory() as s:
        s.get(Account, acc(s, "1101")).is_active = False
        s.commit()
    html = page(client, "transfer", preset="withdraw")
    assert "<h1>記一筆提款</h1>" in html
    assert selected_value(html, "to_account_id") is None


def test_withdraw_preset_multiple_cash_accounts_selects_nothing(client, db, session_factory):
    with session_factory() as s:
        s.get(Account, acc(s, "1103")).subtype = "CASH"
        s.commit()
    html = page(client, "transfer", preset="withdraw")
    assert selected_value(html, "to_account_id") is None


def test_withdraw_saves_as_transfer_and_only_fee_is_expense(client, db, session_factory):
    resp = post(
        client,
        db,
        "transfer",
        amount="3000",
        from_account_id=acc(db, "1102"),
        to_account_id=acc(db, "1101"),
        fee_amount="5",
        fee_account_id=acc(db, "5501"),
    )
    assert saved_message(resp.text) == "已儲存：轉帳 3,005 元"
    [txn] = txns(session_factory)
    assert txn.txn_type == "TRANSFER"
    by_account = {p.account_id: p.amount for p in txn.postings}
    assert by_account == {acc(db, "1101"): 3000, acc(db, "5501"): 5, acc(db, "1102"): -3005}
    assert 'data-expense-net="5" data-income-total="0"' in client.get("/txn").text


# --- 類型切換（HTMX） ------------------------------------------------------------------


def test_htmx_type_switch_keeps_only_date_and_amount(client):
    future = (TODAY + timedelta(days=1)).isoformat()
    resp = client.get(
        "/txn/new",
        params={"type": "transfer", "amount": "450", "txn_date": future, "description": "x"},
        headers=HX,
    )
    html = resp.text
    assert resp.status_code == 200
    assert "<html" not in html  # 只回 #txn-new 的內容
    assert input_value(html, "amount") == "450"
    assert input_value(html, "txn_date") == future
    assert input_value(html, "description") == ""
    assert TXN_TYPE_INPUT.findall(html) == ["transfer"]
    # 換掉整個 #txn-new：DOM 裡只會有這一個新的權杖
    assert len(TOKEN_INPUT.findall(html)) == 1
    assert 'id="txn-area"' in html and html.count('class="type-tabs"') == 1


def test_type_switch_issues_new_token_each_time(client):
    a = TOKEN_INPUT.findall(client.get("/txn/new?type=income", headers=HX).text)
    b = TOKEN_INPUT.findall(client.get("/txn/new?type=income", headers=HX).text)
    assert len(a) == len(b) == 1 and a != b


def test_type_switch_ignores_bad_date(client):
    html = client.get("/txn/new", params={"type": "income", "txn_date": "x"}, headers=HX).text
    assert input_value(html, "txn_date") == TODAY.isoformat()


def test_tabs_push_url(client):
    html = page(client, "income")
    for t in ("expense", "income", "transfer"):
        assert f'hx-push-url="/txn/new?type={t}"' in html
        assert f'href="/txn/new?type={t}"' in html


# --- POST：類型分派與權杖 ----------------------------------------------------------------


def test_post_invalid_txn_type_is_422_without_orchestration(client, db, session_factory):
    resp = post(client, db, "income", posted_type="banana")
    assert resp.status_code == 422
    assert "表單已失效" in resp.text
    assert TXN_TYPE_INPUT.findall(resp.text) == ["expense"]
    assert txns(session_factory) == []


@pytest.mark.parametrize("txn_type", ["income", "transfer"])
@pytest.mark.parametrize("token", ["", "short"])
def test_web_create_requires_token(client, db, session_factory, txn_type, token):
    csrf, _ = tokens(client, txn_type)
    data = {
        "csrf_token": csrf,
        "txn_type": txn_type,
        "txn_date": TODAY.isoformat(),
        "submission_token": token,
        **defaults(db, txn_type),
    }
    resp = client.post("/txn/new", data=data, headers=HX)
    assert resp.status_code == 422
    assert txns(session_factory) == []
    # 換一個新權杖，其他輸入保留
    new = TOKEN_INPUT.findall(resp.text)
    assert len(new) == 1 and new[0] not in ("", "short")
    assert input_value(resp.text, "amount") == defaults(db, txn_type)["amount"]


@pytest.mark.parametrize("txn_type", ["income", "transfer"])
def test_replay_creates_nothing(client, db, session_factory, txn_type):
    _, token = tokens(client, txn_type)
    first = post(client, db, txn_type, token=token)
    assert first.status_code == 200
    again = post(client, db, txn_type, token=token, amount="99999")
    assert again.status_code == 200
    assert 'data-replayed="true"' in again.text
    assert len(txns(session_factory)) == 1
    assert audit_count(session_factory) == 1


def test_card_payment_replay_creates_nothing(client, db, session_factory):
    _, token = tokens(client, "transfer")
    post(client, db, "transfer", token=token, to_account_id=acc(db, "2101"))
    again = post(client, db, "transfer", token=token, to_account_id=acc(db, "2101"))
    assert 'data-replayed="true"' in again.text
    [txn] = txns(session_factory)
    assert (txn.txn_type, txn.source_ref) == ("CARD_PAYMENT", f"submit:{token}")
    assert audit_count(session_factory) == 1


# --- 收入 ------------------------------------------------------------------------------


def test_income_happy_path(client, db, session_factory):
    resp = post(client, db, "income", description="利息")
    assert resp.status_code == 200
    assert saved_message(resp.text) == "已儲存：收入 3,000 元"
    [txn] = txns(session_factory)
    assert txn.txn_type == "INCOME" and txn.description == "利息"
    by_account = {p.account_id: p.amount for p in txn.postings}
    assert by_account == {acc(db, "1102"): 3000, acc(db, "4201"): -3000}
    with session_factory() as s:
        log = s.scalars(select(AuditLog)).one()
        assert log.user_id == user_id(db)


def test_income_save_next_keeps_date_and_receive_account(client, db):
    future = (TODAY + timedelta(days=1)).isoformat()
    resp = post(client, db, "income", txn_date=future, description="x", confirmed="V11")
    html = resp.text
    assert resp.status_code == 200
    assert input_value(html, "txn_date") == future
    assert selected_value(html, "receive_account_id") == str(acc(db, "1102"))
    assert input_value(html, "amount") == ""
    assert selected_value(html, "account_id") is None
    assert input_value(html, "description") == ""
    assert TXN_TYPE_INPUT.findall(html) == ["income"]
    assert re.search(r'<input id="amount"[^>]*autofocus', html)


def test_income_save_home_redirects(client, db, session_factory):
    resp = post(client, db, "income", action="home")
    assert resp.headers["HX-Redirect"] == "/"
    assert len(txns(session_factory)) == 1


def test_income_parse_error_keeps_input_and_token(client, db, session_factory):
    _, token = tokens(client, "income")
    resp = post(client, db, "income", token=token, amount="abc", description="留著")
    assert resp.status_code == 422
    assert field_error_codes(resp.text, "amount") == [None]
    assert input_value(resp.text, "description") == "留著"
    assert TOKEN_INPUT.findall(resp.text) == [token]
    assert txns(session_factory) == []


def test_income_v10_confirmation_keeps_type_fields_and_token(client, db, session_factory):
    _, token = tokens(client, "income")
    resp = post(client, db, "income", token=token, amount="12000", description="獎金")
    assert resp.status_code == 409
    assert confirm_codes(resp.text) == ["V10"]
    assert hidden(resp.text, "txn_type") == "income"
    assert hidden(resp.text, "submission_token") == token
    assert hidden(resp.text, "receive_account_id") == str(acc(db, "1102"))
    assert hidden(resp.text, "description") == "獎金"
    assert txns(session_factory) == []

    ok = post(client, db, "income", token=token, amount="12000", confirmed="V10")
    assert ok.status_code == 200
    assert saved_message(ok.text) == "已儲存：收入 12,000 元"


def test_confirm_back_to_edit_returns_same_type_form(client, db):
    _, token = tokens(client, "transfer")
    resp = post(client, db, "transfer", token=token, amount="12000", action="edit")
    assert resp.status_code == 200
    assert TXN_TYPE_INPUT.findall(resp.text) == ["transfer"]
    assert input_value(resp.text, "amount") == "12000"
    assert TOKEN_INPUT.findall(resp.text) == [token]


# --- 轉帳 ------------------------------------------------------------------------------


def test_transfer_happy_path_not_income_or_expense(client, db, session_factory):
    resp = post(client, db, "transfer")
    assert saved_message(resp.text) == "已儲存：轉帳 2,000 元"
    [txn] = txns(session_factory)
    assert txn.txn_type == "TRANSFER"
    listing = client.get("/txn").text
    assert 'data-expense-net="0" data-income-total="0"' in listing


def test_transfer_fee(client, db, session_factory):
    resp = post(
        client, db, "transfer", amount="5000", fee_amount="15", fee_account_id=acc(db, "5501")
    )
    assert saved_message(resp.text) == "已儲存：轉帳 5,015 元"
    [txn] = txns(session_factory)
    by_account = {p.account_id: p.amount for p in txn.postings}
    assert by_account == {acc(db, "1103"): 5000, acc(db, "5501"): 15, acc(db, "1102"): -5015}
    # 只有手續費算支出
    assert 'data-expense-net="15"' in client.get("/txn").text


def test_transfer_save_next_keeps_date_and_from_account(client, db):
    resp = post(
        client, db, "transfer", fee_amount="15", fee_account_id=acc(db, "5501"), description="x"
    )
    html = resp.text
    assert input_value(html, "txn_date") == TODAY.isoformat()
    assert selected_value(html, "from_account_id") == str(acc(db, "1102"))
    for name in ("amount", "fee_amount", "description"):
        assert input_value(html, name) == ""
    for name in ("to_account_id", "fee_account_id"):
        assert selected_value(html, name) is None
    assert len(TOKEN_INPUT.findall(html)) == 1


def test_save_next_issues_new_token(client, db):
    _, token = tokens(client, "transfer")
    resp = post(client, db, "transfer", token=token)
    [new] = TOKEN_INPUT.findall(resp.text)
    assert new != token


def test_transfer_to_credit_card_becomes_card_payment(client, db, session_factory):
    resp = post(client, db, "transfer", to_account_id=acc(db, "2101"))
    assert resp.status_code == 200
    assert saved_message(resp.text) == "已儲存：繳卡費 2,000 元"
    [txn] = txns(session_factory)
    assert txn.txn_type == "CARD_PAYMENT"
    listing = client.get("/txn").text
    assert 'data-expense-net="0" data-income-total="0"' in listing
    assert "繳卡費" in listing
    assert "繳卡費" in client.get(f"/txn/{txn.id}").text


def test_card_payment_with_fee_errors_on_fee_field(client, db, session_factory):
    resp = post(
        client,
        db,
        "transfer",
        to_account_id=acc(db, "2101"),
        fee_amount="15",
        fee_account_id=acc(db, "5501"),
    )
    assert resp.status_code == 422
    assert field_error_codes(resp.text, "fee_amount") == ["TXN_STRUCTURE"]
    assert txns(session_factory) == []


def test_card_payment_from_pension_rejected_by_p1(client, db, session_factory):
    """勞退不在 Web 的特殊判斷裡：由 P1（P7）擋下，錯誤顯示在欄位或頂端都可以。"""
    resp = post(
        client, db, "transfer", from_account_id=acc(db, "1201"), to_account_id=acc(db, "2101")
    )
    assert resp.status_code == 422
    assert txns(session_factory) == []
    codes = banner_codes(resp.text) + [
        c for n in ("from_account_id", "to_account_id") for c in field_error_codes(resp.text, n)
    ]
    assert codes


def test_transfer_from_credit_card_rejected_by_p1(client, db, session_factory):
    """選單不列信用卡，但竄改的請求仍由 P1（P6）擋下。"""
    resp = post(client, db, "transfer", from_account_id=acc(db, "2101"))
    assert resp.status_code == 422
    assert txns(session_factory) == []


def test_transfer_v06_same_account_shows_on_to_field(client, db, session_factory):
    resp = post(client, db, "transfer", to_account_id=acc(db, "1102"))
    assert resp.status_code == 422
    assert field_error_codes(resp.text, "to_account_id") == ["V06"]
    assert selected_value(resp.text, "from_account_id") == str(acc(db, "1102"))
    assert txns(session_factory) == []


def test_transfer_v11_future_date_confirmation(client, db, session_factory):
    future = (TODAY + timedelta(days=3)).isoformat()
    resp = post(client, db, "transfer", txn_date=future)
    assert resp.status_code == 409
    assert confirm_codes(resp.text) == ["V11"]
    assert hidden(resp.text, "txn_type") == "transfer"
    assert txns(session_factory) == []


def test_transfer_v13_notice_shown_after_save(client, db, session_factory):
    resp = post(client, db, "transfer", from_account_id=acc(db, "1101"), amount="500")
    assert resp.status_code == 200
    assert re.search(r'class="notice notice-warn" data-code="V13"', resp.text)
    assert len(txns(session_factory)) == 1


def test_card_payment_v13_notice_shown_after_save(client, db):
    resp = post(client, db, "transfer", to_account_id=acc(db, "2101"))
    assert saved_message(resp.text) == "已儲存：繳卡費 2,000 元"
    assert re.search(r'data-code="V13"', resp.text)
