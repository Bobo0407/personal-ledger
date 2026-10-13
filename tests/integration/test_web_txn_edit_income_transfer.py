# ruff: noqa: F811  （共用 test_web_txn_new.py 的 fixture：pytest 依參數名稱注入，不是重複定義）
"""GET/POST /txn/{id}/edit：收入、轉帳、繳卡費（P2 Step 5B，Web 層）；
以及修改表單的「停用中的目前值」（D5，支出一起適用）。

帳務規則（V06、V10、V11、V13、P6、TXN_STRUCTURE、TXN_TYPE_IMMUTABLE、D9）全部由 P1 判斷；
Web 只多一個「表單能不能完整修改」的檢查（txn_edit：還原 → builder 重建 → 比對）。
"""

import re
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.models import Account, AuditLog, Merchant, PaymentMethod, Transaction
from app.services import transactions
from app.services.entries import ExpenseForm, IncomeForm, TransferForm
from tests.integration.test_web_txn_new import (  # noqa: F401
    HX,
    TOKEN_INPUT,
    _today,
    anon,
    banner_codes,
    client,
    confirm_codes,
    csrf_from,
    db,
    field_error_codes,
    hidden,
    input_value,
    log_dir,
    select_block,
    selected_value,
    session_factory,
    user_id,
)
from tests.ledger_helpers import TODAY, acc

TOKEN = "e" * 32
FIELDS = {
    "income": ("txn_date", "amount", "account_id", "receive_account_id", "description"),
    "transfer": (
        "txn_date",
        "amount",
        "from_account_id",
        "to_account_id",
        "fee_amount",
        "fee_account_id",
        "description",
    ),
}
SELECTS = {"account_id", "receive_account_id", "from_account_id", "to_account_id", "fee_account_id"}


def income(db, **kwargs) -> int:
    form = IncomeForm(TODAY, 3000, acc(db, "4201"), acc(db, "1102"), **kwargs)
    return transactions.record_income(db, form, submission_token=TOKEN).transaction_id


def transfer(db, *, to="1103", amount=2000, token=TOKEN, **kwargs) -> int:
    form = TransferForm(TODAY, amount, acc(db, "1102"), acc(db, to), **kwargs)
    return transactions.record_transfer(db, form, submission_token=token).transaction_id


def card_payment(db) -> int:
    return transfer(db, to="2101")


def edit(client, txn_id, kind, **overrides):
    """以「目前的值」送出修改表單；overrides 覆寫或加欄位（例如畫面上沒有的手續費）。"""
    html = client.get(f"/txn/{txn_id}/edit").text
    data = {"csrf_token": csrf_from(client, "/txn/new"), "action": "save"}
    for name in FIELDS[kind]:
        if name in SELECTS:
            if f'<select id="{name}"' in html:
                data[name] = selected_value(html, name) or ""
        elif f'<input id="{name}"' in html:
            data[name] = input_value(html, name)
    data.update({k: str(v) for k, v in overrides.items()})
    return client.post(f"/txn/{txn_id}/edit", data=data, headers=HX)


def state(session_factory, txn_id):
    with session_factory() as s:
        t = s.get(Transaction, txn_id)
        postings = sorted((p.account_id, p.amount, p.line_role) for p in t.postings)
        return t.txn_type, t.txn_date, t.description, t.source_ref, postings


def audits(session_factory, txn_id) -> list[tuple[str, int | None]]:
    with session_factory() as s:
        rows = s.scalars(
            select(AuditLog).where(AuditLog.entity_id == txn_id).order_by(AuditLog.id)
        ).all()
        return [(r.action, r.user_id) for r in rows]


def txn_count(session_factory) -> int:
    with session_factory() as s:
        return s.scalar(select(func.count()).select_from(Transaction))


def option_labels(html: str, name: str) -> list[str]:
    return re.findall(r"<option value=\"\d+\"[^>]*>([^<]+)</option>", select_block(html, name))


