"""services/merchants.py 別名管理（P2 Step 9，F17）。

新增別名的檢查順序、resolve 的唯一性、停用商家、刪除語意、建議清單。
"""

import pytest
from sqlalchemy import func, select

from app.models import Merchant, MerchantAlias, Transaction
from app.services import merchants, search, transactions
from app.services.entries import ExpenseForm
from app.services.errors import NotFound, RuleViolation
from app.services.search import SearchFilters
from tests.ledger_helpers import TODAY, acc


@pytest.fixture
def shops(seeded_db):
    seven = Merchant(name="7-11")
    family = Merchant(name="全家")
    old = Merchant(name="舊店", is_active=False)
    seeded_db.add_all([seven, family, old])
    seeded_db.commit()
    return seven, family, old


def alias_count(db) -> int:
    return db.scalar(select(func.count()).select_from(MerchantAlias))


def code_of(db, merchant_id, text) -> str:
    with pytest.raises(RuleViolation) as exc:
        merchants.add_alias(db, merchant_id, text)
    return exc.value.code


# --- 新增 ----------------------------------------------------------------------------


def test_add_alias_stores_trimmed_text_and_normalized_key(seeded_db, shops):
    seven, _, _ = shops
    alias = merchants.add_alias(seeded_db, seven.id, "  統一 超商  ")
    assert (alias.alias, alias.alias_normalized, alias.merchant_id) == (
        "統一 超商",
        "統一超商",
        seven.id,
    )
    assert not (seeded_db.new or seeded_db.dirty or seeded_db.deleted)  # 已 commit


def test_alias_input_resolves_to_canonical_merchant(seeded_db, shops):
    seven, _, _ = shops
    merchants.add_alias(seeded_db, seven.id, "統一超商")
    for raw in ("統一超商", " 統一　超商 ", "統一超商"):
        assert merchants.resolve(seeded_db, raw) is seven
    assert merchants.resolve_or_create(seeded_db, "統一超商") is seven
    assert seeded_db.scalar(select(func.count()).select_from(Merchant)) == 3


@pytest.mark.parametrize("text", ["", "   ", "　"])
def test_blank_alias_rejected(seeded_db, shops, text):
    assert code_of(seeded_db, shops[0].id, text) == "ALIAS_BLANK"
    assert alias_count(seeded_db) == 0


def test_duplicate_alias_on_same_merchant_rejected_m06(seeded_db, shops):
    seven, _, _ = shops
    merchants.add_alias(seeded_db, seven.id, "Seven")
    assert code_of(seeded_db, seven.id, "ＳＥＶＥＮ") == "M06"
    assert alias_count(seeded_db) == 1


def test_duplicate_alias_on_other_merchant_rejected_m06(seeded_db, shops):
    seven, family, _ = shops
    merchants.add_alias(seeded_db, seven.id, "超商")
    with pytest.raises(RuleViolation) as exc:
        merchants.add_alias(seeded_db, family.id, " 超 商 ")
    assert exc.value.code == "M06"
    assert "7-11" in exc.value.message
    assert alias_count(seeded_db) == 1


def test_alias_equal_to_own_name_rejected(seeded_db, shops):
    assert code_of(seeded_db, shops[0].id, "７－１１") == "ALIAS_SAME_AS_NAME"
    assert alias_count(seeded_db) == 0


def test_alias_equal_to_other_merchant_name_rejected(seeded_db, shops):
    """迴歸：A 的別名不能是 B 的標準名稱，否則 resolve("全家") 會因為別名優先而變成 7-11。"""
    seven, family, _ = shops
    with pytest.raises(RuleViolation) as exc:
        merchants.add_alias(seeded_db, seven.id, " 全 家 ")
    assert exc.value.code == "ALIAS_NAME_CONFLICT"
    assert alias_count(seeded_db) == 0
    assert merchants.resolve(seeded_db, "全家") is family


def test_alias_equal_to_inactive_merchant_name_rejected(seeded_db, shops):
    seven, _, old = shops
    assert code_of(seeded_db, seven.id, "舊店") == "ALIAS_NAME_CONFLICT"
    assert merchants.resolve(seeded_db, "舊店") is old


def test_inactive_merchant_cannot_get_new_alias(seeded_db, shops):
    _, _, old = shops
    assert code_of(seeded_db, old.id, "老店") == "ALIAS_MERCHANT_INACTIVE"
    assert alias_count(seeded_db) == 0


def test_inactive_check_comes_before_text_checks(seeded_db, shops):
    _, _, old = shops
    assert code_of(seeded_db, old.id, "") == "ALIAS_MERCHANT_INACTIVE"


def test_validation_order_blank_before_duplicate(seeded_db, shops):
    seven, _, _ = shops
    merchants.add_alias(seeded_db, seven.id, "Seven")
    assert code_of(seeded_db, seven.id, " ") == "ALIAS_BLANK"


def test_add_alias_to_unknown_merchant(seeded_db, shops):
    with pytest.raises(NotFound):
        merchants.add_alias(seeded_db, 9999, "x")


