# ruff: noqa: F811  （共用 test_web_txn_new.py 的 fixture：pytest 依參數名稱注入，不是重複定義）
"""交易表單、明細、搜尋的標籤（P2 Step 8B，Web 層）。

寫入關聯的原子性在 8A（tests/integration/test_transaction_tags.py）；這裡確認 Web 的流程：
解析與建立標籤的時機、修改語意（完整一組／空字串清空／沒有欄位不處理）、V04、重送、明細、搜尋。
"""

import re

from sqlalchemy import func, select, update

from app.models import AuditLog, PaymentMethod, Tag, Transaction
from app.repositories import tags as tag_repo
from app.services import tags, transactions
from app.services.entries import ExpenseForm, RefundForm
from tests.integration.test_web_txn_income_transfer import post
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
    fresh_tokens,
    hidden,
    input_value,
    log_dir,
    selected_value,
    session_factory,
    submit,
    user_id,
)
from tests.ledger_helpers import TODAY, acc


def tag_names_of(session_factory, txn_id) -> list[str]:
    with session_factory() as s:
        return [t.name for t in tag_repo.tags_for_transaction(s, txn_id)]


def all_tags(session_factory) -> list[str]:
    with session_factory() as s:
        return [t.name for t in tag_repo.list_all(s)]


def last_txn(session_factory) -> int:
    with session_factory() as s:
        return s.scalar(select(func.max(Transaction.id)))


def deactivate(session_factory, name):
    with session_factory() as s:
        s.execute(update(Tag).where(Tag.name == name).values(is_active=False))
        s.commit()


def make_tags(db, *names) -> tuple[int, ...]:
    return tags.resolve_or_create_many(db, list(names))


def pm(db, name="LINE Pay") -> int:
    return db.scalar(select(PaymentMethod.id).where(PaymentMethod.name == name))


# --- 新增 ------------------------------------------------------------------------------


def test_forms_have_tags_field_with_active_suggestions(client, db, session_factory):
    make_tags(db, "旅行", "舊的")
    deactivate(session_factory, "舊的")
    for t in ("expense", "income", "transfer"):
        html = client.get("/txn/new", params={"type": t}).text
        assert '<input id="tags" name="tags"' in html, t
        assert '<option value="旅行">' in html and '<option value="舊的">' not in html


def test_expense_create_with_new_and_existing_tags(client, db, session_factory):
    make_tags(db, "Tokyo Trip")
    resp = submit(client, db, tags="ｔｏｋｙｏ　ｔｒｉｐ、聚餐, 聚餐，  ")
    assert resp.status_code == 200
    txn_id = last_txn(session_factory)
    assert sorted(tag_names_of(session_factory, txn_id)) == sorted(["Tokyo Trip", "聚餐"])
    assert sorted(all_tags(session_factory)) == sorted(["Tokyo Trip", "聚餐"])
    # 儲存並記下一筆：標籤清空
    assert input_value(resp.text, "tags") == ""


def test_income_transfer_and_card_payment_create_with_tags(client, db, session_factory):
    post(client, db, "income", tags="旅行")
    assert tag_names_of(session_factory, last_txn(session_factory)) == ["旅行"]
    post(client, db, "transfer", tags="旅行")
    assert tag_names_of(session_factory, last_txn(session_factory)) == ["旅行"]
    post(client, db, "transfer", to_account_id=acc(db, "2101"), tags="繳卡")
    with session_factory() as s:
        txn = s.get(Transaction, last_txn(session_factory))
        assert txn.txn_type == "CARD_PAYMENT"
    assert tag_names_of(session_factory, last_txn(session_factory)) == ["繳卡"]


def test_inactive_tag_is_v04_on_tags_field(client, db, session_factory):
    make_tags(db, "舊的")
    deactivate(session_factory, "舊的")
    resp = submit(client, db, tags="舊的")
    assert resp.status_code == 422
    assert field_error_codes(resp.text, "tags") == ["V04"]
    assert input_value(resp.text, "tags") == "舊的"
    assert all_tags(session_factory) == ["舊的"]  # 沒有另建一個


def test_parse_error_keeps_tags_and_orphan_tag_tradeoff(client, db, session_factory):
    # 型態解析錯誤：還沒到建立標籤的步驟，不會建立
    resp = submit(client, db, amount="abc", tags="新標籤")
    assert resp.status_code == 422 and input_value(resp.text, "tags") == "新標籤"
    assert all_tags(session_factory) == []
    # P1 擋下（V05 缺科目）：標籤已先建立，留下沒有交易引用的主資料（決策 M2 的取捨）
    resp = submit(client, db, account_id="", tags="新標籤")
    assert resp.status_code == 422
    assert all_tags(session_factory) == ["新標籤"]
    with session_factory() as s:
        assert s.scalar(select(func.count()).select_from(Transaction)) == 0