def deactivate(session_factory, *codes):
    with session_factory() as s:
        for code in codes:
            s.get(Account, acc(s, code)).is_active = False
        s.commit()


def ok(resp) -> bool:
    return resp.status_code == 200 and "已儲存修改。" in resp.text


# --- 收入 ------------------------------------------------------------------------------


def test_income_edit_form(client, db):
    txn_id = income(db, description="利息")
    html = client.get(f"/txn/{txn_id}/edit").text
    assert "<h1>修改收入</h1>" in html
    assert input_value(html, "amount") == "3000"
    assert selected_value(html, "account_id") == str(acc(db, "4201"))
    assert selected_value(html, "receive_account_id") == str(acc(db, "1102"))
    assert input_value(html, "description") == "利息"
    assert TOKEN_INPUT.search(html) is None
    assert 'name="txn_type"' not in html and "type-tabs" not in html
    assert 'name="merchant_name"' not in html
    assert f'hx-post="/txn/{txn_id}/edit"' in html


def test_income_edit_amount_and_accounts(client, db, session_factory):
    txn_id = income(db)
    resp = edit(
        client,
        txn_id,
        "income",
        amount="3500",
        account_id=acc(db, "4101"),
        receive_account_id=acc(db, "1103"),
    )
    assert ok(resp)
    assert resp.headers["HX-Push-Url"] == f"/txn/{txn_id}"
    txn_type, _, _, source_ref, postings = state(session_factory, txn_id)
    assert (txn_type, source_ref) == ("INCOME", f"submit:{TOKEN}")
    assert postings == sorted([(acc(db, "1103"), 3500, None), (acc(db, "4101"), -3500, "MAIN")])
    assert audits(session_factory, txn_id) == [("CREATE", None), ("UPDATE", user_id(db))]
    assert txn_count(session_factory) == 1


def test_income_v10_v11_confirmation(client, db, session_factory):
    txn_id = income(db)
    resp = edit(client, txn_id, "income", amount="12000")
    assert resp.status_code == 409 and confirm_codes(resp.text) == ["V10"]
    assert hidden(resp.text, "receive_account_id") == str(acc(db, "1102"))
    assert 'name="submission_token"' not in resp.text
    future = (TODAY + timedelta(days=2)).isoformat()
    resp = edit(client, txn_id, "income", txn_date=future)
    assert resp.status_code == 409 and confirm_codes(resp.text) == ["V11"]
    assert state(session_factory, txn_id)[1] == TODAY.isoformat()
    assert ok(edit(client, txn_id, "income", txn_date=future, confirmed="V11"))


def test_income_v13_notice_after_moving_receive_account(client, db):
    txn_id = income(db)
    transfer(db, amount=2000, token=None)  # 郵局 -2,000；收入移走之後郵局變負數
    resp = edit(client, txn_id, "income", receive_account_id=acc(db, "1101"))
    assert ok(resp)
    assert re.search(r'class="notice notice-warn" data-code="V13"', resp.text)


def test_income_with_merchant_cannot_be_edited(client, db, session_factory):
    m = Merchant(name="公司")
    db.add(m)
    db.commit()
    txn_id = income(db, merchant_id=m.id)
    resp = client.get(f"/txn/{txn_id}/edit")
    assert resp.status_code == 303 and resp.headers["location"] == f"/txn/{txn_id}"
    assert "收入表單沒有商家欄位" in client.get(f"/txn/{txn_id}").text
    before = state(session_factory, txn_id)
    resp = client.post(
        f"/txn/{txn_id}/edit",
        data={"csrf_token": csrf_from(client, "/txn/new"), "amount": "1", "action": "save"},
        headers=HX,
    )
    assert resp.status_code == 422 and 'data-code="NOT_EDITABLE"' in resp.text
    assert state(session_factory, txn_id) == before
    assert audits(session_factory, txn_id) == [("CREATE", None)]


# --- 轉帳 ------------------------------------------------------------------------------


