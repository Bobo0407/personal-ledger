"""/txn/new 快速記帳（P2 Step 1：EXPENSE）。

Web 層只驗證「接得上 P1」：登入、CSRF、型態解析、錯誤與確認的 HTTP 對應、
user_id 傳到稽核紀錄。帳務規則本身已由 P1 的測試涵蓋，這裡只確認經過 Web 之後結果不變。
"""

import logging
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app import security
from app.config import Settings
from app.db import get_db, make_sessionmaker
from app.logging_config import APP_LOGGER, LOG_FILE_NAME
from app.main import create_app
from app.models import AuditLog, Merchant, PaymentMethod, Setting, Transaction, User
from app.services import accounts as account_service
from app.services.master_data_seed import seed_master_data
from tests.conftest import TEST_CHART
from tests.ledger_helpers import TODAY, acc, freeze_today, post_manual

PASSWORD = "correct-password"
CSRF_INPUT = re.compile(r'name="csrf_token" value="([^"]+)"')
TOKEN_INPUT = re.compile(r'name="submission_token" value="([^"]+)"')
HX = {"HX-Request": "true"}


@pytest.fixture(autouse=True)
def _today(monkeypatch):
    freeze_today(monkeypatch)


@pytest.fixture
def session_factory(migrated_engine):
    factory = make_sessionmaker(migrated_engine)
    with factory() as db:
        seed_master_data(db, TEST_CHART)
        db.add(User(username="bo", password_hash=security.hash_password(PASSWORD)))
        db.commit()
    return factory


@pytest.fixture
def db(session_factory):
    with session_factory() as session:
        yield session


@pytest.fixture
def log_dir(tmp_path):
    return tmp_path / "logs"


@pytest.fixture
def anon(session_factory, log_dir):
    """未登入的 client。raise_server_exceptions=False：500 時拿到錯誤回應而不是例外。"""
    settings = Settings(_env_file=None, secret_key="test-secret-key", log_dir=log_dir)
    app = create_app(settings)

    def override_get_db():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app, follow_redirects=False, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def client(anon):
    token = csrf_from(anon, "/login")
    resp = anon.post(
        "/login", data={"username": "bo", "password": PASSWORD, "next": "/", "csrf_token": token}
    )
    assert resp.status_code == 303
    return anon


def csrf_from(client: TestClient, path: str) -> str:
    resp = client.get(path)
    assert resp.status_code == 200
    match = CSRF_INPUT.search(resp.text)
    assert match
    return match.group(1)


def pm_id(db, name: str) -> int:
    return db.scalar(select(PaymentMethod.id).where(PaymentMethod.name == name))


def user_id(db) -> int:
    return db.scalar(select(User.id).where(User.username == "bo"))


def txn_count(session_factory) -> int:
    with session_factory() as s:
        return s.scalar(select(func.count()).select_from(Transaction))


def only_txn(session_factory) -> Transaction:
    with session_factory() as s:
        txns = s.scalars(select(Transaction)).all()
        assert len(txns) == 1
        txn = txns[0]
        txn.postings  # 在 Session 關閉前載入
        return txn


def fresh_tokens(client: TestClient) -> tuple[str, str]:
    """(csrf_token, submission_token)：從新開的 /txn/new 頁面取得。"""
    html = client.get("/txn/new").text
    return CSRF_INPUT.search(html).group(1), TOKEN_INPUT.search(html).group(1)


def submit(client, db, *, htmx=True, **overrides):
    """以預設值（今天、正餐 120、LINE Pay）送出表單；overrides 可以覆寫或加欄位。
    每次都從新開的頁面取得權杖，也就是「每次都是一份新的表單」。"""
    csrf, token = fresh_tokens(client)
    data = {
        "csrf_token": csrf,
        "submission_token": token,
        "txn_date": TODAY.isoformat(),
        "amount": "120",
        "account_id": str(acc(db, "5101")),
        "payment_method_id": str(pm_id(db, "LINE Pay")),
        "description": "",
        "action": "next",
    }
    data.update({k: str(v) for k, v in overrides.items()})
    return client.post("/txn/new", data=data, headers=HX if htmx else {})


