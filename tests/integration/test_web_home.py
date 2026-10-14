# ruff: noqa: F811  （共用 test_web_txn_new.py 的 fixture：pytest 依參數名稱注入，不是重複定義）
"""首頁入口與快速記帳的「常用」科目（P2 Step 11，UF01，Web 層）；
首頁帳戶餘額與帳平檢查警示（P3-1，規格 8.1、F26）。"""

import re
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from app.models import Account, Transaction
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
from tests.ledger_helpers import TODAY, acc, insert_raw_transaction, post_manual

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


def test_home_has_only_the_expected_entries(client):
    html = client.get("/").text
    hrefs = re.findall(r'<a [^>]*href="([^"]+)"', html)
    # 「/budgets」出現兩次：導覽的「預算」＋空狀態的「前往設定」
    assert sorted(hrefs) == ["/budgets", "/budgets", "/settings", "/txn", "/txn/new"]
    assert "/merchants" not in html


def test_home_settings_entry(client):
    html = client.get("/").text
    assert re.search(r'<a class="button-link" href="/settings">設定</a>', html)


def test_home_requires_login_still(anon):
    resp = anon.get("/")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login?next=%2F"


# --- P3-1 帳戶餘額與帳平檢查 -------------------------------------------------------------


def balance_rows(html: str) -> list[str]:
    """每列帳戶的可見文字（去掉標籤、合併空白）。"""
    rows = re.findall(r'<li class="home-balance"[^>]*>(.*?)</li>', html, re.S)
    return [" ".join(re.sub(r"<[^>]+>", " ", row).split()) for row in rows]


def test_home_shows_balances_with_thousands_and_debt(client, db):
    post_manual(db, TODAY, ("1102", 12345), ("4201", -12345), confirmed={"V10"})
    post_manual(db, TODAY, ("5201", 3200), ("2101", -3200))
    rows = balance_rows(client.get("/").text)
    assert rows == ["現金 0", "郵局 12,345", "聯邦活存 0", "聯邦信用卡 欠款 3,200"]


def test_home_negative_balances_shown_as_is(client, db):
    post_manual(db, TODAY, ("5101", 200), ("1101", -200))
    post_manual(db, TODAY, ("2101", 300), ("1103", -300))
    rows = balance_rows(client.get("/").text)
    assert "現金 -200" in rows
    assert "聯邦信用卡 -300" in rows


def test_home_inactive_account_marked(client, db):
    account = db.get(Account, acc(db, "1101"))
    account.is_active = False
    db.commit()
    assert "現金 已停用 0" in balance_rows(client.get("/").text)


def test_home_has_no_balance_total(client, db):
    post_manual(db, TODAY, ("1102", 500), ("4201", -500))
    html = client.get("/").text
    for word in ("合計", "總資產", "淨資產"):
        assert word not in html


def test_home_integrity_ok_is_a_small_line(client):
    html = client.get("/").text
    assert "帳平檢查：正常" in html
    assert 'role="alert"' not in html


def test_home_integrity_problem_shows_alert_with_links(client, db):
    bad = insert_raw_transaction(db, "2026-10-01", ("5101", 100), ("1101", -90))
    gone = insert_raw_transaction(db, "2026-10-02", ("5101", 100), ("1101", -1), deleted=True)
    html = client.get("/").text
    assert 'role="alert"' in html
    assert "帳平檢查發現 2 筆交易有問題" in html
    assert f'<a href="/txn/{bad}">交易 #{bad}</a>：借貸不平衡</li>' in html
    assert f'<a href="/txn/{gone}">交易 #{gone}</a>：借貸不平衡（已刪除）</li>' in html
    assert "帳平檢查：正常" not in html


def _empty_transaction(db) -> int:
    txn = Transaction(txn_date="2026-10-01", txn_type="MANUAL")
    db.add(txn)
    db.commit()
    return txn.id