def test_confirmation_keeps_tags(client, db, session_factory):
    resp = submit(client, db, amount=12000, tags="旅行")
    assert resp.status_code == 409 and hidden(resp.text, "tags") == "旅行"
    ok = submit(client, db, amount=12000, tags="旅行", confirmed="V10")
    assert ok.status_code == 200
    assert tag_names_of(session_factory, last_txn(session_factory)) == ["旅行"]
    assert all_tags(session_factory) == ["旅行"]


def test_replay_does_not_create_tags(client, db, session_factory):
    csrf, token = fresh_tokens(client)
    submit(client, db, submission_token=token, tags="旅行")
    again = submit(client, db, submission_token=token, tags="另一個標籤")
    assert 'data-replayed="true"' in again.text
    assert all_tags(session_factory) == ["旅行"]


# --- 修改 ------------------------------------------------------------------------------


def expense_with_tags(db, *names) -> int:
    form = ExpenseForm(TODAY, 120, acc(db, "5101"), pm(db))
    return transactions.record_expense(db, form, tag_ids=make_tags(db, *names)).transaction_id


def edit(client, txn_id, **overrides):
    html = client.get(f"/txn/{txn_id}/edit").text
    data = {
        "csrf_token": csrf_from(client, "/txn/new"),
        "txn_date": input_value(html, "txn_date"),
        "amount": input_value(html, "amount"),
        "account_id": selected_value(html, "account_id") or "",
        "payment_method_id": selected_value(html, "payment_method_id") or "",
        "tags": input_value(html, "tags"),
        "action": "save",
    }
    data.update({k: v for k, v in overrides.items()})
    data = {k: v for k, v in data.items() if v is not None}  # tags=None：模擬沒有標籤欄位
    return client.post(f"/txn/{txn_id}/edit", data=data, headers=HX)


def test_edit_prefill_replace_and_clear(client, db, session_factory):
    txn_id = expense_with_tags(db, "旅行", "聚餐")
    html = client.get(f"/txn/{txn_id}/edit").text
    assert sorted(input_value(html, "tags").split("、")) == sorted(["旅行", "聚餐"])
    assert edit(client, txn_id, tags="旅行、東京").status_code == 200
    assert sorted(tag_names_of(session_factory, txn_id)) == sorted(["旅行", "東京"])
    assert edit(client, txn_id, tags="").status_code == 200
    assert tag_names_of(session_factory, txn_id) == []
    history = client.get(f"/txn/{txn_id}").text
    assert "標籤：" in history


def test_edit_confirmation_carries_cleared_tags(client, db, session_factory):
    txn_id = expense_with_tags(db, "旅行")
    resp = edit(client, txn_id, amount="12000", tags="")
    assert resp.status_code == 409
    assert hidden(resp.text, "tags") == ""
    ok = edit(client, txn_id, amount="12000", tags="", confirmed="V10")
    assert ok.status_code == 200
    assert tag_names_of(session_factory, txn_id) == []


def test_edit_tags_field_semantics(client, db, session_factory):
    """A：tags="" → 清空；B：沒有 tags 欄位 → 保留；C：tags="旅行" → 整組替換。
    （FastAPI 會把 Form 的空字串當成未提供，所以「有沒有這個欄位」由 FormData 判斷。）"""
    txn_id = expense_with_tags(db, "旅行", "聚餐")
    assert edit(client, txn_id, tags=None, amount="130").status_code == 200  # B
    assert sorted(tag_names_of(session_factory, txn_id)) == sorted(["旅行", "聚餐"])
    assert edit(client, txn_id, tags="旅行").status_code == 200  # C
    assert tag_names_of(session_factory, txn_id) == ["旅行"]
    assert edit(client, txn_id, tags="").status_code == 200  # A
    assert tag_names_of(session_factory, txn_id) == []
    assert 'name="tags_present"' not in client.get(f"/txn/{txn_id}/edit").text


def test_edit_without_tags_field_keeps_tags(client, db, session_factory):
    txn_id = expense_with_tags(db, "旅行")
    assert edit(client, txn_id, tags=None, amount="150").status_code == 200
    assert tag_names_of(session_factory, txn_id) == ["旅行"]


def test_edit_other_fields_keeps_tags(client, db, session_factory):
    txn_id = expense_with_tags(db, "旅行")
    assert edit(client, txn_id, amount="150").status_code == 200
    assert tag_names_of(session_factory, txn_id) == ["旅行"]


def test_edit_keeps_inactive_current_and_blocks_new_inactive(client, db, session_factory):
    txn_id = expense_with_tags(db, "旅行")
    make_tags(db, "舊的")
    deactivate(session_factory, "旅行")
    deactivate(session_factory, "舊的")
    html = client.get(f"/txn/{txn_id}/edit").text
    assert input_value(html, "tags") == "旅行"
    assert '<option value="旅行">' not in html  # 建議清單不列停用的
    assert edit(client, txn_id, tags="旅行、聚餐").status_code == 200
    assert sorted(tag_names_of(session_factory, txn_id)) == sorted(["旅行", "聚餐"])
    resp = edit(client, txn_id, tags="旅行、舊的")
    assert resp.status_code == 422 and field_error_codes(resp.text, "tags") == ["V04"]
    assert sorted(tag_names_of(session_factory, txn_id)) == sorted(["旅行", "聚餐"])