def test_transfer_edit_form_options(client, db):
    html = client.get(f"/txn/{transfer(db)}/edit").text
    assert "<h1>修改轉帳</h1>" in html
    # 轉出、轉入都不列信用卡（改成信用卡一定被 TXN_TYPE_IMMUTABLE 擋下）；有手續費區塊
    assert "聯邦信用卡" not in option_labels(html, "from_account_id")
    assert "聯邦信用卡" not in option_labels(html, "to_account_id")
    assert 'id="fee_amount"' in html


def test_transfer_edit_amount_from_to(client, db, session_factory):
    txn_id = transfer(db)
    resp = edit(
        client,
        txn_id,
        "transfer",
        amount="800",
        from_account_id=acc(db, "1103"),
        to_account_id=acc(db, "1101"),
        description="提款",
    )
    assert ok(resp)
    assert state(session_factory, txn_id)[4] == sorted(
        [(acc(db, "1101"), 800, None), (acc(db, "1103"), -800, None)]
    )
    assert audits(session_factory, txn_id)[-1] == ("UPDATE", user_id(db))


def test_transfer_fee_add_change_remove(client, db, session_factory):
    txn_id = transfer(db)
    fee = acc(db, "5501")
    assert ok(edit(client, txn_id, "transfer", fee_amount="15", fee_account_id=fee))
    assert (fee, 15, "FEE") in state(session_factory, txn_id)[4]
    assert (acc(db, "1102"), -2015, None) in state(session_factory, txn_id)[4]
    assert ok(edit(client, txn_id, "transfer", fee_amount="30", fee_account_id=acc(db, "5502")))
    assert (acc(db, "5502"), 30, "FEE") in state(session_factory, txn_id)[4]
    assert ok(edit(client, txn_id, "transfer", fee_amount="", fee_account_id=""))
    assert len(state(session_factory, txn_id)[4]) == 2
    assert [a for a, _ in audits(session_factory, txn_id)] == [
        "CREATE",
        "UPDATE",
        "UPDATE",
        "UPDATE",
    ]
    # 異動紀錄自動顯示分錄變化
    assert "手續費" in client.get(f"/txn/{txn_id}").text


def test_transfer_v06_same_account(client, db, session_factory):
    txn_id = transfer(db)
    before = state(session_factory, txn_id)
    resp = edit(client, txn_id, "transfer", to_account_id=acc(db, "1102"))
    assert resp.status_code == 422
    assert field_error_codes(resp.text, "to_account_id") == ["V06"]
    assert state(session_factory, txn_id) == before


def test_transfer_p6_from_credit_card_rejected_by_p1(client, db, session_factory):
    txn_id = transfer(db)
    before = state(session_factory, txn_id)
    resp = edit(client, txn_id, "transfer", from_account_id=acc(db, "2101"))
    assert resp.status_code == 422
    assert state(session_factory, txn_id) == before
    assert audits(session_factory, txn_id) == [("CREATE", None)]


def test_transfer_to_credit_card_is_type_immutable(client, db, session_factory):
    txn_id = transfer(db)
    before = state(session_factory, txn_id)
    resp = edit(client, txn_id, "transfer", to_account_id=acc(db, "2101"))
    assert resp.status_code == 422
    assert banner_codes(resp.text) == ["TXN_TYPE_IMMUTABLE"]
    assert state(session_factory, txn_id) == before
    assert txn_count(session_factory) == 1


def test_transfer_v10_v11_v13(client, db, session_factory):
    txn_id = transfer(db)
    resp = edit(client, txn_id, "transfer", amount="12000")
    assert resp.status_code == 409 and confirm_codes(resp.text) == ["V10"]
    future = (TODAY + timedelta(days=1)).isoformat()
    resp = edit(client, txn_id, "transfer", txn_date=future)
    assert resp.status_code == 409 and confirm_codes(resp.text) == ["V11"]
    resp = edit(client, txn_id, "transfer", from_account_id=acc(db, "1101"))
    assert ok(resp) and 'data-code="V13"' in resp.text


# --- 繳卡費 ----------------------------------------------------------------------------


