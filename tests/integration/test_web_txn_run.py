# ruff: noqa: F811  （共用 test_web_txn_new.py 的 fixture：pytest 依參數名稱注入，不是重複定義）
"""連續補記「本次已記 N 筆」（P2 Step 10，UF02，Web 層）。

「本次」只是簽章 Cookie 裡的 transaction id 清單（app/entry_run.py）；這裡確認：
何時加入、何時不加入、重送、結束一輪、過期、竄改、已刪除的顯示，以及只出現在建立表單。
帳務規則本身不在這裡測（沒有動 P1）。
"""

import re
from datetime import timedelta

import pytest
from sqlalchemy import select

from app import clock, entry_run
from app.models import Transaction
from tests.integration.test_web_txn_edit import edit
from tests.integration.test_web_txn_income_transfer import post, tokens
from tests.integration.test_web_txn_new import (  # noqa: F401
    CSRF_INPUT,
    HX,
    TOKEN_INPUT,
    _today,
    anon,
    client,
    csrf_from,
    db,
    fresh_tokens,
    input_value,
    log_dir,
    pm_id,
    selected_value,
    session_factory,
    submit,
)
from tests.ledger_helpers import TODAY, acc

SECRET = "test-secret-key"  # tests/integration/test_web_txn_new.py 的 anon fixture
RUN_SECTION = re.compile(r'<section id="txn-run".*?</section>', re.S)


def run_block(html: str) -> str | None:
    match = RUN_SECTION.search(html)
    return match.group(0) if match else None


def run_count(html: str) -> int | None:
    block = run_block(html)
    return int(re.search(r'data-run-count="(\d+)"', block).group(1)) if block else None


def run_ids(html: str) -> list[int]:
    """區塊裡的交易 id，依畫面由上到下。"""
    block = run_block(html)
    return [int(i) for i in re.findall(r'<li class="txn-row[^"]*" data-txn-id="(\d+)"', block)]


def title(html: str) -> str:
    return re.search(r'<h2 id="txn-run-title">(.*?)</h2>', run_block(html)).group(1)


def txn_ids(session_factory) -> list[int]:
    with session_factory() as s:
        return list(s.scalars(select(Transaction.id).order_by(Transaction.id)))


def run_cookie_header(resp) -> str | None:
    for header in resp.headers.get_list("set-cookie"):
        if header.startswith(f"{entry_run.RUN_COOKIE}="):
            return header
    return None


def cookie_ids(resp) -> tuple[int, ...]:
    header = run_cookie_header(resp)
    assert header
    value = header.split(";", 1)[0].split("=", 1)[1]
    return entry_run.decode(SECRET, value, clock.now())


def put_cookie(client, ids, *, age=timedelta(0)) -> None:
    """直接指定 Cookie 內容（先清掉伺服器設過的，避免 Cookie jar 裡同名的兩份並存）。"""
    client.cookies.delete(entry_run.RUN_COOKIE)
    client.cookies.set(
        entry_run.RUN_COOKIE, entry_run.encode(SECRET, tuple(ids), clock.now() - age), path="/txn"
    )


def put_cookie_value(client, value: str) -> None:
    client.cookies.delete(entry_run.RUN_COOKIE)
    client.cookies.set(entry_run.RUN_COOKIE, value, path="/txn")


def delete_txn(client, txn_id: int) -> None:
    resp = client.post(
        f"/txn/{txn_id}/delete", data={"csrf_token": csrf_from(client, "/txn/new")}, headers=HX
    )
    assert resp.status_code == 200


def restore_txn(client, txn_id: int) -> None:
    resp = client.post(
        f"/txn/{txn_id}/restore", data={"csrf_token": csrf_from(client, "/txn/new")}, headers=HX
    )
    assert resp.status_code == 200


# --- 開始與累加 --------------------------------------------------------------------------


def test_no_run_block_before_anything_is_saved(client):
    html = client.get("/txn/new").text
    assert run_block(html) is None
    assert "本次已記" not in html


def test_first_save_next_starts_the_run_with_one(client, db, session_factory):
    resp = submit(client, db)
    [txn_id] = txn_ids(session_factory)
    assert resp.status_code == 200
    assert title(resp.text) == "本次已記 1 筆"
    assert run_count(resp.text) == 1
    assert run_ids(resp.text) == [txn_id]
    assert f'href="/txn/{txn_id}"' in run_block(resp.text)
    assert cookie_ids(resp) == (txn_id,)


def test_second_save_next_makes_two_newest_first(client, db, session_factory):
    submit(client, db)
    resp = submit(client, db, amount=80)
    first, second = txn_ids(session_factory)
    assert title(resp.text) == "本次已記 2 筆"
    assert run_ids(resp.text) == [second, first]
    assert cookie_ids(resp) == (first, second)


