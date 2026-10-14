# ruff: noqa: F811  （共用 test_web_txn_new.py 的 fixture）
"""/budgets 預算設定頁（P3-3B；規格 F20、7.3、M05）。"""

import re

import pytest

from app.models import Account, Budget
from app.services import budgets
from tests.integration.test_web_txn_new import (  # noqa: F401
    _today,
    anon,
    client,
    csrf_from,
    db,
    log_dir,
    session_factory,
)
from tests.ledger_helpers import TODAY, acc, post_manual

OCT, NOV = "2026-10", "2026-11"


def save(client, db, *, code="5100", amount="1000", month=OCT, **extra):
    csrf = csrf_from(client, "/budgets")
    data = {"csrf_token": csrf, "monthly_amount": amount, "effective_from": month, **extra}
    return client.post(f"/budgets/{acc(db, code)}", data=data)


def rows(db, code="5100"):
    db.expire_all()
    return [
        (b.effective_from, b.monthly_amount)
        for b in db.query(Budget)
        .filter_by(account_id=acc(db, code))
        .order_by(Budget.effective_from)
    ]


def test_requires_login(anon):
    assert anon.get("/budgets").status_code == 303


def test_post_requires_login(anon, db):
    assert anon.post(f"/budgets/{acc(db, '5100')}", data={}).status_code in (303, 403)


def test_post_without_csrf_rejected(client, db):
    resp = client.post(
        f"/budgets/{acc(db, '5100')}", data={"monthly_amount": "1", "effective_from": OCT}
    )
    assert resp.status_code == 403
    assert rows(db) == []


def test_get_lists_forms_with_month_input(client, db):
    html = client.get("/budgets").text
    assert f'name="effective_from" type="month" min="{OCT}" value="{OCT}"' in html
    assert 'type="number"' in html and "取消" not in html.split("</p>", 1)[1]
    assert "<select" not in html


def test_get_has_no_delete_or_cancel_controls(client, db):
    save(client, db)
    html = client.get("/budgets").text
    assert "刪除" not in html and 'method="delete"' not in html
    assert not re.search(r"<button[^>]*>[^<]*(取消|刪除)", html)


def test_save_redirects_and_persists(client, db):
    resp = save(client, db)
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/budgets?saved={acc(db, '5100')}"
    assert rows(db) == [(OCT, 1000)]


def test_saved_notice_shown(client, db):
    save(client, db)
    html = client.get(f"/budgets?saved={acc(db, '5100')}").text
    assert "已儲存" in html


def test_same_month_updates_amount(client, db):
    save(client, db)
    save(client, db, amount="1500")
    assert rows(db) == [(OCT, 1500)]


def test_future_month_creates_scheduled_version(client, db):
    save(client, db)
    save(client, db, amount="2000", month=NOV)
    assert rows(db) == [(OCT, 1000), (NOV, 2000)]
    html = client.get("/budgets").text
    assert "已排定" in html and "2026-11 起" in html


def test_current_prefilled_with_in_force_amount(client, db):
    save(client, db)
    html = client.get("/budgets").text
    assert re.search(rf'id="amount-{acc(db, "5100")}"[^>]*value="1000"', html)


@pytest.mark.parametrize("amount", ["0", "-5", "abc", "", "1.5"])
def test_invalid_amount_422(client, db, amount):
    resp = save(client, db, amount=amount)
    assert resp.status_code == 422
    assert "BUDGET_AMOUNT" in resp.text
    assert rows(db) == []


def test_past_month_tampering_422(client, db):
    resp = save(client, db, month="2026-09")
    assert resp.status_code == 422
    assert "BUDGET_PAST_MONTH" in resp.text
    assert rows(db) == []


@pytest.mark.parametrize("month", ["", "2026-13", "2026-1", "abc", "2026-10-01"])
def test_bad_month_422(client, db, month):
    assert save(client, db, month=month).status_code == 422
    assert rows(db) == []


def test_422_keeps_submitted_input(client, db):
    resp = save(client, db, amount="0", month=NOV)
    assert 'value="0"' in resp.text and f'value="{NOV}"' in resp.text


def test_m05_child_account_rejected(client, db):
    child = (
        db.query(Account)
        .filter(Account.parent_id.isnot(None), Account.account_class == "EXPENSE")
        .first()
    )
    if child is None:
        pytest.skip("測試科目表沒有第二層費用科目")
    csrf = csrf_from(client, "/budgets")
    resp = client.post(
        f"/budgets/{child.id}",
        data={"csrf_token": csrf, "monthly_amount": "100", "effective_from": OCT},
    )
    assert resp.status_code == 422 and "M05" in resp.text


def test_non_expense_account_rejected(client, db):
    csrf = csrf_from(client, "/budgets")
    resp = client.post(
        f"/budgets/{acc(db, '1101')}",
        data={"csrf_token": csrf, "monthly_amount": "100", "effective_from": OCT},
    )
    assert resp.status_code == 422 and "M05" in resp.text


def test_unknown_account_redirects(client, db):
    csrf = csrf_from(client, "/budgets")
    resp = client.post(
        "/budgets/999999",
        data={"csrf_token": csrf, "monthly_amount": "100", "effective_from": OCT},
    )
    assert resp.status_code == 303 and resp.headers["location"] == "/budgets"


def test_inactive_account_read_only(client, db):
    aid = acc(db, "5100")
    budgets.set_budget(db, aid, OCT, 1000)
    db.get(Account, aid).is_active = False
    db.commit()
    html = client.get("/budgets").text
    assert "已停用" in html
    assert f'action="/budgets/{aid}"' not in html


def test_inactive_account_post_rejected(client, db):
    csrf = csrf_from(client, "/budgets")
    db.get(Account, acc(db, "5100")).is_active = False
    db.commit()
    resp = client.post(
        f"/budgets/{acc(db, '5100')}",
        data={"csrf_token": csrf, "monthly_amount": "100", "effective_from": OCT},
    )
    assert resp.status_code == 422 and "INACTIVE" in resp.text


def test_progress_card_shown_on_budgets_page(client, db):
    save(client, db)
    post_manual(db, TODAY, ("5101", 900), ("1101", -900))
    html = client.get("/budgets").text
    assert 'data-state="warn"' in html and "接近上限" in html and "90%" in html


def test_invalid_alert_pct_budget_page_still_usable(client, db):
    from app.models import Setting

    save(client, db)
    db.get(Setting, "budget_alert_pct").value = "abc"
    db.commit()
    resp = client.get("/budgets")
    assert resp.status_code == 200
    assert 'data-code="BUDGET_SETTING"' in resp.text
    assert f'action="/budgets/{acc(db, "5100")}"' in resp.text
    assert save(client, db, amount="2000").status_code == 303
