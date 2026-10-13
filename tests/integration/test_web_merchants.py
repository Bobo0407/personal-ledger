# ruff: noqa: F811  （共用 test_web_txn_new.py 的 fixture：pytest 依參數名稱注入，不是重複定義）
"""/merchants 商家別名管理（P2 Step 9，Web 層）。

規則本身在 test_merchant_aliases.py 驗證；這裡驗證：登入、CSRF、整頁與 HTMX 局部回應、
422 保留輸入、停用商家的畫面、刪除確認區塊、記帳／列表的商家建議清單、列表頁入口。
"""

import re

import pytest
from sqlalchemy import func, select

from app.models import Merchant, MerchantAlias, Transaction
from tests.integration.test_web_txn_new import (  # noqa: F401
    HX,
    _today,
    anon,
    client,
    csrf_from,
    db,
    field_error_codes,
    input_value,
    log_dir,
    session_factory,
    submit,
)


@pytest.fixture
def shops(db):
    seven = Merchant(name="7-11")
    old = Merchant(name="舊店", is_active=False)
    db.add_all([seven, old])
    db.flush()
    db.add(MerchantAlias(merchant_id=old.id, alias="老店", alias_normalized="老店"))
    db.commit()
    return seven.id, old.id


def post(client, url, htmx=True, **data):
    token = csrf_from(client, "/txn/new")
    return client.post(url, data={"csrf_token": token, **data}, headers=HX if htmx else {})


def aliases(session_factory) -> list[tuple[int, str]]:
    with session_factory() as s:
        return [(a.merchant_id, a.alias) for a in s.scalars(select(MerchantAlias))]


def section(html: str, merchant_id: int) -> str:
    match = re.search(
        rf'<li class="merchant-item" id="merchant-{merchant_id}".*?\n</li>', html, re.S
    )
    assert match, merchant_id
    return match.group(0)


def datalist(html: str) -> list[tuple[str, str | None]]:
    block = re.search(r'<datalist id="merchant-options">(.*?)</datalist>', html, re.S).group(1)
    return [
        (value, label or None)
        for value, label in re.findall(r'<option value="([^"]*)"(?: label="([^"]*)")?>', block)
    ]


# --- 頁面 ----------------------------------------------------------------------------


def test_requires_login(anon):
    resp = anon.get("/merchants", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login?next=%2Fmerchants"


def test_page_lists_merchants_with_inactive_marked(client, shops):
    seven, old = shops
    html = client.get("/merchants").text
    active, inactive = section(html, seven), section(html, old)
    assert "已停用" not in active
    assert 'action="/merchants/%d/aliases"' % seven in active
    assert "已停用" in inactive
    assert "老店" in inactive
    assert '/aliases"' not in inactive  # 沒有新增別名的表單
    assert f'action="/merchants/{old}/aliases/' in inactive  # 但可以刪除既有別名


def test_empty_page(client):
    resp = client.get("/merchants")
    assert resp.status_code == 200
    assert "還沒有商家" in resp.text


def test_txn_list_links_to_merchants(client):
    assert 'href="/merchants"' in client.get("/txn").text


def test_home_page_unchanged(client):
    assert 'href="/merchants"' not in client.get("/").text


# --- 新增 ----------------------------------------------------------------------------


def test_add_alias_htmx_returns_section(client, shops, session_factory):
    seven, _ = shops
    resp = post(client, f"/merchants/{seven}/aliases", alias=" 統一超商 ")
    assert resp.status_code == 200
    assert f'id="merchant-{seven}"' in resp.text
    assert "<html" not in resp.text
    assert "已新增別名「統一超商」" in resp.text
    assert input_value(resp.text, f"alias-{seven}") == ""
    assert (seven, "統一超商") in aliases(session_factory)


def test_add_alias_without_htmx_redirects(client, shops, session_factory):
    seven, _ = shops
    resp = post(client, f"/merchants/{seven}/aliases", htmx=False, alias="Seven")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/merchants"
    assert (seven, "Seven") in aliases(session_factory)


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("  ", "ALIAS_BLANK"),
        ("７－１１", "ALIAS_SAME_AS_NAME"),
        ("舊 店", "ALIAS_NAME_CONFLICT"),
        ("老店", "M06"),
    ],
)
def test_add_alias_rejected_keeps_input(client, shops, session_factory, text, code):
    seven, _ = shops
    before = aliases(session_factory)
    resp = post(client, f"/merchants/{seven}/aliases", alias=text)
    assert resp.status_code == 422
    assert field_error_codes(resp.text, f"alias-{seven}") == [code]
    assert input_value(resp.text, f"alias-{seven}") == text
    assert aliases(session_factory) == before


def test_add_alias_rejected_without_htmx_renders_page(client, shops):
    seven, old = shops
    resp = post(client, f"/merchants/{seven}/aliases", htmx=False, alias="舊店")
    assert resp.status_code == 422
    assert "<html" in resp.text
    assert field_error_codes(resp.text, f"alias-{seven}") == ["ALIAS_NAME_CONFLICT"]
    assert "老店" in section(resp.text, old)


