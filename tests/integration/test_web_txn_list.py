# ruff: noqa: F811  （共用 test_web_txn_new.py 的 fixture：pytest 依參數名稱注入，不是重複定義）
"""GET /txn 交易列表與搜尋（P2 Step 3，Web 層）。

金額、合計的語意在 test_search.py 驗證；這裡驗證：登入、預設本月、query string、
HTMX 局部回應、422、分頁連結保留條件、空狀態、搜尋不建立商家。
"""

import re
from datetime import date
from urllib.parse import parse_qs, urlparse

from sqlalchemy import func, select

from app.models import Merchant, PaymentMethod
from app.services import transactions
from app.services.entries import ExpenseForm, TransferForm
from tests.integration.test_web_txn_new import (  # noqa: F401
    HX,
    _today,
    anon,
    client,
    db,
    log_dir,
    session_factory,
)
from tests.ledger_helpers import TODAY, acc


def expense(db, amount, day=TODAY, merchant_id=None, description=None) -> int:
    pm = db.scalar(select(PaymentMethod.id).where(PaymentMethod.name == "LINE Pay"))
    form = ExpenseForm(
        txn_date=day,
        amount=amount,
        account_id=acc(db, "5101"),
        payment_method_id=pm,
        merchant_id=merchant_id,
        description=description,
    )
    return transactions.record_expense(db, form, confirmed=["V10", "V11", "V12"]).transaction_id


def row_ids(html: str) -> list[int]:
    return [int(x) for x in re.findall(r'<li class="txn-row" data-txn-id="(\d+)">', html)]


def summary(html: str) -> tuple[int, int, int]:
    m = re.search(
        r'data-count="(-?\d+)" data-expense-net="(-?\d+)" data-income-total="(-?\d+)"', html
    )
    assert m, "找不到搜尋結果摘要"
    return tuple(int(x) for x in m.groups())


def field_value(html: str, name: str) -> str:
    return re.search(rf'<input id="{name}"[^>]*?value="([^"]*)"', html, re.S).group(1)


def test_requires_login(anon):
    resp = anon.get("/txn")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login?next=%2Ftxn"


def test_default_is_this_month_newest_first(client, db):
    expense(db, 100, day=date(2026, 9, 30))
    a = expense(db, 200, day=date(2026, 10, 1))
    b = expense(db, 300, day=date(2026, 10, 15))
    resp = client.get("/txn")
    assert resp.status_code == 200
    assert "<html" in resp.text
    assert field_value(resp.text, "date_from") == "2026-10-01"
    assert field_value(resp.text, "date_to") == "2026-10-31"
    assert row_ids(resp.text) == [b, a]
    assert summary(resp.text) == (2, 500, 0)
    assert "共 2 筆｜支出淨額 500 元" in resp.text


def test_cleared_dates_mean_all_time(client, db):
    old = expense(db, 100, day=date(2026, 9, 30))
    new = expense(db, 200)
    resp = client.get("/txn?date_from=&date_to=")
    assert row_ids(resp.text) == [new, old]


def test_htmx_returns_fragment_with_form_and_results(client, db):
    expense(db, 100)
    resp = client.get("/txn?date_from=2026-10-01&date_to=2026-10-31", headers=HX)
    assert resp.status_code == 200
    assert "<html" not in resp.text
    assert 'hx-get="/txn"' in resp.text
    assert 'hx-push-url="true"' in resp.text
    assert len(row_ids(resp.text)) == 1


def test_row_shows_badge_merchant_description_and_positive_amount(client, db):
    db.add(Merchant(name="7-11"))
    db.commit()
    merchant_id = db.scalar(select(Merchant.id))
    expense(db, 1234, merchant_id=merchant_id, description="午餐")
    pm_ids = (acc(db, "1102"), acc(db, "1103"))
    transactions.record_transfer(db, TransferForm(TODAY, 5000, *pm_ids), confirmed=["V10"])
    html = client.get("/txn").text
    assert '<span class="badge" data-type="EXPENSE">支出</span>' in html
    assert '<span class="badge" data-type="TRANSFER">轉帳</span>' in html
    assert '<span class="txn-amount">1,234</span>' in html
    assert '<span class="txn-amount">5,000</span>' in html
    assert '<span class="txn-merchant">7-11</span> · 午餐' in html
    assert summary(html) == (2, 1234, 0)  # 轉帳不算支出