@pytest.mark.parametrize(
    "make",
    [
        lambda db: insert_raw_transaction(db, "2026-10-01", ("5101", 100), ("1101", -90)),
        lambda db: insert_raw_transaction(
            db, "2026-10-01", ("5101", 100), ("1101", -90), deleted=True
        ),
        lambda db: insert_raw_transaction(
            db, "2026-10-01", ("5101", 100), ("1101", -90), status="DRAFT"
        ),
        lambda db: insert_raw_transaction(db, "2026-10-01", ("5101", 100)),
        lambda db: insert_raw_transaction(db, "2026-10-01", ("5100", 100), ("1101", -100)),
        lambda db: insert_raw_transaction(
            db, "2026-10-01", ("5101", 100), ("1101", -90), txn_type="EXPENSE"
        ),
        _empty_transaction,
    ],
    ids=["unbalanced", "deleted", "draft", "single", "non-leaf", "expense-unbalanced", "empty"],
)
def test_integrity_links_open_transaction_detail(client, db, make):
    """首頁警示的每個連結都要能進入明細（含已刪除），不能 500。"""
    txn_id = make(db)
    html = client.get("/").text
    assert f'href="/txn/{txn_id}"' in html
    resp = client.get(f"/txn/{txn_id}")
    assert resp.status_code == 200
    assert f"#{txn_id}" in resp.text or str(txn_id) in resp.text


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


# --- P3-2B 信用卡區塊 ---------------------------------------------------------------------


def card_text(html: str) -> str:
    match = re.search(r'<li class="home-card"[^>]*>(.*?)</li>', html, re.S)
    assert match, "no card block"
    return " ".join(re.sub(r"<[^>]+>", " ", match.group(1)).split())


def test_home_card_shows_amount_due_and_due_date(client, db):
    post_manual(db, TODAY, ("5201", 3200), ("2101", -3200))
    html = client.get("/").text
    # 凍結今天 2026-10-15：結帳日當天的消費算本期；繳款日 11/03，還有 19 天
    assert card_text(html) == "聯邦信用卡 本期應繳 3,200 繳款日 2026-11-03（還有 19 天）"
    assert "金額依記帳資料推算，可能與銀行帳單不同（入帳日、分期、首期期初餘額）。" in html


def test_home_card_zero_due_hides_remaining_days(client):
    text = card_text(client.get("/").text)
    assert text == "聯邦信用卡 本期應繳 0 繳款日 2026-11-03"
    assert "還有" not in text


def test_home_card_overpaid(client, db):
    from app.services import transactions
    from app.services.entries import CardPaymentForm

    post_manual(db, TODAY - timedelta(days=20), ("5201", 500), ("2101", -500))
    transactions.record_card_payment(
        db, CardPaymentForm(TODAY, 800, acc(db, "2101"), acc(db, "1103"))
    )
    text = card_text(client.get("/").text)
    assert "本期應繳 0" in text and "已多繳 300" in text and "還有" not in text


def test_home_card_due_today(client, db, monkeypatch):
    from tests.ledger_helpers import freeze_today

    post_manual(db, date(2026, 10, 10), ("5201", 700), ("2101", -700))
    freeze_today(monkeypatch, date(2026, 11, 3))
    assert "繳款日 2026-11-03（今天截止）" in card_text(client.get("/").text)


def test_home_card_overdue_message(client, db, monkeypatch):
    from tests.ledger_helpers import freeze_today

    post_manual(db, date(2026, 10, 10), ("5201", 700), ("2101", -700))
    freeze_today(monkeypatch, date(2026, 11, 10))
    text = card_text(client.get("/").text)
    assert "依目前記帳資料，繳款日已過 7 天，尚有 700 元本期應繳。" in text
    assert "還有" not in text and "今天截止" not in text


def test_home_card_no_overdue_message_when_nothing_due(client, monkeypatch):
    from tests.ledger_helpers import freeze_today

    freeze_today(monkeypatch, date(2026, 11, 10))
    assert "已過" not in card_text(client.get("/").text)