def test_inactive_merchant_cannot_add_alias(client, shops, session_factory):
    _, old = shops
    resp = post(client, f"/merchants/{old}/aliases", alias="老老店")
    assert resp.status_code == 422
    assert "已停用" in resp.text
    assert aliases(session_factory) == [(old, "老店")]


def test_add_alias_unknown_merchant(client, shops):
    resp = post(client, "/merchants/9999/aliases", alias="x")
    assert resp.status_code == 404
    assert resp.headers["HX-Redirect"] == "/merchants"


def test_add_alias_requires_csrf(client, shops, session_factory):
    seven, _ = shops
    resp = client.post(f"/merchants/{seven}/aliases", data={"alias": "x"}, headers=HX)
    assert resp.status_code == 403
    assert aliases(session_factory) == [(shops[1], "老店")]


# --- 刪除 ----------------------------------------------------------------------------


def alias_id(session_factory, text) -> int:
    with session_factory() as s:
        return s.scalar(select(MerchantAlias.id).where(MerchantAlias.alias == text))


def test_delete_is_behind_expandable_confirmation(client, shops):
    _, old = shops
    html = section(client.get("/merchants").text, old)
    assert re.search(r'<details class="alias-delete">\s*<summary>', html)
    assert "確定刪除別名" in html
    assert "confirm(" not in html and "hx-confirm" not in html


def test_delete_inactive_merchant_alias(client, shops, session_factory):
    _, old = shops
    aid = alias_id(session_factory, "老店")
    resp = post(client, f"/merchants/{old}/aliases/{aid}/delete")
    assert resp.status_code == 200
    assert "已刪除別名" in resp.text
    assert "沒有別名" in resp.text
    assert aliases(session_factory) == []
    with session_factory() as s:
        assert s.scalar(select(func.count()).select_from(Merchant)) == 2


def test_delete_without_htmx_redirects(client, shops, session_factory):
    _, old = shops
    aid = alias_id(session_factory, "老店")
    resp = post(client, f"/merchants/{old}/aliases/{aid}/delete", htmx=False)
    assert resp.status_code == 303
    assert aliases(session_factory) == []


def test_delete_alias_of_other_merchant_is_404(client, shops, session_factory):
    seven, old = shops
    aid = alias_id(session_factory, "老店")
    resp = post(client, f"/merchants/{seven}/aliases/{aid}/delete")
    assert resp.status_code == 404
    assert aliases(session_factory) == [(old, "老店")]


def test_delete_requires_csrf(client, shops, session_factory):
    _, old = shops
    aid = alias_id(session_factory, "老店")
    resp = client.post(f"/merchants/{old}/aliases/{aid}/delete", headers=HX)
    assert resp.status_code == 403
    assert aliases(session_factory) == [(old, "老店")]


# --- 記帳與列表的建議清單 ------------------------------------------------------------------


def test_suggestions_in_form_and_list(client, shops):
    seven, _ = shops
    post(client, f"/merchants/{seven}/aliases", alias="統一超商")
    expected = [("7-11", None), ("統一超商", "統一超商 → 7-11")]
    assert datalist(client.get("/txn/new").text) == expected
    assert datalist(client.get("/txn").text) == expected


def test_entering_alias_records_canonical_merchant(client, db, shops, session_factory):
    seven, _ = shops
    post(client, f"/merchants/{seven}/aliases", alias="統一超商")
    resp = submit(client, db, merchant_name="統一 超商")
    assert resp.status_code in (200, 303)
    with session_factory() as s:
        assert s.scalar(select(Transaction.merchant_id)) == seven
        assert s.scalar(select(func.count()).select_from(Merchant)) == 2


def test_search_by_alias_and_delete_alias_keeps_transaction(client, db, shops, session_factory):
    """別名搜尋命中標準商家的交易；刪除別名後交易的 merchant_id、明細、標準名稱搜尋都不變。"""
    seven, _ = shops
    post(client, f"/merchants/{seven}/aliases", alias="統一超商")
    submit(client, db, merchant_name="7-11")
    with session_factory() as s:
        txn_id = s.scalar(select(Transaction.id))

    def search(name):
        return client.get("/txn", params={"merchant": name, "date_from": "", "date_to": ""}).text

    assert f'data-txn-id="{txn_id}"' in search("統一 超商")

    post(client, f"/merchants/{seven}/aliases/{alias_id(session_factory, '統一超商')}/delete")
    with session_factory() as s:
        assert s.scalar(select(Transaction.merchant_id)) == seven
    assert f'data-txn-id="{txn_id}"' in search("7-11")
    assert "找不到商家" in search("統一超商")
    assert re.search(r"<dt>商家</dt><dd data-merchant>7-11</dd>", client.get(f"/txn/{txn_id}").text)


def test_edit_form_has_alias_suggestions(client, db, shops):
    seven, _ = shops
    post(client, f"/merchants/{seven}/aliases", alias="統一超商")
    submit(client, db, merchant_name="統一超商")
    from app.models import Transaction as T

    txn_id = db.scalar(select(T.id))
    html = client.get(f"/txn/{txn_id}/edit").text
    assert ("統一超商", "統一超商 → 7-11") in datalist(html)
    assert input_value(html, "merchant_name") == "7-11"  # 修改頁預填標準名稱（既有行為）