def test_card_payment_edit_tags(client, db, session_factory):
    post(client, db, "transfer", to_account_id=acc(db, "2101"), tags="繳卡")
    txn_id = last_txn(session_factory)
    html = client.get(f"/txn/{txn_id}/edit").text
    resp = client.post(
        f"/txn/{txn_id}/edit",
        data={
            "csrf_token": csrf_from(client, "/txn/new"),
            "txn_date": input_value(html, "txn_date"),
            "amount": input_value(html, "amount"),
            "from_account_id": selected_value(html, "from_account_id"),
            "to_account_id": selected_value(html, "to_account_id"),
            "tags": "繳卡、十月",
            "action": "save",
        },
        headers=HX,
    )
    assert resp.status_code == 200
    assert sorted(tag_names_of(session_factory, txn_id)) == sorted(["繳卡", "十月"])


# --- 明細、退款 -------------------------------------------------------------------------


def test_detail_shows_tags_inactive_and_deleted(client, db, session_factory):
    txn_id = expense_with_tags(db, "旅行", "聚餐")
    deactivate(session_factory, "聚餐")
    transactions.delete_transaction(db, txn_id)
    html = client.get(f"/txn/{txn_id}").text
    assert re.search(r"data-tags>.*旅行.*聚餐（已停用）", html, re.S)


def test_refund_has_no_tags_input_and_detail_shows_inherited(client, db, session_factory):
    original = expense_with_tags(db, "旅行", "東京")
    page = client.get(f"/txn/{original}/refund").text
    assert 'name="tags"' not in page
    r = transactions.record_refund(db, RefundForm(original, TODAY, 50, pm(db))).transaction_id
    assert tag_names_of(session_factory, r) == []
    html = client.get(f"/txn/{r}").text
    assert "標籤（沿用原交易）" in html and "data-tags>" not in html
    assert re.search(r"data-inherited-tags>.*旅行.*東京", html, re.S)
    plain = expense_with_tags(db)
    r2 = transactions.record_refund(db, RefundForm(plain, TODAY, 50, pm(db))).transaction_id
    assert "沿用原交易" not in client.get(f"/txn/{r2}").text


# --- 搜尋 ------------------------------------------------------------------------------


def test_refund_merchant_presentation_follows_original_current_merchant(
    client, db, session_factory
):
    """原交易商家 A + 退款 → 原交易改成 B → 搜 B：退款列與明細都顯示 B（沿用原交易），
    和搜尋分類一致；退款自己存的 merchant_id 仍是 A（不改寫歷史）。"""
    from app.models import Merchant

    a, b = Merchant(name="商家A"), Merchant(name="商家B")
    db.add_all([a, b])
    db.commit()
    form = ExpenseForm(TODAY, 1000, acc(db, "5201"), pm(db), merchant_id=a.id)
    original = transactions.record_expense(db, form).transaction_id
    r = transactions.record_refund(db, RefundForm(original, TODAY, 300, pm(db))).transaction_id
    form_b = ExpenseForm(TODAY, 1000, acc(db, "5201"), pm(db), merchant_id=b.id)
    transactions.update_transaction(db, original, form_b)

    html = client.get("/txn", params={"merchant": "商家B", "date_from": "", "date_to": ""}).text
    assert re.search(r'data-count="2" data-expense-net="700"', html)
    row = re.search(rf'data-txn-id="{r}".*?</li>', html, re.S).group(0)
    assert "商家B" in row and "商家A" not in row
    detail = client.get(f"/txn/{r}").text
    assert "商家（沿用原交易）" in detail
    assert re.search(r"<dd data-merchant>商家B</dd>", detail)
    with session_factory() as s:
        assert s.get(Transaction, r).merchant_id == a.id
    # 一般交易仍顯示自己的商家、沒有「沿用」標示
    assert "沿用原交易" not in client.get(f"/txn/{original}").text.split("分錄")[0]


def test_search_by_tag_with_refund(client, db, session_factory):
    original = expense_with_tags(db, "旅行")
    transactions.record_refund(db, RefundForm(original, TODAY, 20, pm(db)))
    expense_with_tags(db, "聚餐")
    html = client.get("/txn", params={"tag": "旅行", "date_from": "", "date_to": ""}).text
    m = re.search(r'data-count="(\d+)" data-expense-net="(-?\d+)"', html)
    assert m.groups() == ("2", "100")
    missing = client.get("/txn", params={"tag": "沒有", "date_from": "", "date_to": ""}).text
    assert 'data-empty="tag"' in missing
    assert all_tags(session_factory) == sorted(["旅行", "聚餐"]) or set(
        all_tags(session_factory)
    ) == {"旅行", "聚餐"}


def test_audit_counts_unchanged_by_tag_master_data(client, db, session_factory):
    submit(client, db, tags="旅行")
    with session_factory() as s:
        assert s.scalar(select(func.count()).select_from(AuditLog)) == 1
