# ruff: noqa: F811  （共用 test_web_txn_new.py 的 fixture：pytest 依參數名稱注入，不是重複定義）
"""/settings 信用卡設定頁（P3-2B；規格 F07、7.3、M07）。"""

import re

import pytest

from app.models import CreditCardProfile
from app.services import credit_cards
from tests.integration.test_web_txn_new import (  # noqa: F401
    _today,
    anon,
    client,
    csrf_from,
    db,
    log_dir,
    session_factory,
)
from tests.ledger_helpers import acc


def profile(db, code="2101"):
    db.expire_all()
    p = db.get(CreditCardProfile, acc(db, code))
    return None if p is None else (p.closing_day, p.due_day, p.autopay_account_id, p.credit_limit)


def save(client, db, *, code="2101", closing="15", due="3", autopay="", **extra):
    csrf = csrf_from(client, "/settings")
    data = {
        "csrf_token": csrf,
        "closing_day": closing,
        "due_day": due,
        "autopay_account_id": autopay,
        **extra,
    }
    return client.post(f"/settings/cards/{acc(db, code)}", data=data)


def input_value(html: str, name: str, card_id: int) -> str:
    match = re.search(rf'<input id="{name}-{card_id}"[^>]*?value="([^"]*)"', html, re.S)
    assert match, name
    return match.group(1)


def autopay_options(html: str, card_id: int) -> list[tuple[str, str]]:
    block = re.search(rf'<select id="autopay-{card_id}".*?</select>', html, re.S).group(0)
    return re.findall(r'<option value="([^"]*)"[^>]*>([^<]*)</option>', block)


def selected(html: str, card_id: int) -> str | None:
    block = re.search(rf'<select id="autopay-{card_id}".*?</select>', html, re.S).group(0)
    match = re.search(r'<option value="([^"]*)" selected>', block)
    return match.group(1) if match else None


# --- 登入與 CSRF -----------------------------------------------------------------------------


def test_settings_requires_login(anon):
    resp = anon.get("/settings")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login?next=%2Fsettings"


def test_post_requires_login(anon, db):
    resp = anon.post(f"/settings/cards/{acc(db, '2101')}", data={"closing_day": "1"})
    assert resp.status_code in (303, 403)
    assert profile(db) == (15, 3, acc(db, "1103"), None)


def test_post_without_csrf_is_rejected(client, db):
    resp = client.post(
        f"/settings/cards/{acc(db, '2101')}",
        data={"closing_day": "20", "due_day": "5", "autopay_account_id": ""},
    )
    assert resp.status_code == 403
    assert profile(db) == (15, 3, acc(db, "1103"), None)


# --- GET ------------------------------------------------------------------------------------


def test_get_shows_card_with_prefilled_values(client, db):
    html = client.get("/settings").text
    card = acc(db, "2101")
    assert "聯邦信用卡" in html
    assert input_value(html, "closing", card) == "15"
    assert input_value(html, "due", card) == "3"
    assert selected(html, card) == str(acc(db, "1103"))
    assert 'href="/"' in html  # 返回首頁


def test_autopay_options_only_eligible_accounts(client, db):
    html = client.get("/settings").text
    options = autopay_options(html, acc(db, "2101"))
    assert [name for _, name in options] == ["不設定", "郵局", "聯邦活存"]


def test_unset_card_shows_empty_form(client, db):
    db.delete(db.get(CreditCardProfile, acc(db, "2101")))
    db.commit()
    html = client.get("/settings").text
    card = acc(db, "2101")
    assert "尚未設定" in html
    assert input_value(html, "closing", card) == ""
    assert selected(html, card) is None


def test_limitation_hint_shown(client):
    assert "過去的週期會重新切分" in client.get("/settings").text


def test_inactive_card_not_listed(client, db):
    from app.models import Account

    db.get(Account, acc(db, "2101")).is_active = False
    db.commit()
    html = client.get("/settings").text
    assert "聯邦信用卡" not in html
    assert "沒有啟用中的信用卡帳戶" in html