@pytest.fixture
def second_card(session_factory):
    with session_factory() as s:
        card = Account(
            code="2102", name="玉山信用卡", account_class="LIABILITY", subtype="CREDIT_CARD"
        )
        s.add(card)
        s.commit()
        return card.id


def test_card_payment_edit_form(client, db, second_card):
    html = client.get(f"/txn/{card_payment(db)}/edit").text
    assert "<h1>修改繳卡費</h1>" in html
    assert sorted(option_labels(html, "to_account_id")) == sorted(["聯邦信用卡", "玉山信用卡"])
    assert "聯邦信用卡" not in option_labels(html, "from_account_id")
    assert 'id="fee_amount"' not in html and 'name="fee_account_id"' not in html
    assert "扣款帳戶" in html


def test_card_payment_edit_amount_from_and_card(client, db, session_factory, second_card):
    txn_id = card_payment(db)
    resp = edit(
        client,
        txn_id,
        "transfer",
        amount="2500",
        from_account_id=acc(db, "1103"),
        to_account_id=second_card,
    )
    assert ok(resp)
    txn_type, _, _, source_ref, postings = state(session_factory, txn_id)
    assert (txn_type, source_ref) == ("CARD_PAYMENT", f"submit:{TOKEN}")
    assert postings == sorted([(second_card, 2500, None), (acc(db, "1103"), -2500, None)])
    assert txn_count(session_factory) == 1
    assert audits(session_factory, txn_id) == [("CREATE", None), ("UPDATE", user_id(db))]
    listing = client.get("/txn").text
    assert 'data-expense-net="0" data-income-total="0"' in listing


def test_card_payment_to_non_card_is_type_immutable(client, db, session_factory):
    txn_id = card_payment(db)
    before = state(session_factory, txn_id)
    resp = edit(client, txn_id, "transfer", to_account_id=acc(db, "1103"))
    assert resp.status_code == 422
    assert banner_codes(resp.text) == ["TXN_TYPE_IMMUTABLE"]
    assert state(session_factory, txn_id) == before
    assert txn_count(session_factory) == 1


def test_card_payment_fee_is_structure_error_on_banner(client, db, session_factory):
    """畫面沒有手續費；直接 POST 帶手續費由 P1 擋下（TXN_STRUCTURE），錯誤顯示在頂端。"""
    txn_id = card_payment(db)
    before = state(session_factory, txn_id)
    resp = edit(client, txn_id, "transfer", fee_amount="15", fee_account_id=acc(db, "5501"))
    assert resp.status_code == 422
    assert banner_codes(resp.text) == ["TXN_STRUCTURE"]
    assert state(session_factory, txn_id) == before


def test_edit_ignores_posted_txn_type_and_token(client, db, session_factory):
    """表單依原交易類型決定：送來的 txn_type、權杖都不看。"""
    txn_id = income(db)
    resp = edit(
        client, txn_id, "income", txn_type="transfer", submission_token="x" * 32, amount="3100"
    )
    assert ok(resp)
    assert state(session_factory, txn_id)[0] == "INCOME"


def test_create_still_requires_token(client, db, session_factory):
    for kind, extra in (
        ("income", {"account_id": acc(db, "4201"), "receive_account_id": acc(db, "1102")}),
        ("transfer", {"from_account_id": acc(db, "1102"), "to_account_id": acc(db, "1103")}),
    ):
        resp = client.post(
            "/txn/new",
            data={
                "csrf_token": csrf_from(client, "/txn/new"),
                "txn_type": kind,
                "txn_date": TODAY.isoformat(),
                "amount": "100",
                **extra,
            },
            headers=HX,
        )
        assert resp.status_code == 422
    assert txn_count(session_factory) == 0


# --- D5：停用中的目前值 ------------------------------------------------------------------


