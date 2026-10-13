"""標籤服務與搜尋的分類語意（P2 Step 8B）。

F = 符合條件的有效交易，一筆交易一列；筆數 = |F|、分頁 = F、
支出淨額／收入合計 = 有效分錄 WHERE transaction_id IN F。
商家、標籤是分類維度：退款跟著原交易「目前」的商家與標籤；日期、金額看退款自己。
"""

from datetime import date

import pytest
from sqlalchemy import select, update

from app.models import Merchant, PaymentMethod, Tag
from app.repositories import tags as tag_repo
from app.services import search, tags, transactions
from app.services.entries import ExpenseForm, IncomeForm, RefundForm, TransferForm
from app.services.search import SearchFilters
from tests.ledger_helpers import TODAY, acc, freeze_today


@pytest.fixture(autouse=True)
def _today(monkeypatch):
    freeze_today(monkeypatch)


def pm(db, name="LINE Pay") -> int:
    return db.scalar(select(PaymentMethod.id).where(PaymentMethod.name == name))


def tag_ids(db, *names) -> tuple[int, ...]:
    return tags.resolve_or_create_many(db, list(names))


def expense(db, *tag_names, amount=1000, fee=None, merchant_id=None) -> int:
    form = ExpenseForm(
        TODAY,
        amount,
        acc(db, "5201"),
        pm(db),
        fee_amount=fee,
        fee_account_id=acc(db, "5502") if fee else None,
        merchant_id=merchant_id,
    )
    ids = tag_ids(db, *tag_names) if tag_names else None
    return transactions.record_expense(db, form, tag_ids=ids).transaction_id


def refund(db, original, amount=300) -> int:
    return transactions.record_refund(
        db, RefundForm(original, TODAY, amount, pm(db))
    ).transaction_id


def find(db, *, tag=None, merchant=None, page=1):
    filters = SearchFilters(
        date_from=date(2026, 1, 1), date_to=date(2026, 12, 31), tag_name=tag, merchant_name=merchant
    )
    return search.search(db, filters, page=page)


def summary(result) -> tuple[list[int], int, int, int]:
    return (
        sorted(r.id for r in result.rows),
        result.total_count,
        result.expense_net,
        result.income_total,
    )


# --- 標籤服務 ---------------------------------------------------------------------------


def test_resolve_or_create_matches_normalized_and_keeps_first_spelling(seeded_db):
    [first] = tags.resolve_or_create_many(seeded_db, ["Tokyo Trip"])
    again = tags.resolve_or_create_many(seeded_db, ["ｔｏｋｙｏ　ｔｒｉｐ", "TOKYO  TRIP"])
    assert again == (first,)
    assert [t.name for t in tag_repo.list_all(seeded_db)] == ["Tokyo Trip"]
    [other] = tags.resolve_or_create_many(seeded_db, ["TokyoTrip"])
    assert other != first


def test_inactive_tag_is_returned_not_duplicated(seeded_db):
    [tid] = tags.resolve_or_create_many(seeded_db, ["舊標籤"])
    seeded_db.execute(update(Tag).where(Tag.id == tid).values(is_active=False))
    seeded_db.commit()
    assert tags.resolve_or_create_many(seeded_db, ["舊標籤"]) == (tid,)
    assert tags.active_names(seeded_db) == []
    assert tags.resolve(seeded_db, "舊標籤").id == tid


def test_create_race_falls_back_to_existing(seeded_db, monkeypatch):
    seeded_db.add(Tag(name="旅行"))
    seeded_db.commit()
    real = tag_repo.list_all
    calls = {"n": 0}

    def stale(db, active_only=False):
        calls["n"] += 1
        return [] if calls["n"] == 1 else real(db, active_only)

    monkeypatch.setattr(tag_repo, "list_all", stale)
    [tid] = tags.resolve_or_create_many(seeded_db, ["旅行"])
    assert [t.id for t in real(seeded_db)] == [tid]


# --- 搜尋：例子 A–H ----------------------------------------------------------------------


def test_a_refund_follows_original_tag(seeded_db):
    original = expense(seeded_db, "旅行", fee=15)
    r = refund(seeded_db, original)
    assert summary(find(seeded_db, tag="旅行")) == (sorted([original, r]), 2, 715, 0)


def test_b_c_deleted_and_restored_refund(seeded_db):
    original = expense(seeded_db, "旅行", fee=15)
    r = refund(seeded_db, original)
    transactions.delete_transaction(seeded_db, r)
    assert summary(find(seeded_db, tag="旅行")) == ([original], 1, 1015, 0)
    transactions.restore_transaction(seeded_db, r)
    assert summary(find(seeded_db, tag="旅行")) == (sorted([original, r]), 2, 715, 0)