def select_block(html: str, name: str) -> str:
    match = re.search(rf'<select id="{name}".*?</select>', html, re.S)
    assert match, name
    return match.group(0)


def selected_value(html: str, name: str) -> str | None:
    match = re.search(r'<option value="([^"]*)" selected>', select_block(html, name))
    return match.group(1) if match else None


def input_value(html: str, name: str) -> str:
    match = re.search(rf'<input id="{name}"[^>]*?value="([^"]*)"', html, re.S)
    assert match, name
    return match.group(1)


def field_error_codes(html: str, name: str) -> list[str | None]:
    return [
        code or None
        for code in re.findall(
            rf'<p class="field-error" id="{name}-error"(?: data-code="([^"]+)")?>', html
        )
    ]


def banner_codes(html: str) -> list[str]:
    match = re.search(r'<div class="alert" role="alert">(.*?)</div>', html, re.S)
    return re.findall(r'data-code="([^"]+)"', match.group(1)) if match else []


# --- 登入、CSRF -----------------------------------------------------------------------


def test_get_requires_login(anon):
    resp = anon.get("/txn/new")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login?next=%2Ftxn%2Fnew"


def test_htmx_post_without_login_redirects_whole_page(anon, db, session_factory):
    token = csrf_from(anon, "/login")
    resp = anon.post(
        "/txn/new",
        data={"csrf_token": token, "amount": "120", "account_id": str(acc(db, "5101"))},
        headers=HX,
    )
    assert resp.status_code == 200
    assert resp.headers["HX-Redirect"] == "/login?next=%2Ftxn%2Fnew"
    assert resp.text == ""
    assert txn_count(session_factory) == 0


def test_non_htmx_unauthenticated_still_gets_303(anon):
    token = csrf_from(anon, "/login")
    resp = anon.post("/txn/new", data={"csrf_token": token})
    assert resp.status_code == 303


def test_post_without_csrf_is_rejected(client, db, session_factory):
    resp = client.post("/txn/new", data={"amount": "120"}, headers=HX)
    assert resp.status_code == 403
    assert txn_count(session_factory) == 0


# --- GET 表單 -------------------------------------------------------------------------


def test_form_defaults(client, db):
    html = client.get("/txn/new").text
    assert input_value(html, "txn_date") == TODAY.isoformat()
    assert selected_value(html, "payment_method_id") == str(pm_id(db, "LINE Pay"))
    assert selected_value(html, "account_id") is None
    amount = re.search(r'<input id="amount"[^>]*>', html, re.S).group(0)
    assert 'inputmode="numeric"' in amount
    assert "autofocus" in amount


def test_page_loads_htmx_and_response_handling(client):
    html = client.get("/txn/new").text
    assert "js/htmx.min.js" in html
    config = re.search(r'<meta name="htmx-config" content=\'([^\']+)\'>', html).group(1)
    assert '{"code":"409","swap":true,"error":false}' in config
    assert '{"code":"422","swap":true,"error":false}' in config
    assert '"target":"#txn-status"' in config


def test_account_options_are_active_expense_leaves(client, db):
    account_service.deactivate_account(db, acc(db, "5102"))
    accounts = select_block(client.get("/txn/new").text, "account_id")
    options = re.findall(r"<option value=\"\d+\"[^>]*>([^<]+)</option>", accounts)
    assert "正餐" in options
    assert "飲料" not in options  # 已停用
    assert "餐飲" not in options  # 非末層，只當分組標題
    assert '<optgroup label="餐飲">' in accounts
    for other in ("現金", "郵局", "聯邦信用卡", "期初權益", "本薪", "利息收入"):
        assert other not in options


# --- 儲存 -----------------------------------------------------------------------------