def test_save_next_still_keeps_date_and_payment_and_clears_the_rest(client, db):
    resp = submit(client, db, description="午餐", merchant_name="全家")
    assert input_value(resp.text, "txn_date") == TODAY.isoformat()
    assert selected_value(resp.text, "payment_method_id") == str(pm_id(db, "LINE Pay"))
    assert input_value(resp.text, "amount") == ""
    assert input_value(resp.text, "merchant_name") == ""
    assert run_count(resp.text) == 1


def test_run_row_shows_current_amount_merchant_and_description(client, db):
    resp = submit(client, db, amount=250, merchant_name="全家", description="午餐")
    block = run_block(resp.text)
    assert '<span class="txn-amount">250</span>' in block
    assert "全家" in block and "午餐" in block


def test_expense_income_transfer_and_card_payment_share_one_run(client, db, session_factory):
    submit(client, db)
    post(client, db, "income")
    post(client, db, "transfer")
    resp = post(client, db, "transfer", to_account_id=acc(db, "2101"))
    assert run_count(resp.text) == 4
    block = run_block(resp.text)
    for label in ("支出", "收入", "轉帳", "繳卡費"):
        assert f">{label}</span>" in block
    assert run_ids(resp.text) == sorted(txn_ids(session_factory), reverse=True)


# --- 離開再回來、重新整理 ----------------------------------------------------------------


def test_refresh_and_coming_back_keep_the_run(client, db, session_factory):
    submit(client, db)
    submit(client, db, amount=80)
    html = client.get("/txn/new").text  # 重新整理，或點進明細後回來
    assert run_count(html) == 2
    # 表單回到預設（今天、預設付款方式）；不另外保存上一筆
    assert input_value(html, "txn_date") == TODAY.isoformat()
    assert input_value(html, "amount") == ""
    [a, b] = txn_ids(session_factory)
    assert client.get(f"/txn/{a}").status_code == 200  # 點進去
    assert run_ids(client.get("/txn/new").text) == [b, a]


def test_type_switch_keeps_the_run_block(client, db):
    submit(client, db)
    for txn_type in ("income", "transfer", "expense"):
        html = client.get("/txn/new", params={"type": txn_type}, headers=HX).text
        assert run_count(html) == 1


def test_run_is_not_shown_on_edit_form_or_other_pages(client, db):
    txn_id = int(re.search(r'data-txn-id="(\d+)"', submit(client, db).text).group(1))
    assert run_block(client.get(f"/txn/{txn_id}/edit").text) is None
    for path in ("/", "/txn", f"/txn/{txn_id}", "/merchants"):
        assert "本次已記" not in client.get(path).text, path


# --- 不加入的情況 ------------------------------------------------------------------------


def test_validation_failure_does_not_change_the_run(client, db, session_factory):
    submit(client, db)
    resp = submit(client, db, amount="abc")  # 欄位解析失敗
    assert resp.status_code == 422
    assert run_cookie_header(resp) is None
    assert run_count(resp.text) == 1
    resp = submit(client, db, account_id="")  # 欄位解析失敗：沒選科目
    assert resp.status_code == 422
    assert run_cookie_header(resp) is None
    assert run_count(client.get("/txn/new").text) == 1
    assert len(txn_ids(session_factory)) == 1


def test_p1_validation_failure_does_not_start_a_run(client, db, session_factory):
    resp = submit(client, db, txn_date="2999-99-99")
    assert resp.status_code == 422
    assert run_cookie_header(resp) is None
    assert run_block(resp.text) is None
    assert txn_ids(session_factory) == []


def test_pending_confirmation_does_not_count_until_confirmed(client, db, session_factory):
    submit(client, db)
    pending = submit(client, db, amount=20000)  # V10：大額，要先確認
    assert pending.status_code == 409
    assert run_cookie_header(pending) is None
    assert len(txn_ids(session_factory)) == 1
    assert run_count(client.get("/txn/new").text) == 1

    confirmed = submit(client, db, amount=20000, confirmed="V10")
    assert confirmed.status_code == 200
    assert run_count(confirmed.text) == 2
    assert len(cookie_ids(confirmed)) == 2


def test_back_to_edit_does_not_change_the_run(client, db):
    submit(client, db)
    resp = submit(client, db, amount=20000, action="edit")
    assert resp.status_code == 200
    assert run_cookie_header(resp) is None
    assert run_count(resp.text) == 1


# --- 重送 --------------------------------------------------------------------------------


def _same_form_data(client, db, **extra):
    csrf, token = fresh_tokens(client)
    return {
        "csrf_token": csrf,
        "submission_token": token,
        "txn_date": TODAY.isoformat(),
        "amount": "120",
        "account_id": str(acc(db, "5101")),
        "payment_method_id": str(pm_id(db, "LINE Pay")),
        "action": "next",
        **extra,
    }