def test_home_card_without_profile_links_to_settings(client, db):
    from app.models import CreditCardProfile

    db.delete(db.get(CreditCardProfile, acc(db, "2101")))
    db.commit()
    html = client.get("/").text
    assert card_text(html) == "聯邦信用卡 尚未設定結帳日 前往設定"
    assert '<a href="/settings">前往設定</a>' in html
    assert "本期應繳" not in html


def test_home_card_section_hidden_without_visible_cards(client, db):
    db.get(Account, acc(db, "2101")).show_on_home = False
    db.commit()
    html = client.get("/").text
    assert "home-cards" not in html and "金額依記帳資料推算" not in html


def test_home_inactive_card_marked(client, db):
    db.get(Account, acc(db, "2101")).is_active = False
    db.commit()
    assert "聯邦信用卡 已停用" in card_text(client.get("/").text)


def test_settings_change_shows_on_home(client, db):
    """設定頁儲存後，首頁用新的結帳日／繳款日重算。"""
    post_manual(db, date(2026, 10, 10), ("5201", 700), ("2101", -700))
    csrf = csrf_from(client, "/settings")
    resp = client.post(
        f"/settings/cards/{acc(db, '2101')}",
        data={"csrf_token": csrf, "closing_day": "10", "due_day": "25", "autopay_account_id": ""},
    )
    assert resp.status_code == 303
    assert "繳款日 2026-10-25" in card_text(client.get("/").text)


# --- 首頁預算區塊（P3-3B）----------------------------------------------------------------


def test_home_budget_nav_entry(client):
    html = client.get("/").text
    assert re.search(r'<a class="button-link" href="/budgets">預算</a>', html)


def test_home_budget_empty_state(client):
    html = client.get("/").text
    assert "尚未設定預算" in html
    assert re.search(r'<a href="/budgets">前往設定</a>', html)
    assert "budget-card" not in html


def test_home_budget_section_between_balances_and_cards(client):
    html = client.get("/").text
    assert html.index("帳戶餘額") < html.index("本月預算") < html.index("home-cards-title")


def _set(db, code, amount, month="2026-10"):
    from app.services import budgets

    budgets.set_budget(db, acc(db, code), month, amount)


def test_home_budget_card_shows_progress(client, db):
    _set(db, "5100", 1000)
    post_manual(db, TODAY, ("5101", 300), ("1101", -300))
    html = client.get("/").text
    assert "尚未設定預算" not in html
    card = re.search(r'<div class="budget-card"[^>]*data-state="(\w+)"', html)
    assert card and card.group(1) == "ok"
    assert "30%" in html and '剩餘 <span class="num">700</span>' in html


@pytest.mark.parametrize(
    ("spent", "state", "text"),
    [(800, "warn", "接近上限"), (1000, "used_up", "已用完"), (1200, "over", "已超支")],
)
def test_home_budget_states(client, db, spent, state, text):
    _set(db, "5100", 1000)
    post_manual(db, TODAY, ("5101", spent), ("1101", -spent))
    html = client.get("/").text
    assert f'data-state="{state}"' in html and text in html


def test_home_budget_over_shows_over_amount(client, db):
    _set(db, "5100", 1000)
    post_manual(db, TODAY, ("5101", 1200), ("1101", -1200))
    assert '超支 <span class="num">200</span> 元' in client.get("/").text


def test_home_budget_invalid_alert_pct_shows_error_only_there(client, db):
    from app.models import Setting

    _set(db, "5100", 1000)
    db.get(Setting, "budget_alert_pct").value = "abc"
    db.commit()
    resp = client.get("/")
    assert resp.status_code == 200
    assert 'data-code="BUDGET_SETTING"' in resp.text
    assert "budget-card" not in resp.text
    assert "帳戶餘額" in resp.text
