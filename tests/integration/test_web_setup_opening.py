# ruff: noqa: F811  （共用 test_web_txn_new.py 的 fixture：pytest 依參數名稱注入，不是重複定義）
"""/setup/opening 期初餘額精靈（P3-7；規格 F04、7.3、T13）。

Web 層只驗證「接得上 P1」：登入、CSRF、欄位解析、錯誤／確認的 HTTP 對應、重複送出不會建立第二筆。
借貸與驗證規則本身由 P1 的測試涵蓋；這裡確認經過 Web 之後帳務結果不變。
"""

import re

from sqlalchemy import func, select

from app.models import AuditLog, Posting, Transaction
from app.services.ledger_queries import balance, net_worth
from tests.integration.test_web_txn_new import (  # noqa: F401
    _today,
    anon,
    client,
    csrf_from,
    db,
    log_dir,
    session_factory,
    user_id,
)
from tests.ledger_helpers import acc


def submit(client, db, amounts: dict[str, str], *, txn_date="2026-10-15", **extra):
    """amounts：{科目代碼: 輸入文字}。送出頁面上所有可輸入的帳戶（沒填的送空白，同手機表單）。"""
    csrf = csrf_from(client, "/setup/opening")
    html = client.get("/setup/opening").text
    ids = [int(i) for i in re.findall(r'name="account_id" value="(\d+)"', html)]
    by_id = {acc(db, code): text for code, text in amounts.items()}
    data = {
        "csrf_token": csrf,
        "txn_date": txn_date,
        "account_id": [str(i) for i in ids],
        "balance": [by_id.get(i, "") for i in ids],
        **extra,
    }
    return client.post("/setup/opening", data=data)


def opening_txns(db):
    db.expire_all()
    return list(db.scalars(select(Transaction).where(Transaction.txn_type == "OPENING")))


def total_postings(db):
    db.expire_all()
    return int(db.scalar(select(func.count(Posting.id))) or 0)


# --- 登入與 CSRF -----------------------------------------------------------------------------


def test_requires_login(anon):
    resp = anon.get("/setup/opening")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login?next=%2Fsetup%2Fopening"


def test_post_without_csrf_is_rejected_and_writes_nothing(client, db):
    resp = client.post(
        "/setup/opening", data={"txn_date": "2026-10-15", "account_id": [], "balance": []}
    )
    assert resp.status_code == 403
    assert opening_txns(db) == []


# --- 頁面 ------------------------------------------------------------------------------------


def test_page_offers_an_input_per_eligible_account(client, db):
    html = client.get("/setup/opening").text
    ids = {int(i) for i in re.findall(r'name="account_id" value="(\d+)"', html)}
    assert ids == {acc(db, c) for c in ("1101", "1102", "1103", "1201", "1301", "2101")}
    assert 'name="txn_date"' in html and 'value="2026-10-15"' in html
    assert "期初權益" in html  # 說明文字：對方科目由系統處理
    assert 'name="csrf_token"' in html


# --- 成功 ------------------------------------------------------------------------------------


def test_save_creates_one_balanced_opening_and_balances_are_correct(client, db):
    resp = submit(client, db, {"1101": "2,000", "1102": "50000", "2101": "3000"})
    assert resp.status_code == 303 and resp.headers["location"] == "/setup/opening?saved=1"

    txns = opening_txns(db)
    assert len(txns) == 1 and txns[0].status == "POSTED"
    amounts = sorted(p.amount for p in txns[0].postings)
    assert sum(amounts) == 0  # 借方正、貸方負，總和為 0
    # 現金 +2000、郵局 +50000、信用卡 −3000（負債貸方）、期初權益 −49000
    assert amounts == [-49000, -3000, 2000, 50000]
    assert balance(db, acc(db, "1101")) == 2000
    assert balance(db, acc(db, "1102")) == 50000
    assert balance(db, acc(db, "2101")) == 3000  # 負債：欠款為正
    assert net_worth(db) == 49000  # T13
    assert balance(db, acc(db, "3101")) == 49000  # 期初權益（權益正常方向為貸方）

    audits = db.scalars(select(AuditLog).where(AuditLog.entity == "transaction")).all()
    assert len(audits) == 1 and audits[0].user_id == user_id(db)


def test_after_saving_the_page_shows_balances_and_no_input_for_those_accounts(client, db):
    submit(client, db, {"1101": "2000"})
    html = client.get("/setup/opening?saved=1").text
    assert "已儲存期初餘額" in html
    assert f'name="account_id" value="{acc(db, "1101")}"' not in html  # 已有期初：不再提供輸入
    assert "已建立期初的帳戶" in html and "2,000" in html
    assert f'name="account_id" value="{acc(db, "1102")}"' in html  # 其他帳戶仍可輸入


