# ruff: noqa: F811  （共用 test_web_txn_new.py 的 fixture：pytest 依參數名稱注入，不是重複定義）
"""首頁入口與快速記帳的「常用」科目（P2 Step 11，UF01，Web 層）。"""

import re

from sqlalchemy import select

from app.models import Transaction
from tests.integration.test_web_txn_edit import edit
from tests.integration.test_web_txn_new import (  # noqa: F401
    HX,
    _today,
    anon,
    client,
    csrf_from,
    db,
    log_dir,
    select_block,
    selected_value,
    session_factory,
    submit,
)
from tests.ledger_helpers import acc

# --- 首頁 --------------------------------------------------------------------------------


def test_home_has_quick_entry_and_list_links(client):
    html = client.get("/").text
    assert re.search(r'<a class="button-link primary" href="/txn/new">快速記一筆</a>', html)
    assert re.search(r'<a class="button-link" href="/txn">交易列表</a>', html)


def test_home_keeps_login_info_and_logout(client):
    html = client.get("/").text
    assert "已登入：<strong>bo</strong>" in html
    assert '<form method="post" action="/logout">' in html
    assert re.search(r'name="csrf_token" value="[^"]+"', html)


def test_home_has_only_the_two_entries(client):
    html = client.get("/").text
    hrefs = re.findall(r'<a [^>]*href="([^"]+)"', html)
    assert sorted(hrefs) == ["/txn", "/txn/new"]
    assert "/merchants" not in html


def test_home_requires_login_still(anon):
    resp = anon.get("/")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login?next=%2F"


# --- 快速記帳主科目 ----------------------------------------------------------------------


def account_select(client) -> str:
    return select_block(client.get("/txn/new").text, "account_id")


def groups(select_html: str) -> list[tuple[str | None, list[str]]]:
    """[(optgroup 標籤, [選項文字])]；請選擇科目那一項不算。"""
    result: list[tuple[str | None, list[str]]] = []
    for label, body in re.findall(r'<optgroup label="([^"]*)">(.*?)</optgroup>', select_html, re.S):
        result.append((label, re.findall(r"<option value=\"\d+\"[^>]*>([^<]*)</option>", body)))
    return result


def option_ids(select_html: str) -> list[str]:
    return re.findall(r'<option value="(\d+)"', select_html)


def spend(client, db, code: str, **extra):
    return submit(client, db, account_id=acc(db, code), **extra)


def test_no_usage_has_no_frequent_group(client):
    labels = [label for label, _ in groups(account_select(client))]
    assert "常用" not in labels
    assert labels[0] == "餐飲"


def test_used_accounts_go_to_frequent_group_with_parent_hint(client, db):
    spend(client, db, "5201")
    spend(client, db, "5101")
    spend(client, db, "5101")
    first = groups(account_select(client))[0]
    assert first == ("常用", ["正餐（餐飲）", "網購（購物）"])


def test_each_account_appears_once_in_the_select(client, db):
    spend(client, db, "5101")
    spend(client, db, "5303")
    ids = option_ids(account_select(client))
    assert len(ids) == len(set(ids))
    assert str(acc(db, "5101")) in ids and str(acc(db, "5303")) in ids


def test_only_frequent_group_has_parent_hint(client, db):
    spend(client, db, "5101")
    other = [(label, opts) for label, opts in groups(account_select(client)) if label != "常用"]
    for _, options in other:
        assert not any("（" in option for option in options)


def test_save_and_next_reranks_by_latest_usage(client, db):
    spend(client, db, "5101")
    resp = spend(client, db, "5201")
    first = groups(select_block(resp.text, "account_id"))[0]
    assert first == ("常用", ["正餐（餐飲）", "網購（購物）"])  # 同次數：沿用原順序
    resp = spend(client, db, "5201")
    first = groups(select_block(resp.text, "account_id"))[0]
    assert first == ("常用", ["網購（購物）", "正餐（餐飲）"])


def test_validation_error_rerender_keeps_selected_account(client, db):
    spend(client, db, "5201")
    resp = spend(client, db, "5201", amount="abc")
    assert resp.status_code == 422
    assert selected_value(resp.text, "account_id") == str(acc(db, "5201"))
    assert groups(select_block(resp.text, "account_id"))[0][0] == "常用"


def test_fee_and_interest_selectors_unchanged(client, db):
    spend(client, db, "5501")
    html = client.get("/txn/new").text
    for name in ("fee_account_id", "interest_account_id"):
        block = select_block(html, name)
        assert "常用" not in block
        assert "（" not in block
        assert groups(block)[0][0] == "餐飲"


def test_transfer_fee_selector_unchanged(client, db):
    spend(client, db, "5501")
    html = client.get("/txn/new", params={"type": "transfer"}).text
    assert "常用" not in select_block(html, "fee_account_id")


def test_edit_form_account_order_unchanged(client, db, session_factory):
    spend(client, db, "5201")
    with session_factory() as s:
        txn_id = s.scalar(select(Transaction.id))
    block = select_block(client.get(f"/txn/{txn_id}/edit").text, "account_id")
    assert "常用" not in block
    assert groups(block)[0][0] == "餐飲"


def test_modified_expense_moves_usage_to_the_new_account(client, db, session_factory):
    spend(client, db, "5101")
    with session_factory() as s:
        txn_id = s.scalar(select(Transaction.id))
    resp = edit(client, db, txn_id, account_id=str(acc(db, "5201")))
    assert resp.status_code == 200
    assert groups(account_select(client))[0] == ("常用", ["網購（購物）"])


def test_deleted_expense_does_not_count_and_restored_does(client, db, session_factory):
    spend(client, db, "5101")
    with session_factory() as s:
        txn_id = s.scalar(select(Transaction.id))
    token = csrf_from(client, "/txn/new")
    client.post(f"/txn/{txn_id}/delete", data={"csrf_token": token}, headers=HX)
    assert "常用" not in account_select(client)
    client.post(f"/txn/{txn_id}/restore", data={"csrf_token": token}, headers=HX)
    assert groups(account_select(client))[0] == ("常用", ["正餐（餐飲）"])


def test_run_summary_unaffected_by_ranking(client, db):
    spend(client, db, "5101")
    resp = spend(client, db, "5201")
    assert 'data-run-count="2"' in resp.text
    assert "已儲存：網購 120 元" in " ".join(resp.text.split())