def test_save_and_next_records_expense(client, db, session_factory):
    resp = submit(client, db, description="午餐")
    assert resp.status_code == 200
    assert "<html" not in resp.text
    assert "已儲存：正餐 120 元" in resp.text

    txn = only_txn(session_factory)
    assert txn.txn_type == "EXPENSE"
    assert txn.status == "POSTED"
    assert txn.txn_date == TODAY.isoformat()
    assert txn.description == "午餐"
    assert txn.payment_method_id == pm_id(db, "LINE Pay")
    lines = [(p.account_id, p.amount, p.line_role) for p in txn.postings]
    assert lines == [(acc(db, "5101"), 120, "MAIN"), (acc(db, "1102"), -120, None)]
    assert sum(p.amount for p in txn.postings) == 0


def test_save_and_next_keeps_date_and_payment_method_only(client, db):
    later = "2026-10-14"
    html = submit(
        client, db, txn_date=later, payment_method_id=pm_id(db, "現金"), description="x"
    ).text
    assert input_value(html, "txn_date") == later
    assert selected_value(html, "payment_method_id") == str(pm_id(db, "現金"))
    assert input_value(html, "amount") == ""
    assert input_value(html, "description") == ""
    assert selected_value(html, "account_id") is None
    assert "autofocus" in re.search(r'<input id="amount"[^>]*>', html, re.S).group(0)


def test_save_and_home_redirects(client, db, session_factory):
    resp = submit(client, db, action="home")
    assert resp.status_code == 200
    assert resp.headers["HX-Redirect"] == "/"
    assert txn_count(session_factory) == 1


def test_audit_log_records_logged_in_user(client, db, session_factory):
    submit(client, db)
    txn = only_txn(session_factory)
    with session_factory() as s:
        logs = s.scalars(select(AuditLog).where(AuditLog.entity_id == txn.id)).all()
    assert [(log.action, log.user_id) for log in logs] == [("CREATE", user_id(db))]


def test_fields_outside_the_form_are_not_saved(client, db, session_factory):
    """手續費、利息、不計入預算在 P2 Step 6 進了表單；外幣（V1.5）仍不在表單上，送來也忽略。"""
    submit(client, db, original_currency="USD", original_amount_minor="399")
    txn = only_txn(session_factory)
    assert len(txn.postings) == 2
    assert all(p.original_currency is None for p in txn.postings)


# --- 型態解析錯誤（Web 欄位錯誤，沒有規則代碼）-------------------------------------------


@pytest.mark.parametrize("value", ["abc", "12.5", "1,200"])
def test_unparseable_amount_is_web_field_error(client, db, session_factory, value):
    resp = submit(client, db, amount=value, description="留著")
    assert resp.status_code == 422
    assert "請輸入整數金額" in resp.text
    assert field_error_codes(resp.text, "amount") == [None]
    assert 'aria-invalid="true" aria-describedby="amount-error"' in resp.text
    assert input_value(resp.text, "amount") == value
    assert input_value(resp.text, "description") == "留著"
    assert txn_count(session_factory) == 0


def test_unparseable_date_and_ids(client, db, session_factory):
    resp = submit(client, db, txn_date="2026/10/15", account_id="x", payment_method_id="y")
    assert resp.status_code == 422
    assert field_error_codes(resp.text, "txn_date") == [None]
    assert field_error_codes(resp.text, "account_id") == [None]
    assert field_error_codes(resp.text, "payment_method_id") == [None]
    assert "日期格式不正確" in resp.text
    assert txn_count(session_factory) == 0


# --- P1 驗證錯誤（422，沿用 P1 的規則代碼）-----------------------------------------------


@pytest.mark.parametrize(("value", "code"), [("", "V05"), ("0", "V01"), ("-5", "V01")])
def test_amount_rules_come_from_p1(client, db, session_factory, value, code):
    resp = submit(client, db, amount=value)
    assert resp.status_code == 422
    assert field_error_codes(resp.text, "amount") == [code]
    assert txn_count(session_factory) == 0