def test_negative_balance_is_allowed(client, db):
    resp = submit(client, db, {"1102": "-500"})
    assert resp.status_code == 303
    assert balance(db, acc(db, "1102")) == -500


def test_remaining_accounts_can_be_entered_later_without_touching_the_first(client, db):
    submit(client, db, {"1101": "2000"})
    resp = submit(client, db, {"1102": "7000"})
    assert resp.status_code == 303
    assert len(opening_txns(db)) == 2
    assert balance(db, acc(db, "1101")) == 2000 and balance(db, acc(db, "1102")) == 7000


# --- 防重複 ----------------------------------------------------------------------------------


def test_submitting_the_same_account_twice_is_rejected_and_creates_no_second_opening(client, db):
    assert submit(client, db, {"1101": "2000"}).status_code == 303
    before = total_postings(db)
    # 模擬「重送」：直接帶著已經有期初的帳戶再送一次（畫面已不提供這個欄位，等同手動竄改或舊表單）
    csrf = csrf_from(client, "/setup/opening")
    resp = client.post(
        "/setup/opening",
        data={
            "csrf_token": csrf,
            "txn_date": "2026-10-15",
            "account_id": [str(acc(db, "1101"))],
            "balance": ["2000"],
        },
    )
    assert resp.status_code == 422
    assert "已經有期初餘額" in resp.text
    assert len(opening_txns(db)) == 1 and total_postings(db) == before
    assert balance(db, acc(db, "1101")) == 2000


# --- 錯誤 ------------------------------------------------------------------------------------


def test_non_integer_amount_is_a_field_error_and_keeps_the_input(client, db):
    resp = submit(client, db, {"1101": "12.5", "1102": "300"})
    assert resp.status_code == 422
    assert "請輸入整數金額" in resp.text
    assert 'value="12.5"' in resp.text and 'value="300"' in resp.text
    assert opening_txns(db) == []


def test_all_blank_is_rejected_without_writing(client, db):
    resp = submit(client, db, {})
    assert resp.status_code == 422
    assert "請至少填寫" in resp.text
    assert opening_txns(db) == []


def test_missing_date_is_rejected(client, db):
    resp = submit(client, db, {"1101": "100"}, txn_date="")
    assert resp.status_code == 422 and "請輸入期初日期" in resp.text
    assert opening_txns(db) == []


def test_tampered_account_that_is_not_an_asset_or_liability_is_rejected(client, db):
    csrf = csrf_from(client, "/setup/opening")
    resp = client.post(
        "/setup/opening",
        data={
            "csrf_token": csrf,
            "txn_date": "2026-10-15",
            "account_id": [str(acc(db, "5101"))],
            "balance": ["100"],
        },
    )
    assert resp.status_code == 422
    assert opening_txns(db) == []


def test_mismatched_form_lists_are_rejected(client, db):
    csrf = csrf_from(client, "/setup/opening")
    resp = client.post(
        "/setup/opening",
        data={
            "csrf_token": csrf,
            "txn_date": "2026-10-15",
            "account_id": ["1", "2"],
            "balance": ["5"],
        },
    )
    assert resp.status_code == 422
    assert opening_txns(db) == []


def test_future_date_needs_confirmation_then_saves(client, db):
    resp = submit(client, db, {"1101": "100"}, txn_date="2026-10-20")
    assert resp.status_code == 409 and "仍要儲存" in resp.text
    assert opening_txns(db) == []
    codes = re.search(r'name="confirmed" value="([^"]+)"', resp.text).group(1)
    resp = submit(client, db, {"1101": "100"}, txn_date="2026-10-20", confirmed=codes)
    assert resp.status_code == 303
    assert len(opening_txns(db)) == 1


# --- 首頁入口 --------------------------------------------------------------------------------


def test_home_prompts_until_an_opening_exists_then_shows_the_balances(client, db):
    assert "data-opening-prompt" in client.get("/").text
    submit(client, db, {"1101": "2000", "1102": "50000"})
    html = client.get("/").text
    assert "data-opening-prompt" not in html and "/setup/opening" not in html
    assert "50,000" in html and "2,000" in html


# --- 永遠可到達的入口（settings）-----------------------------------------------------------------


def test_settings_always_links_to_the_opening_page(client, db):
    def link(html):
        return re.search(r'<a href="/setup/opening">前往期初餘額</a>', html)

    assert link(client.get("/settings").text)
    submit(client, db, {"1101": "2000"})  # 首頁提示已消失，settings 入口仍在
    assert "data-opening-prompt" not in client.get("/").text
    assert link(client.get("/settings").text)
    assert client.get("/setup/opening").status_code == 200  # 而且連過去仍能補其他帳戶