def test_replay_does_not_count_twice(client, db, session_factory):
    data = _same_form_data(client, db)
    first = client.post("/txn/new", data=data, headers=HX)
    second = client.post("/txn/new", data=data, headers=HX)
    third = client.post("/txn/new", data=data, headers=HX)
    assert 'data-replayed="true"' in third.text
    assert len(txn_ids(session_factory)) == 1
    assert run_count(first.text) == run_count(second.text) == run_count(third.text) == 1
    assert cookie_ids(third) == cookie_ids(first)


def test_replay_adds_the_transaction_when_the_cookie_missed_it(client, db, session_factory):
    """第一次回應遺失、Cookie 沒寫到：重送時補進這一輪。"""
    data = _same_form_data(client, db)
    client.post("/txn/new", data=data, headers=HX)
    [txn_id] = txn_ids(session_factory)
    client.cookies.delete(entry_run.RUN_COOKIE, path="/txn")
    assert run_block(client.get("/txn/new").text) is None

    again = client.post("/txn/new", data=data, headers=HX)
    assert 'data-replayed="true"' in again.text
    assert run_ids(again.text) == [txn_id]
    assert cookie_ids(again) == (txn_id,)
    assert len(txn_ids(session_factory)) == 1


# --- 儲存並回首頁 = 結束這一輪 -----------------------------------------------------------


def test_save_and_home_clears_the_run(client, db, session_factory):
    submit(client, db)
    submit(client, db, amount=80)
    resp = submit(client, db, amount=50, action="home")
    assert resp.headers["HX-Redirect"] == "/"
    assert len(txn_ids(session_factory)) == 3
    header = run_cookie_header(resp)
    assert header and "Max-Age=0" in header and "Path=/txn" in header
    assert run_block(client.get("/txn/new").text) is None


def test_next_after_home_starts_a_new_run_from_one(client, db):
    submit(client, db)
    submit(client, db, action="home")
    assert run_count(submit(client, db).text) == 1


def test_replay_with_save_and_home_also_ends_the_run(client, db, session_factory):
    submit(client, db)
    data = _same_form_data(client, db, action="home")
    client.post("/txn/new", data=data, headers=HX)
    resp = client.post("/txn/new", data=data, headers=HX)  # 重送
    assert resp.headers["HX-Redirect"] == "/"
    assert len(txn_ids(session_factory)) == 2
    header = run_cookie_header(resp)
    assert header and "Max-Age=0" in header and "Path=/txn" in header
    assert run_block(client.get("/txn/new").text) is None


def test_failed_save_and_home_keeps_the_run(client, db):
    submit(client, db)
    resp = submit(client, db, amount="abc", action="home")
    assert resp.status_code == 422
    assert run_cookie_header(resp) is None
    assert run_count(client.get("/txn/new").text) == 1


# --- 過期、竄改、上限 --------------------------------------------------------------------


def test_run_expires_after_thirty_idle_minutes(client, db, session_factory):
    submit(client, db)
    [txn_id] = txn_ids(session_factory)
    put_cookie(client, [txn_id], age=timedelta(minutes=29))
    assert run_count(client.get("/txn/new").text) == 1
    put_cookie(client, [txn_id], age=timedelta(minutes=31))
    assert run_block(client.get("/txn/new").text) is None


def test_saving_after_expiry_starts_a_new_run(client, db, session_factory):
    submit(client, db)
    [old] = txn_ids(session_factory)
    put_cookie(client, [old], age=timedelta(minutes=31))
    resp = submit(client, db, amount=80)
    new = max(txn_ids(session_factory))
    assert run_count(resp.text) == 1
    assert run_ids(resp.text) == [new]
    assert cookie_ids(resp) == (new,)


def test_each_successful_save_extends_the_idle_time(client, db, monkeypatch):
    start = clock.now()

    def at(minutes: int) -> None:
        monkeypatch.setattr(clock, "now", lambda: start + timedelta(minutes=minutes))

    submit(client, db)  # 第 0 分鐘
    at(25)
    submit(client, db, amount=80)  # 第 25 分鐘：閒置期限延長到第 55 分鐘
    at(54)  # 距離第一筆 54 分鐘，距離最後一筆 29 分鐘
    assert run_count(client.get("/txn/new").text) == 2
    at(56)  # 距離最後一筆超過 30 分鐘
    assert run_block(client.get("/txn/new").text) is None