def test_missing_account_and_date(client, db, session_factory):
    resp = submit(client, db, account_id="", txn_date="")
    assert resp.status_code == 422
    assert field_error_codes(resp.text, "account_id") == ["V05"]
    assert txn_count(session_factory) == 0


def test_missing_date_is_v05_on_date_field(client, db):
    resp = submit(client, db, txn_date="")
    assert resp.status_code == 422
    assert field_error_codes(resp.text, "txn_date") == ["V05"]


def test_unknown_payment_method(client, db, session_factory):
    resp = submit(client, db, payment_method_id=9999)
    assert resp.status_code == 422
    assert field_error_codes(resp.text, "payment_method_id") == ["V05"]
    assert txn_count(session_factory) == 0


def test_line_level_issues_show_in_banner(client, db, session_factory):
    """分錄層級的 V03（非末層）沒有 field，顯示在表單頂端。"""
    resp = submit(client, db, account_id=acc(db, "5100"))
    assert resp.status_code == 422
    assert banner_codes(resp.text) == ["V03"]
    assert txn_count(session_factory) == 0


def test_stale_inactive_account_shows_in_banner(client, db, session_factory):
    account_service.deactivate_account(db, acc(db, "5102"))
    resp = submit(client, db, account_id=acc(db, "5102"))
    assert resp.status_code == 422
    assert banner_codes(resp.text) == ["V04"]
    assert txn_count(session_factory) == 0


# --- 確認（409：V10、V11）----------------------------------------------------------------


def hidden(html: str, name: str) -> str:
    match = re.search(rf'<input type="hidden" name="{name}" value="([^"]*)">', html)
    assert match, name
    return match.group(1)


def confirm_codes(html: str) -> list[str]:
    return re.findall(r'<li data-code="([^"]+)">', html)


def test_t19_large_amount_requires_confirmation(client, db, session_factory):
    resp = submit(client, db, amount=12000)
    assert resp.status_code == 409
    assert confirm_codes(resp.text) == ["V10"]
    assert "12,000" in resp.text
    assert hidden(resp.text, "confirmed") == "V10"
    assert hidden(resp.text, "amount") == "12000"
    assert txn_count(session_factory) == 0

    resp = submit(client, db, amount=12000, confirmed=hidden(resp.text, "confirmed"))
    assert resp.status_code == 200
    assert "已儲存" in resp.text
    assert txn_count(session_factory) == 1


def test_future_date_requires_confirmation(client, db, session_factory):
    resp = submit(client, db, txn_date="2026-10-18")
    assert resp.status_code == 409
    assert confirm_codes(resp.text) == ["V11"]
    resp = submit(client, db, txn_date="2026-10-18", confirmed="V11")
    assert resp.status_code == 200
    assert txn_count(session_factory) == 1


def test_both_confirmations_listed_together(client, db):
    resp = submit(client, db, amount=12000, txn_date="2026-10-18")
    assert resp.status_code == 409
    assert confirm_codes(resp.text) == ["V10", "V11"]
    assert hidden(resp.text, "confirmed") == "V10,V11"


def test_confirmation_does_not_cover_new_rules(client, db, session_factory):
    """只確認過 V10，但內容又觸發 V11：P1 重新檢查，仍要再確認 V11。"""
    resp = submit(client, db, amount=12000, txn_date="2026-10-18", confirmed="V10")
    assert resp.status_code == 409
    assert confirm_codes(resp.text) == ["V11"]
    assert hidden(resp.text, "confirmed") == "V10,V11"
    assert txn_count(session_factory) == 0


def test_confirmation_keeps_save_and_home(client, db, session_factory):
    resp = submit(client, db, amount=12000, action="home")
    assert resp.status_code == 409
    assert 'id="btn-confirm" type="submit" name="action" value="home"' in resp.text
    resp = submit(client, db, amount=12000, action="home", confirmed="V10")
    assert resp.headers["HX-Redirect"] == "/"
    assert txn_count(session_factory) == 1