def test_inactive_current_expense_account_and_payment_method(client, db, session_factory):
    pm = db.scalar(select(PaymentMethod.id).where(PaymentMethod.name == "台灣Pay"))
    form = ExpenseForm(TODAY, 120, acc(db, "5101"), pm)
    txn_id = transactions.record_expense(db, form).transaction_id
    deactivate(session_factory, "5101", "5102")
    with session_factory() as s:
        s.get(PaymentMethod, pm).is_active = False
        s.get(
            PaymentMethod, s.scalar(select(PaymentMethod.id).where(PaymentMethod.name == "現金"))
        ).is_active = False
        s.commit()
    html = client.get(f"/txn/{txn_id}/edit").text
    accounts = option_labels(html, "account_id")
    assert "正餐（已停用）" in accounts and "飲料" not in " ".join(accounts)
    assert selected_value(html, "account_id") == str(acc(db, "5101"))
    methods = option_labels(html, "payment_method_id")
    assert "台灣Pay（已停用）" in methods and "現金" not in " ".join(methods)
    assert selected_value(html, "payment_method_id") == str(pm)

    data = {
        "csrf_token": csrf_from(client, "/txn/new"),
        "txn_date": TODAY.isoformat(),
        "amount": "120",
        "account_id": str(acc(db, "5101")),
        "payment_method_id": str(pm),
        "description": "只改備註",
        "action": "save",
    }
    before = state(session_factory, txn_id)
    assert ok(client.post(f"/txn/{txn_id}/edit", data=data, headers=HX))
    after = state(session_factory, txn_id)
    assert after[2] == "只改備註" and after[4] == before[4]
    assert [a for a, _ in audits(session_factory, txn_id)] == ["CREATE", "UPDATE"]


def test_inactive_current_income_accounts(client, db, session_factory):
    txn_id = income(db)
    deactivate(session_factory, "4201", "4101")
    html = client.get(f"/txn/{txn_id}/edit").text
    assert option_labels(html, "account_id") == ["利息收入（已停用）"]
    assert selected_value(html, "account_id") == str(acc(db, "4201"))
    before = state(session_factory, txn_id)
    assert ok(edit(client, txn_id, "income", description="只改備註"))
    assert state(session_factory, txn_id)[4] == before[4]
    assert [a for a, _ in audits(session_factory, txn_id)] == ["CREATE", "UPDATE"]


def test_inactive_current_transfer_accounts(client, db, session_factory):
    txn_id = transfer(db, fee_amount=15, fee_account_id=acc(db, "5501"))
    deactivate(session_factory, "1103", "5501", "1101", "5502")
    html = client.get(f"/txn/{txn_id}/edit").text
    assert "聯邦活存（已停用）" in option_labels(html, "to_account_id")
    assert selected_value(html, "to_account_id") == str(acc(db, "1103"))
    assert "其他手續費（已停用）" in option_labels(html, "fee_account_id")
    # 其他停用值不列；目前值只出現在它自己的選單
    assert not any("現金" in x for x in option_labels(html, "from_account_id"))
    assert not any("聯邦活存" in x for x in option_labels(html, "from_account_id"))
    assert not any("海外交易手續費" in x for x in option_labels(html, "fee_account_id"))
    before = state(session_factory, txn_id)
    assert ok(edit(client, txn_id, "transfer", description="只改備註"))
    assert state(session_factory, txn_id)[4] == before[4]


def test_inactive_current_card(client, db, session_factory):
    txn_id = card_payment(db)
    deactivate(session_factory, "2101")
    html = client.get(f"/txn/{txn_id}/edit").text
    assert option_labels(html, "to_account_id") == ["聯邦信用卡（已停用）"]
    assert ok(edit(client, txn_id, "transfer", description="只改備註"))
    assert state(session_factory, txn_id)[0] == "CARD_PAYMENT"


def test_switching_to_another_inactive_account_is_rejected_by_p1(client, db, session_factory):
    txn_id = income(db)
    deactivate(session_factory, "4101")
    before = state(session_factory, txn_id)
    resp = edit(client, txn_id, "income", account_id=acc(db, "4101"))
    assert resp.status_code == 422
    assert state(session_factory, txn_id) == before