# --- POST -----------------------------------------------------------------------------------


def test_save_redirects_and_persists(client, db):
    resp = save(client, db, closing="25", due="10", autopay=str(acc(db, "1102")))
    card = acc(db, "2101")
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/settings?saved={card}"
    assert profile(db) == (25, 10, acc(db, "1102"), None)
    html = client.get(resp.headers["location"]).text
    assert "已儲存" in html
    assert input_value(html, "closing", card) == "25"
    assert selected(html, card) == str(acc(db, "1102"))


def test_save_creates_profile_for_unset_card(client, db):
    db.delete(db.get(CreditCardProfile, acc(db, "2101")))
    db.commit()
    assert save(client, db, closing="31", due="15").status_code == 303
    assert profile(db) == (31, 15, None, None)


def test_save_keeps_credit_limit(client, db):
    credit_cards.set_profile(db, acc(db, "2101"), closing_day=15, due_day=3, credit_limit=60000)
    assert save(client, db, closing="20", due="8").status_code == 303
    assert profile(db) == (20, 8, None, 60000)


def test_save_clears_autopay(client, db):
    assert save(client, db, autopay="").status_code == 303
    assert profile(db)[2] is None


def test_form_cannot_change_credit_limit(client, db):
    save(client, db, closing="20", due="8", credit_limit="999")
    assert profile(db)[3] is None


@pytest.mark.parametrize(
    ("closing", "due"),
    [("0", "3"), ("32", "3"), ("15", "0"), ("15", "32"), ("", "3"), ("15", ""), ("abc", "3")],
)
def test_invalid_days_422_keeps_input_and_data(client, db, closing, due):
    resp = save(client, db, closing=closing, due=due)
    card = acc(db, "2101")
    assert resp.status_code == 422
    assert 'class="field-error"' in resp.text
    assert 'data-code="M07"' in resp.text
    assert input_value(resp.text, "closing", card) == closing
    assert input_value(resp.text, "due", card) == due
    assert profile(db) == (15, 3, acc(db, "1103"), None)


@pytest.mark.parametrize("code", ["1101", "2101"])
def test_invalid_autopay_422(client, db, code):
    resp = save(client, db, autopay=str(acc(db, code)))
    assert resp.status_code == 422
    assert 'data-code="M07"' in resp.text
    assert profile(db) == (15, 3, acc(db, "1103"), None)


def test_garbage_autopay_422(client, db):
    resp = save(client, db, autopay="abc")
    assert resp.status_code == 422
    assert profile(db) == (15, 3, acc(db, "1103"), None)


def test_error_only_marks_the_submitted_card(client, db):
    from app.services import accounts as account_service

    other = account_service.create_account(
        db, code="2102", name="另一張卡", account_class="LIABILITY", subtype="CREDIT_CARD"
    )
    resp = save(client, db, closing="0")
    assert resp.status_code == 422
    assert resp.text.count('class="field-error"') == 1
    assert input_value(resp.text, "closing", other.id) == ""


def test_non_card_account_is_page_level_error(client, db):
    csrf = csrf_from(client, "/settings")
    resp = client.post(
        f"/settings/cards/{acc(db, '1102')}",
        data={"csrf_token": csrf, "closing_day": "15", "due_day": "3", "autopay_account_id": ""},
    )
    assert resp.status_code == 422
    assert 'data-code="NOT_CREDIT_CARD"' in resp.text
    assert db.get(CreditCardProfile, acc(db, "1102")) is None


def test_unknown_card_redirects_back(client, db):
    csrf = csrf_from(client, "/settings")
    resp = client.post(
        "/settings/cards/999999",
        data={"csrf_token": csrf, "closing_day": "15", "due_day": "3", "autopay_account_id": ""},
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/settings"