def test_d_original_tags_changed(seeded_db):
    original = expense(seeded_db, "旅行", fee=15)
    r = refund(seeded_db, original)
    form = ExpenseForm(
        TODAY,
        1000,
        acc(seeded_db, "5201"),
        pm(seeded_db),
        fee_amount=15,
        fee_account_id=acc(seeded_db, "5502"),
    )
    transactions.update_transaction(seeded_db, original, form, tag_ids=tag_ids(seeded_db, "出差"))
    assert summary(find(seeded_db, tag="旅行")) == ([], 0, 0, 0)
    assert summary(find(seeded_db, tag="出差")) == (sorted([original, r]), 2, 715, 0)
    # 退款自己沒有寫入任何標籤
    assert tag_repo.ids_for_transaction(seeded_db, r) == ()


def test_e_merchant_refund_follows_original_current_merchant(seeded_db):
    a, b = Merchant(name="商家A"), Merchant(name="商家B")
    seeded_db.add_all([a, b])
    seeded_db.commit()
    original = expense(seeded_db, merchant_id=a.id)
    r = refund(seeded_db, original)
    assert summary(find(seeded_db, merchant="商家A")) == (sorted([original, r]), 2, 700, 0)
    form = ExpenseForm(TODAY, 1000, acc(seeded_db, "5201"), pm(seeded_db), merchant_id=b.id)
    transactions.update_transaction(seeded_db, original, form)
    assert summary(find(seeded_db, merchant="商家A")) == ([], 0, 0, 0)
    assert summary(find(seeded_db, merchant="商家B")) == (sorted([original, r]), 2, 700, 0)
    # 退款存的 merchant_id 仍是建立當下的值（不改歷史寫入）
    from app.models import Transaction

    assert seeded_db.get(Transaction, r).merchant_id == a.id


def test_f_multi_tag_transaction_is_one_row(seeded_db, monkeypatch):
    many = expense(seeded_db, "旅行", "聚餐", "東京", amount=500)
    single = expense(seeded_db, "旅行", amount=80)
    result = find(seeded_db, tag="旅行")
    assert summary(result) == (sorted([many, single]), 2, 580, 0)
    # 分頁：每頁 1 筆時兩頁各一筆、不重複
    monkeypatch.setattr(search, "PAGE_SIZE", 1)
    p1, p2 = find(seeded_db, tag="旅行", page=1), find(seeded_db, tag="旅行", page=2)
    assert p1.page_count == 2 and len(p1.rows) == len(p2.rows) == 1
    assert {p1.rows[0].id, p2.rows[0].id} == {many, single}


def test_g_transfer_with_fee_counts_only_fee(seeded_db):
    t = transactions.record_transfer(
        seeded_db,
        TransferForm(
            TODAY, 2000, acc(seeded_db, "1102"), acc(seeded_db, "1103"), 15, acc(seeded_db, "5501")
        ),
        tag_ids=tag_ids(seeded_db, "旅行"),
    ).transaction_id
    result = find(seeded_db, tag="旅行")
    assert summary(result) == ([t], 1, 15, 0)
    assert result.rows[0].amount == 2015


def test_h_income_with_tag(seeded_db):
    i = transactions.record_income(
        seeded_db,
        IncomeForm(TODAY, 3000, acc(seeded_db, "4201"), acc(seeded_db, "1102")),
        tag_ids=tag_ids(seeded_db, "旅行"),
    ).transaction_id
    assert summary(find(seeded_db, tag="旅行")) == ([i], 1, 0, 3000)


def test_all_together(seeded_db):
    original = expense(seeded_db, "旅行", fee=15)
    refund(seeded_db, original)
    expense(seeded_db, "旅行", "聚餐", "東京", amount=500)
    transactions.record_transfer(
        seeded_db,
        TransferForm(
            TODAY, 2000, acc(seeded_db, "1102"), acc(seeded_db, "1103"), 15, acc(seeded_db, "5501")
        ),
        tag_ids=tag_ids(seeded_db, "旅行"),
    )
    transactions.record_income(
        seeded_db,
        IncomeForm(TODAY, 3000, acc(seeded_db, "4201"), acc(seeded_db, "1102")),
        tag_ids=tag_ids(seeded_db, "旅行"),
    )
    expense(seeded_db, amount=999)  # 沒有標籤：不命中
    _, count, net, income = summary(find(seeded_db, tag="旅行"))
    assert (count, net, income) == (5, 1230, 3000)


def test_tag_search_inactive_not_found_and_deleted(seeded_db):
    t1 = expense(seeded_db, "舊標籤")
    [tid] = tag_ids(seeded_db, "舊標籤")
    seeded_db.execute(update(Tag).where(Tag.id == tid).values(is_active=False))
    seeded_db.commit()
    assert summary(find(seeded_db, tag="舊標籤"))[0] == [t1]  # 停用的可以搜
    missing = find(seeded_db, tag="沒有這個")
    assert missing.tag_not_found and missing.total_count == 0
    assert tag_repo.list_all(seeded_db) == [seeded_db.get(Tag, tid)]  # 搜尋不建立標籤
    transactions.delete_transaction(seeded_db, t1)
    assert summary(find(seeded_db, tag="舊標籤")) == ([], 0, 0, 0)  # 已刪除排除
