# ruff: noqa: F811  （共用 test_web_txn_new.py 的 fixture：pytest 依參數名稱注入，不是重複定義）
"""V14 提醒與儲存後預算卡片的 Web 層（P3-3C）：建立、儲存並回首頁、修改、還原、刪除。

重點：一次性提醒（app/flash.py）只顯示一次、V13 共用同一條路、不影響 ledger_run。
"""

import re

from app.models import Setting
from app.services import budgets, transactions
from app.services.entries import ExpenseForm, RefundForm
from tests.integration.test_web_txn_detail import post
from tests.integration.test_web_txn_edit import edit
from tests.integration.test_web_txn_new import (  # noqa: F401
    HX,
    _today,
    anon,
    client,
    csrf_from,
    db,
    log_dir,
    pm_id,
    session_factory,
    submit,
)
from tests.ledger_helpers import TODAY, acc, post_manual

OCT = "2026-10"
CONFIG_TEXT = "預算提醒設定異常，這筆交易已儲存，但無法判斷是否跨過預算門檻。"


def budget(db, code="5100", amount=1000):
    budgets.set_budget(db, acc(db, code), OCT, amount)


def fund(db):
    """郵局有餘額，LINE Pay 的支出才不會順便觸發 V13（這些測試要單獨看 V14）。"""
    post_manual(db, TODAY, ("1102", 100000), ("4201", -100000), confirmed={"V10"})


def make(db, amount, code="5101") -> int:
    form = ExpenseForm(TODAY, amount, acc(db, code), pm_id(db, "LINE Pay"))
    return transactions.record_expense(db, form, confirmed={"V10", "V11"}).transaction_id


def notice_codes(html: str) -> list[str]:
    return re.findall(r'<p class="notice notice-warn"[^>]*data-code="([A-Z0-9_]+)"', html)


def card_states(html: str) -> list[str]:
    return re.findall(r'class="budget-card"[^>]*data-state="(\w+)"', html)


def set_cookie_names(resp) -> list[str]:
    return [h.split("=", 1)[0] for h in resp.headers.get_list("set-cookie")]


# --- 建立 -----------------------------------------------------------------------------------


def test_create_shows_v14_notice_and_card(client, db):
    budget(db)
    html = submit(client, db, amount=850).text
    assert "V14" in notice_codes(html)
    assert "接近上限" in html
    assert card_states(html) == ["warn"]


def test_create_without_crossing_shows_card_only(client, db):
    budget(db)
    html = submit(client, db, amount=100).text
    assert "V14" not in notice_codes(html)
    assert card_states(html) == ["ok"]
    assert "剩餘" in html


def test_create_over_budget_card_and_notice(client, db):
    budget(db)
    html = submit(client, db, amount=1200).text
    assert card_states(html) == ["over"]
    assert "已超支 200 元" in html and "超支 <span" in html


def test_create_without_budget_is_unchanged(client, db):
    html = submit(client, db, amount=5000).text
    assert card_states(html) == [] and "V14" not in notice_codes(html)
    assert "data-budget-cards" not in html


def test_non_expense_save_shows_cards_too(client, db):
    # 收入／轉帳走另一個分支（saved.label）；沒計入預算時不顯示卡片
    assert "data-budget-cards" not in client.get("/txn/new").text


def test_card_template_does_no_arithmetic(client, db):
    budget(db)
    html = submit(client, db, amount=850).text
    assert re.search(r'<span class="num">850</span> / <span class="num">1,000</span>', html)
    assert "85%" in html and "剩 <span" in html and ">150<" in html


def test_replayed_submission_has_no_cards(client, db):
    budget(db)
    first = submit(client, db, amount=850)
    token = re.search(r'name="submission_token" value="([^"]+)"', first.text)
    assert first.status_code == 200 and card_states(first.text) == ["warn"]
    assert token is not None


# --- 儲存並回首頁：一次性提醒 --------------------------------------------------------------------


def test_save_and_home_carries_v14_to_home_once(client, db):
    fund(db)
    budget(db)
    resp = submit(client, db, amount=850, action="home")
    assert resp.status_code == 200 and resp.headers["HX-Redirect"] == "/"
    assert "ledger_flash" in set_cookie_names(resp)

    first = client.get("/")
    assert notice_codes(first.text) == ["V14"]
    assert "接近上限" in first.text
    assert "ledger_flash" in set_cookie_names(first)  # 同一個回應把它刪掉

    again = client.get("/")
    assert "V14" not in notice_codes(again.text)
    assert "ledger_flash" not in set_cookie_names(again)