def test_edit_returns_form_without_saving(client, db, session_factory):
    resp = submit(client, db, amount=12000, confirmed="V10", action="edit", description="手機")
    assert resp.status_code == 200
    assert input_value(resp.text, "amount") == "12000"
    assert input_value(resp.text, "description") == "手機"
    assert 'name="confirmed"' not in resp.text
    assert txn_count(session_factory) == 0


# --- 提醒（V13：儲存後顯示，不阻擋）------------------------------------------------------


def test_v13_notice_shown_after_save(client, db, session_factory):
    resp = submit(client, db, payment_method_id=pm_id(db, "現金"))
    assert resp.status_code == 200
    assert txn_count(session_factory) == 1
    notice = re.search(r'<p class="notice notice-warn" data-code="V13">([^<]+)</p>', resp.text)
    assert notice
    assert "現金餘額變成 −120" in notice.group(1)


def test_no_v13_notice_when_balance_stays_positive(client, db):
    post_manual(db, TODAY, ("1101", 1000), ("4201", -1000))
    resp = submit(client, db, payment_method_id=pm_id(db, "現金"))
    assert resp.status_code == 200
    assert "notice-warn" not in resp.text


# --- 系統錯誤（500）-------------------------------------------------------------------


def read_log(log_dir) -> str:
    for handler in logging.getLogger(APP_LOGGER).handlers:
        handler.flush()
    return (log_dir / LOG_FILE_NAME).read_text(encoding="utf-8")


def test_system_error_returns_htmx_fragment_without_details(client, db, session_factory, log_dir):
    db.get(Setting, "large_amount_threshold").value = "abc"  # InvalidSetting
    db.commit()
    resp = submit(client, db, amount=4567, description="秘密備註")
    assert resp.status_code == 500
    assert "<html" not in resp.text
    assert "發生錯誤，資料未變更" in resp.text
    assert "large_amount_threshold" not in resp.text
    assert txn_count(session_factory) == 0

    log = read_log(log_dir)
    assert "unhandled error POST /txn/new" in log
    assert "4567" not in log
    assert "秘密備註" not in log


# === P2 Step 2：商家、V12、憑證／發票、一次性送出權杖 ======================================


def merchants_in_db(session_factory) -> list[tuple[int, str]]:
    with session_factory() as s:
        return [(m.id, m.name) for m in s.scalars(select(Merchant).order_by(Merchant.id))]


def test_merchant_created_then_reused(client, db, session_factory):
    assert submit(client, db, merchant_name="7-11").status_code == 200
    assert submit(client, db, merchant_name=" ７－１１ ", amount=60).status_code == 200
    assert merchants_in_db(session_factory) == [(1, "7-11")]
    with session_factory() as s:
        assert [t.merchant_id for t in s.scalars(select(Transaction))] == [1, 1]
    html = client.get("/txn/new").text
    assert '<datalist id="merchant-options">' in html
    assert '<option value="7-11"></option>' in html


def test_blank_merchant_saves_without_merchant(client, db, session_factory):
    submit(client, db, merchant_name="   ")
    assert only_txn(session_factory).merchant_id is None
    assert merchants_in_db(session_factory) == []


def test_inactive_merchant_error_shows_on_merchant_field(client, db, session_factory):
    db.add(Merchant(name="舊店", is_active=False))
    db.commit()
    resp = submit(client, db, merchant_name="舊店")
    assert resp.status_code == 422
    assert field_error_codes(resp.text, "merchant_name") == ["V04"]
    assert input_value(resp.text, "merchant_name") == "舊店"
    assert txn_count(session_factory) == 0


def test_new_merchant_stays_when_entry_is_rejected(client, db, session_factory):
    """決策 M2 的已知取捨：商家先 commit，交易被擋下時留下沒有交易引用的商家。"""
    resp = submit(client, db, merchant_name="新店", amount="")
    assert resp.status_code == 422
    assert txn_count(session_factory) == 0
    assert merchants_in_db(session_factory) == [(1, "新店")]