def test_concurrent_duplicate_alias_reported_as_m06(seeded_db, shops, monkeypatch):
    """檢查通過後才被別的請求搶先加上（UNIQUE 擋下）：rollback 後回報 M06。"""
    seven, family, _ = shops
    real_check = merchants._check_new_alias
    calls = []

    def racing_check(db, merchant, text):
        key = real_check(db, merchant, text)
        if not calls:
            calls.append(1)
            db.add(MerchantAlias(merchant_id=family.id, alias="超商", alias_normalized=key))
            db.commit()
        return key

    monkeypatch.setattr(merchants, "_check_new_alias", racing_check)
    with pytest.raises(RuleViolation) as exc:
        merchants.add_alias(seeded_db, seven.id, "超商")
    assert exc.value.code == "M06"
    assert alias_count(seeded_db) == 1
    assert not (seeded_db.new or seeded_db.dirty or seeded_db.deleted)


# --- 刪除 ----------------------------------------------------------------------------


def _expense(db, merchant_id) -> int:
    from app.models import PaymentMethod

    pm = db.scalar(select(PaymentMethod.id).where(PaymentMethod.name == "LINE Pay"))
    form = ExpenseForm(
        txn_date=TODAY,
        amount=120,
        account_id=acc(db, "5101"),
        payment_method_id=pm,
        merchant_id=merchant_id,
    )
    return transactions.record_expense(db, form, confirmed=["V10", "V11", "V12"]).transaction_id


def test_remove_alias_keeps_transactions_and_merchant(seeded_db, shops):
    seven, _, _ = shops
    alias = merchants.add_alias(seeded_db, seven.id, "統一超商")
    txn_id = _expense(seeded_db, merchants.resolve(seeded_db, "統一超商").id)
    by_alias = search.search(seeded_db, SearchFilters(merchant_name="統一超商"))
    assert [r.id for r in by_alias.rows] == [txn_id]

    merchants.remove_alias(seeded_db, seven.id, alias.id)

    assert alias_count(seeded_db) == 0
    assert seeded_db.get(Transaction, txn_id).merchant_id == seven.id
    assert merchants.resolve(seeded_db, "統一超商") is None
    assert merchants.resolve(seeded_db, "7-11") is seven
    result = search.search(seeded_db, SearchFilters(merchant_name="7-11"))
    assert [r.id for r in result.rows] == [txn_id]
    gone = search.search(seeded_db, SearchFilters(merchant_name="統一超商"))
    assert gone.merchant_not_found and gone.rows == ()


def test_removed_alias_can_be_added_again(seeded_db, shops):
    seven, family, _ = shops
    alias = merchants.add_alias(seeded_db, seven.id, "超商")
    merchants.remove_alias(seeded_db, seven.id, alias.id)
    assert merchants.add_alias(seeded_db, family.id, "超商").merchant_id == family.id


def test_inactive_merchant_alias_can_be_removed(seeded_db, shops):
    _, _, old = shops
    seeded_db.add(MerchantAlias(merchant_id=old.id, alias="老店", alias_normalized="老店"))
    seeded_db.commit()
    alias_id = seeded_db.scalar(select(MerchantAlias.id))
    merchants.remove_alias(seeded_db, old.id, alias_id)
    assert alias_count(seeded_db) == 0


def test_remove_alias_must_belong_to_merchant(seeded_db, shops):
    seven, family, _ = shops
    alias = merchants.add_alias(seeded_db, seven.id, "統一超商")
    with pytest.raises(NotFound):
        merchants.remove_alias(seeded_db, family.id, alias.id)
    with pytest.raises(NotFound):
        merchants.remove_alias(seeded_db, seven.id, 9999)
    with pytest.raises(NotFound):
        merchants.remove_alias(seeded_db, 9999, alias.id)
    assert alias_count(seeded_db) == 1


# --- 列表與建議 ------------------------------------------------------------------------


def test_list_with_aliases_includes_inactive(seeded_db, shops):
    seven, _, old = shops
    merchants.add_alias(seeded_db, seven.id, "統一超商")
    merchants.add_alias(seeded_db, seven.id, "Seven")
    seeded_db.add(MerchantAlias(merchant_id=old.id, alias="老店", alias_normalized="老店"))
    seeded_db.commit()
    views = {v.name: v for v in merchants.list_with_aliases(seeded_db)}
    assert [a.alias for a in views["7-11"].aliases] == ["統一超商", "Seven"]
    assert views["舊店"].is_active is False
    assert [a.alias for a in views["舊店"].aliases] == ["老店"]
    assert views["全家"].aliases == ()


def test_suggestions_include_aliases_of_active_merchants_only(seeded_db, shops):
    seven, _, old = shops
    merchants.add_alias(seeded_db, seven.id, "統一超商")
    seeded_db.add(MerchantAlias(merchant_id=old.id, alias="老店", alias_normalized="老店"))
    seeded_db.commit()
    options = [(o.value, o.canonical) for o in merchants.suggestions(seeded_db)]
    assert options == [("7-11", None), ("統一超商", "7-11"), ("全家", None)]
    assert all(value not in ("舊店", "老店") for value, _ in options)