@pytest.mark.parametrize(
    "garbage", ["abc", "1.2", "a.b.c", "", "0.0.0", "9" * 5000, "%E4%BD%A0%E5%A5%BD", "1.1-2.dead"]
)
def test_tampered_cookie_is_ignored_safely(client, db, session_factory, garbage):
    put_cookie_value(client, garbage)
    page = client.get("/txn/new")
    assert page.status_code == 200 and run_block(page.text) is None
    resp = submit(client, db)
    assert resp.status_code == 200
    assert run_count(resp.text) == 1
    assert len(txn_ids(session_factory)) == 1


def test_cookie_signed_with_another_key_is_ignored(client, db, session_factory):
    submit(client, db)
    [txn_id] = txn_ids(session_factory)
    put_cookie_value(client, entry_run.encode("wrong-key", (txn_id,), clock.now()))
    assert run_block(client.get("/txn/new").text) is None


def test_run_is_capped_at_fifty_ids(client, db, session_factory):
    old = list(range(1000, 1000 + entry_run.MAX_IDS))  # 不存在的 id：只用來占滿上限
    put_cookie(client, old)
    resp = submit(client, db)
    new = max(txn_ids(session_factory))
    ids = cookie_ids(resp)
    assert len(ids) == entry_run.MAX_IDS
    assert ids[0] == 1001 and ids[-1] == new  # 最舊的被丟掉
    assert run_count(resp.text) == 1  # 找不到的 id 不顯示、不計入


def test_ids_the_database_does_not_have_are_ignored_and_not_counted(client, db, session_factory):
    submit(client, db)
    [txn_id] = txn_ids(session_factory)
    put_cookie(client, [txn_id, 98765])
    html = client.get("/txn/new").text
    assert run_count(html) == 1
    assert run_ids(html) == [txn_id]
    assert "98765" not in html


# --- 已刪除、還原、修改 ------------------------------------------------------------------


def test_soft_delete_and_restore_do_not_change_n(client, db, session_factory):
    for amount in (100, 200, 300):
        submit(client, db, amount=amount)
    ids = txn_ids(session_factory)
    assert run_count(client.get("/txn/new").text) == 3

    delete_txn(client, ids[1])
    html = client.get("/txn/new").text
    assert run_count(html) == 3 and title(html) == "本次已記 3 筆"
    assert run_ids(html) == sorted(ids, reverse=True)  # 仍列在清單
    deleted_rows = re.findall(r'<li class="txn-row is-deleted" data-txn-id="(\d+)"', html)
    assert deleted_rows == [str(ids[1])]
    assert run_block(html).count("已刪除") == 1
    assert f'href="/txn/{ids[1]}"' in run_block(html)  # 仍可點進明細還原

    restore_txn(client, ids[1])
    html = client.get("/txn/new").text
    assert run_count(html) == 3
    assert "is-deleted" not in html and "已刪除" not in run_block(html)


def test_deleting_everything_still_shows_the_run_with_deleted_rows(client, db, session_factory):
    submit(client, db)
    [txn_id] = txn_ids(session_factory)
    delete_txn(client, txn_id)
    html = client.get("/txn/new").text
    assert run_count(html) == 1
    assert run_block(html).count("已刪除") == 1


def test_next_save_after_a_delete_counts_on(client, db, session_factory):
    submit(client, db)
    submit(client, db, amount=80)
    delete_txn(client, txn_ids(session_factory)[0])
    assert run_count(submit(client, db, amount=60).text) == 3


def test_run_shows_the_latest_database_values_after_an_edit(client, db, session_factory):
    submit(client, db, amount=120, description="舊備註")
    [txn_id] = txn_ids(session_factory)
    resp = edit(client, db, txn_id, amount="345", description="新備註")
    assert resp.status_code == 200
    block = run_block(client.get("/txn/new").text)
    assert '<span class="txn-amount">345</span>' in block
    assert "新備註" in block and "舊備註" not in block
    assert '<span class="txn-amount">120</span>' not in block


# --- 不新增任何儲存 ----------------------------------------------------------------------


def test_cookie_holds_only_a_timestamp_and_transaction_ids(client, db, session_factory):
    resp = submit(client, db, amount=777, merchant_name="某某商店", description="秘密備註")
    value = run_cookie_header(resp).split(";", 1)[0].split("=", 1)[1]
    assert re.fullmatch(r"\d+\.\d+(-\d+)*\.[0-9a-f]{64}", value)
    for text in ("777", "某某商店", "秘密備註"):
        assert text not in value


def test_run_cookie_flags(client, db):
    header = run_cookie_header(submit(client, db))
    assert "Path=/txn" in header and "HttpOnly" in header and "SameSite=lax" in header


def test_new_run_token_and_csrf_still_present_in_form(client, db):
    resp = submit(client, db)
    assert len(TOKEN_INPUT.findall(resp.text)) == 1
    assert CSRF_INPUT.search(resp.text)
    assert tokens  # 與 test_web_txn_income_transfer 共用的 helper 仍可用