def test_v12_web_confirmation(client, db, session_factory):
    submit(client, db, merchant_name="7-11", amount=85)
    resp = submit(client, db, merchant_name="7-11", amount=85)
    assert resp.status_code == 409
    assert confirm_codes(resp.text) == ["V12"]
    assert "今天已經有一筆 7-11 85 元，是重複嗎？" in resp.text
    assert hidden(resp.text, "merchant_name") == "7-11"
    assert txn_count(session_factory) == 1

    resp = submit(client, db, merchant_name="7-11", amount=85, confirmed="V12")
    assert resp.status_code == 200
    assert txn_count(session_factory) == 2


def test_v12_web_ignores_deleted_original(client, db, session_factory):
    from app.services import transactions as txn_service

    submit(client, db, merchant_name="7-11", amount=85)
    txn_service.delete_transaction(db, only_txn(session_factory).id)
    assert submit(client, db, merchant_name="7-11", amount=85).status_code == 200


@pytest.mark.parametrize("raw", ["ab12345678", "AB-12345678", " ab-12345678 "])
def test_invoice_normalized_and_saved(client, db, session_factory, raw):
    resp = submit(client, db, voucher_type="EINVOICE", invoice_no=raw)
    assert resp.status_code == 200
    txn = only_txn(session_factory)
    assert (txn.voucher_type, txn.invoice_no) == ("EINVOICE", "AB12345678")


def test_invoice_format_error_on_field(client, db, session_factory):
    resp = submit(client, db, invoice_no="AB-1234")
    assert resp.status_code == 422
    assert field_error_codes(resp.text, "invoice_no") == ["V08"]
    assert '<details class="voucher" open>' in resp.text
    assert input_value(resp.text, "invoice_no") == "AB-1234"
    assert txn_count(session_factory) == 0


def test_invoice_duplicate_on_field(client, db, session_factory):
    submit(client, db, invoice_no="AB12345678")
    resp = submit(client, db, invoice_no="ab-12345678", amount=99)
    assert resp.status_code == 422
    assert field_error_codes(resp.text, "invoice_no") == ["V08"]
    assert f"這張發票已記在 {TODAY.month}/{TODAY.day} 的交易" in resp.text
    assert txn_count(session_factory) == 1


def test_unknown_voucher_type_is_web_field_error(client, db, session_factory):
    resp = submit(client, db, voucher_type="PHOTO")
    assert resp.status_code == 422
    assert field_error_codes(resp.text, "voucher_type") == [None]
    assert txn_count(session_factory) == 0


def test_save_and_next_clears_merchant_and_voucher(client, db):
    html = submit(
        client, db, merchant_name="7-11", voucher_type="EINVOICE", invoice_no="AB12345678"
    ).text
    assert input_value(html, "merchant_name") == ""
    assert input_value(html, "invoice_no") == ""
    assert selected_value(html, "voucher_type") is None
    assert '<details class="voucher">' in html


def test_post_without_token_is_422_and_gets_new_token(client, db, session_factory):
    resp = submit(client, db, submission_token="")
    assert resp.status_code == 422
    assert "表單已失效，請重新整理後再試。" in resp.text
    assert banner_codes(resp.text) == []
    assert TOKEN_INPUT.search(resp.text).group(1) != ""
    assert txn_count(session_factory) == 0


def test_page_has_submission_token(client):
    first = TOKEN_INPUT.search(client.get("/txn/new").text).group(1)
    second = TOKEN_INPUT.search(client.get("/txn/new").text).group(1)
    assert len(first) >= 16
    assert first != second