def test_merchant_filter_and_unknown_merchant(client, db, session_factory):
    db.add(Merchant(name="7-11"))
    db.commit()
    mine = expense(db, 85, merchant_id=db.scalar(select(Merchant.id)))
    expense(db, 60)
    resp = client.get("/txn", params={"date_from": "", "date_to": "", "merchant": "７－１１"})
    assert row_ids(resp.text) == [mine]
    assert '<details class="voucher advanced" open>' in resp.text

    resp = client.get("/txn", params={"merchant": "不存在的店"})
    assert resp.status_code == 200
    assert row_ids(resp.text) == []
    assert "找不到商家「不存在的店」。" in resp.text
    with session_factory() as s:
        assert s.scalar(select(func.count()).select_from(Merchant)) == 1


def test_amount_filter(client, db):
    expense(db, 99)
    mid = expense(db, 150)
    expense(db, 501)
    resp = client.get("/txn", params={"min_amount": "100", "max_amount": "500"})
    assert row_ids(resp.text) == [mid]


def test_invalid_conditions_are_422(client):
    resp = client.get(
        "/txn", params={"date_from": "2026-10-31", "date_to": "2026-10-01"}, headers=HX
    )
    assert resp.status_code == 422
    assert "結束日期不能早於開始日期" in resp.text
    assert 'aria-invalid="true" aria-describedby="date_to-error"' in resp.text
    assert "txn-summary" not in resp.text

    resp = client.get("/txn", params={"min_amount": "500", "max_amount": "100"})
    assert resp.status_code == 422
    assert "最高金額不能小於最低金額" in resp.text
    assert '<details class="voucher advanced" open>' in resp.text

    resp = client.get("/txn", params={"date_from": "2026/10/01", "min_amount": "abc"})
    assert resp.status_code == 422
    assert "日期格式不正確" in resp.text
    assert "請輸入整數金額" in resp.text


def test_pagination_links_keep_conditions(client, db):
    made = [expense(db, 10 + i, day=date(2026, 10, 2)) for i in range(51)]
    resp = client.get("/txn", params={"date_from": "2026-10-01", "date_to": "", "min_amount": "10"})
    assert len(row_ids(resp.text)) == 50
    assert summary(resp.text)[0] == 51
    assert "第 1 / 2 頁" in resp.text
    next_url = re.search(r'<a href="([^"]+)" hx-get="[^"]+"[^>]*>下一頁</a>', resp.text).group(1)
    next_url = next_url.replace("&amp;", "&")
    query = parse_qs(urlparse(next_url).query, keep_blank_values=True)
    assert query == {
        "date_from": ["2026-10-01"],
        "date_to": [""],
        "merchant": [""],
        "min_amount": ["10"],
        "max_amount": [""],
        "page": ["2"],
    }
    page2 = client.get(next_url, headers=HX)
    assert row_ids(page2.text) == [made[0]]
    assert summary(page2.text)[0] == 51
    assert "上一頁" in page2.text and "下一頁" not in page2.text


def test_empty_states(client):
    assert "本月還沒有交易。" in client.get("/txn").text
    resp = client.get("/txn", params={"date_from": "2026-01-01", "date_to": "2026-01-31"})
    assert "找不到符合條件的交易。" in resp.text
    assert summary(resp.text) == (0, 0, 0)


def test_pages_link_to_each_other(client):
    assert '<a href="/txn/new">記一筆</a>' in client.get("/txn").text
    assert '<a href="/txn">交易列表</a>' in client.get("/txn/new").text