def test_save_and_home_carries_v13_the_same_way(client, db):
    resp = submit(client, db, payment_method_id=pm_id(db, "現金"), action="home")
    assert "ledger_flash" in set_cookie_names(resp)
    assert notice_codes(client.get("/").text) == ["V13"]
    assert notice_codes(client.get("/").text) == []


def test_save_and_home_without_notices_sets_no_flash(client, db):
    fund(db)
    resp = submit(client, db, amount=100, action="home")
    assert resp.headers["HX-Redirect"] == "/"
    assert "ledger_flash" not in set_cookie_names(resp)


def test_save_and_home_still_ends_the_run(client, db):
    budget(db)
    submit(client, db, amount=100, action="next")
    resp = submit(client, db, amount=850, action="home")
    assert "ledger_run" in set_cookie_names(resp)  # 清除（Max-Age=0）
    run_header = next(h for h in resp.headers.get_list("set-cookie") if h.startswith("ledger_run"))
    assert "Max-Age=0" in run_header
    flash_header = next(
        h for h in resp.headers.get_list("set-cookie") if h.startswith("ledger_flash")
    )
    assert "Path=/;" in flash_header + ";"


def test_save_and_next_does_not_use_flash_and_keeps_the_run(client, db):
    budget(db)
    resp = submit(client, db, amount=850, action="next")
    names = set_cookie_names(resp)
    assert "ledger_run" in names and "ledger_flash" not in names
    assert "V14" in notice_codes(resp.text)  # 直接顯示在表單上方


def test_reading_flash_does_not_touch_ledger_run(client, db):
    budget(db)
    submit(client, db, amount=850, action="home")
    resp = client.get("/")
    assert "ledger_run" not in set_cookie_names(resp)


def test_forged_flash_cookie_is_ignored(client, db):
    client.cookies.set("ledger_flash", "1.abc.def", path="/")
    resp = client.get("/")
    assert resp.status_code == 200 and notice_codes(resp.text) == []


def test_flash_is_not_shown_on_unrelated_pages(client, db):
    budget(db)
    submit(client, db, amount=850, action="home")
    assert "V14" not in notice_codes(client.get("/budgets").text)


# --- 修改、還原 ---------------------------------------------------------------------------------


def test_edit_crossing_shows_notice_and_card(client, db):
    budget(db)
    txn = make(db, 500)
    html = edit(client, db, txn, amount=900).text
    assert "V14" in notice_codes(html) and card_states(html) == ["warn"]
    assert "已儲存修改" in html


def test_edit_decrease_shows_card_without_notice(client, db):
    budget(db)
    txn = make(db, 900)
    html = edit(client, db, txn, amount=100).text
    assert "V14" not in notice_codes(html) and card_states(html) == ["ok"]


def test_restore_crossing_shows_notice_and_card(client, db):
    budget(db)
    gone = make(db, 700)
    transactions.delete_transaction(db, gone)
    make(db, 200)
    html = post(client, f"/txn/{gone}/restore").text
    assert "V14" in notice_codes(html) and card_states(html) == ["warn"]
    assert "已還原" in html


# --- 刪除 ---------------------------------------------------------------------------------------


def test_delete_refund_crossing_is_shown_once_on_the_detail_page(client, db):
    budget(db)
    original = make(db, 900)
    refund_id = transactions.record_refund(
        db, RefundForm(original, TODAY, 400, pm_id(db, "LINE Pay")), confirmed={"V11"}
    ).transaction_id
    resp = post(client, f"/txn/{refund_id}/delete")
    assert resp.headers["HX-Redirect"] == f"/txn/{refund_id}"
    assert "ledger_flash" in set_cookie_names(resp)

    first = client.get(f"/txn/{refund_id}")
    assert notice_codes(first.text).count("V14") == 1
    assert 'data-state="deleted"' in first.text
    assert "ledger_flash" in set_cookie_names(first)

    again = client.get(f"/txn/{refund_id}")
    assert "V14" not in notice_codes(again.text)


def test_delete_without_notices_sets_no_flash(client, db):
    budget(db)
    resp = post(client, f"/txn/{make(db, 100)}/delete")
    assert "ledger_flash" not in set_cookie_names(resp)


# --- 設定異常 ------------------------------------------------------------------------------------


def test_invalid_alert_pct_saves_and_shows_one_config_notice(client, db):
    budget(db)
    db.get(Setting, "budget_alert_pct").value = "abc"
    db.commit()
    resp = submit(client, db, amount=900)
    assert resp.status_code == 200
    assert notice_codes(resp.text).count("BUDGET_ALERT_CONFIG") == 1
    assert "V14" not in notice_codes(resp.text)
    assert CONFIG_TEXT in resp.text
    assert card_states(resp.text) == []  # 設定異常時不顯示卡片