def test_same_token_twice_creates_one_transaction(client, db, session_factory):
    csrf, token = fresh_tokens(client)
    data = {
        "csrf_token": csrf,
        "submission_token": token,
        "txn_date": TODAY.isoformat(),
        "amount": "120",
        "account_id": str(acc(db, "5101")),
        "payment_method_id": str(pm_id(db, "LINE Pay")),
        "action": "next",
    }
    first = client.post("/txn/new", data=data, headers=HX)
    second = client.post("/txn/new", data=data, headers=HX)
    assert first.status_code == second.status_code == 200
    assert 'data-replayed="true"' in second.text
    assert "這筆已經儲存過，沒有重複建立。" in second.text
    txn = only_txn(session_factory)
    assert f'data-txn-id="{txn.id}"' in second.text
    with session_factory() as s:
        logs = s.scalars(select(AuditLog)).all()
    assert [(log.entity_id, log.user_id) for log in logs] == [(txn.id, user_id(db))]
    # 回應帶著新的權杖，下一筆可以照常儲存
    assert TOKEN_INPUT.search(second.text).group(1) != token


def test_same_token_replay_with_save_and_home(client, db, session_factory):
    csrf, token = fresh_tokens(client)
    data = {
        "csrf_token": csrf,
        "submission_token": token,
        "txn_date": TODAY.isoformat(),
        "amount": "120",
        "account_id": str(acc(db, "5101")),
        "payment_method_id": str(pm_id(db, "LINE Pay")),
        "action": "home",
    }
    client.post("/txn/new", data=data, headers=HX)
    resp = client.post("/txn/new", data=data, headers=HX)
    assert resp.headers["HX-Redirect"] == "/"
    assert txn_count(session_factory) == 1


def test_token_survives_422_409_and_edit(client, db):
    csrf, token = fresh_tokens(client)
    base = {
        "csrf_token": csrf,
        "submission_token": token,
        "txn_date": TODAY.isoformat(),
        "account_id": str(acc(db, "5101")),
        "payment_method_id": str(pm_id(db, "LINE Pay")),
    }
    resp = client.post("/txn/new", data={**base, "amount": "abc"}, headers=HX)
    assert TOKEN_INPUT.search(resp.text).group(1) == token
    resp = client.post("/txn/new", data={**base, "amount": "0"}, headers=HX)
    assert TOKEN_INPUT.search(resp.text).group(1) == token
    resp = client.post("/txn/new", data={**base, "amount": "12000"}, headers=HX)
    assert resp.status_code == 409
    assert hidden(resp.text, "submission_token") == token
    resp = client.post("/txn/new", data={**base, "amount": "12000", "action": "edit"}, headers=HX)
    assert TOKEN_INPUT.search(resp.text).group(1) == token
    resp = client.post("/txn/new", data={**base, "amount": "12000", "confirmed": "V10"}, headers=HX)
    assert resp.status_code == 200
    assert "已儲存" in resp.text


def test_replay_with_new_merchant_name_has_no_side_effects(client, db, session_factory):
    """已用過的權杖重送時換成全新的商家名稱：直接回傳第一次的交易，
    不建立商家、不新增交易、不新增稽核紀錄。"""
    csrf, token = fresh_tokens(client)
    data = {
        "csrf_token": csrf,
        "submission_token": token,
        "txn_date": TODAY.isoformat(),
        "amount": "85",
        "account_id": str(acc(db, "5101")),
        "payment_method_id": str(pm_id(db, "LINE Pay")),
        "merchant_name": "7-11",
        "action": "next",
    }
    first = client.post("/txn/new", data=data, headers=HX)
    assert first.status_code == 200
    txn = only_txn(session_factory)
    assert merchants_in_db(session_factory) == [(1, "7-11")]

    second = client.post(
        "/txn/new", data={**data, "merchant_name": "一個全新的不存在商家"}, headers=HX
    )
    assert second.status_code == 200
    assert 'data-replayed="true"' in second.text
    assert f'data-txn-id="{txn.id}"' in second.text
    assert txn_count(session_factory) == 1
    with session_factory() as s:
        assert s.scalar(select(func.count()).select_from(AuditLog)) == 1
    assert merchants_in_db(session_factory) == [(1, "7-11")]
