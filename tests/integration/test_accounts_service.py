"""科目樹與科目維護（規格 9.2 M01–M04；決策 D3、S8–S10、S1-a）。

M01／M03 需要「已有分錄」的狀態，但 LedgerService 在 Step 3 才有，
所以這裡的 add_posting() 直接寫分錄——只限測試使用。
"""

import pytest

from app.models import Account, Posting, Transaction
from app.repositories import accounts as account_repo
from app.services import accounts as svc
from app.services import credit_cards
from app.services import payment_methods as pm_service
from app.services.errors import NotFound, RuleViolation


def by_code(db, code: str) -> Account:
    account = account_repo.get_by_code(db, code)
    assert account is not None, code
    return account


def add_posting(db, account: Account, amount: int = 100) -> None:
    """測試專用：繞過 LedgerService 直接建立一筆分錄。"""
    txn = Transaction(txn_date="2026-10-11", txn_type="MANUAL")
    txn.postings = [Posting(line_no=1, account_id=account.id, amount=amount)]
    db.add(txn)
    db.commit()


def assert_rule(rule: str, fn, /, *args, **kwargs) -> None:
    with pytest.raises(RuleViolation) as exc_info:
        fn(*args, **kwargs)
    assert exc_info.value.code == rule, exc_info.value.message


def new_child(db, parent_code: str, code: str = "5199", name: str = "新子科目", **kwargs):
    return svc.create_account(
        db, code=code, name=name, parent_id=by_code(db, parent_code).id, **kwargs
    )


# --- 科目樹與最末層 -----------------------------------------------------------------


def test_tree_orders_by_sort_order_then_code(seeded_db):
    roots = svc.tree(seeded_db)
    assert [n.account.code for n in roots][:3] == ["1101", "1102", "1103"]
    dining = next(n for n in roots if n.account.code == "5100")
    assert [n.account.name for n in dining.children] == ["正餐", "飲料"]
    assert not dining.is_leaf


def test_leaf_accounts_excludes_groups(seeded_db):
    codes = {a.code for a in svc.leaf_accounts(seeded_db)}
    assert {"5101", "5102", "1101", "2101", "3101"} <= codes
    assert "5100" not in codes


def test_parent_stays_non_leaf_when_all_children_inactive(seeded_db):
    # 決策 D3：最末層 = 沒有任何子科目，不論子科目是否啟用
    games = by_code(seeded_db, "5303")
    concert = by_code(seeded_db, "5301")
    svc.deactivate_account(seeded_db, games.id)
    svc.deactivate_account(seeded_db, concert.id)

    entertainment = by_code(seeded_db, "5300")
    assert not svc.is_leaf(seeded_db, entertainment.id)
    assert "5300" not in {a.code for a in svc.leaf_accounts(seeded_db, active_only=False)}


def test_leaf_accounts_active_only(seeded_db):
    games = by_code(seeded_db, "5303")
    svc.deactivate_account(seeded_db, games.id)
    assert "5303" not in {a.code for a in svc.leaf_accounts(seeded_db)}
    assert "5303" in {a.code for a in svc.leaf_accounts(seeded_db, active_only=False)}


def test_tree_can_hide_inactive(seeded_db):
    svc.deactivate_account(seeded_db, by_code(seeded_db, "5303").id)
    entertainment = next(n for n in svc.tree(seeded_db, False) if n.account.code == "5300")
    assert [n.account.code for n in entertainment.children] == ["5301"]


def test_unknown_account_raises_not_found(seeded_db):
    with pytest.raises(NotFound):
        svc.is_leaf(seeded_db, 999_999)


# --- 新增 -------------------------------------------------------------------------


def test_create_child_inherits_class(seeded_db):
    child = new_child(seeded_db, "5100", name="宵夜")
    assert child.account_class == "EXPENSE"
    assert child.parent_id == by_code(seeded_db, "5100").id
    assert svc.is_leaf(seeded_db, child.id)


def test_create_top_level_account(seeded_db):
    account = svc.create_account(
        seeded_db, code="1401", name="應收-小明", account_class="ASSET", subtype="RECEIVABLE"
    )
    assert account.parent_id is None
    assert account.subtype == "RECEIVABLE"


def test_child_class_must_match_parent(seeded_db):
    assert_rule("M02", new_child, seeded_db, "5100", account_class="INCOME")


def test_cannot_add_child_to_account_with_postings(seeded_db):
    add_posting(seeded_db, by_code(seeded_db, "5101"))
    assert_rule("M03", new_child, seeded_db, "5101", code="510101", name="便當")


def test_cannot_add_child_to_inactive_parent(seeded_db):
    svc.deactivate_account(seeded_db, by_code(seeded_db, "5303").id)
    assert_rule("PARENT_INACTIVE", new_child, seeded_db, "5303", code="530301")


def test_cannot_add_child_under_tax_category_until_cleared(seeded_db):
    # S10-a：要先清除上層的稅務分類，系統不會偷偷清掉
    health = by_code(seeded_db, "5602")
    assert_rule("S10", new_child, seeded_db, "5602", code="560201", name="補充保費")
    assert health.tax_category == "INSURANCE"

    svc.set_tax_category(seeded_db, health.id, None)
    child = new_child(seeded_db, "5602", code="560201", name="補充保費", tax_category="INSURANCE")
    assert child.tax_category == "INSURANCE"


def test_cannot_add_child_under_payment_method_account(seeded_db):
    assert_rule(
        "PM_ACCOUNT",
        svc.create_account,
        seeded_db,
        code="110201",
        name="子帳戶",
        parent_id=by_code(seeded_db, "1102").id,
        subtype="BANK",
    )


def test_cannot_add_child_under_credit_card_profile_account(seeded_db):
    # 1103 是信用卡的扣繳帳戶，也是「聯邦轉帳」的扣款帳戶；先停用付款方式只剩信用卡設定擋下
    pm = next(p for p in pm_service.list_payment_methods(seeded_db) if p.name == "聯邦轉帳")
    pm_service.deactivate_payment_method(seeded_db, pm.id)
    seeded_db.delete(pm)
    seeded_db.commit()
    assert_rule(
        "CARD_ACCOUNT",
        svc.create_account,
        seeded_db,
        code="110301",
        name="子帳戶",
        parent_id=by_code(seeded_db, "1103").id,
        subtype="BANK",
    )


@pytest.mark.parametrize(
    ("account_class", "subtype"),
    [
        ("ASSET", None),
        ("LIABILITY", None),
        ("ASSET", "CREDIT_CARD"),
        ("ASSET", "PAYABLE"),
        ("LIABILITY", "BANK"),
        ("LIABILITY", "RECEIVABLE"),
        ("EXPENSE", "OTHER"),
        ("EQUITY", "OTHER"),
    ],
)
def test_subtype_must_match_class(seeded_db, account_class, subtype):
    assert_rule(
        "SUBTYPE",
        svc.create_account,
        seeded_db,
        code="9999",
        name="測試",
        account_class=account_class,
        subtype=subtype,
    )


@pytest.mark.parametrize(
    ("account_class", "subtype"),
    [("ASSET", "OTHER"), ("LIABILITY", "OTHER"), ("LIABILITY", "PAYABLE")],
)
def test_subtype_allowed(seeded_db, account_class, subtype):
    account = svc.create_account(
        seeded_db, code="9999", name="測試", account_class=account_class, subtype=subtype
    )
    assert account.subtype == subtype


def test_credit_card_account_can_exist_without_profile(seeded_db):
    # S1-a：信用卡帳戶可以先建立，「尚未設定信用卡週期」
    card = svc.create_account(
        seeded_db, code="2102", name="新信用卡", account_class="LIABILITY", subtype="CREDIT_CARD"
    )
    assert credit_cards.get_profile(seeded_db, card.id) is None


def test_sibling_names_must_be_unique(seeded_db):
    assert_rule("S9", new_child, seeded_db, "5100", name="正餐")
    assert_rule(
        "S9", svc.create_account, seeded_db, code="5998", name="餐飲", account_class="EXPENSE"
    )
    # 不同上層可以同名
    assert new_child(seeded_db, "5200", name="正餐").name == "正餐"


def test_code_must_be_unique(seeded_db):
    assert_rule("CODE_TAKEN", new_child, seeded_db, "5100", code="5101", name="另一個")


@pytest.mark.parametrize(("code", "name"), [("", "名稱"), ("9999", "   ")])
def test_code_and_name_required(seeded_db, code, name):
    assert_rule(
        "REQUIRED", svc.create_account, seeded_db, code=code, name=name, account_class="EXPENSE"
    )


def test_create_account_cannot_set_system_key():
    # 系統科目只由 seed 建立：create_account 沒有 system_key 參數
    import inspect

    assert "system_key" not in inspect.signature(svc.create_account).parameters


def test_top_level_requires_class(seeded_db):
    assert_rule("CLASS", svc.create_account, seeded_db, code="9999", name="沒有類別")


# --- 改名與稅務分類 -----------------------------------------------------------------


def test_rename_account(seeded_db):
    lunch = by_code(seeded_db, "5101")
    assert svc.rename_account(seeded_db, lunch.id, "  午晚餐 ").name == "午晚餐"


def test_rename_respects_sibling_uniqueness(seeded_db):
    assert_rule("S9", svc.rename_account, seeded_db, by_code(seeded_db, "5101").id, "飲料")


def test_rename_system_account_is_allowed(seeded_db):
    opening = by_code(seeded_db, "3101")
    assert svc.rename_account(seeded_db, opening.id, "期初淨值").name == "期初淨值"
    assert opening.system_key == "OPENING_EQUITY"


def test_tax_category_only_on_leaf(seeded_db):
    assert_rule("S10", svc.set_tax_category, seeded_db, by_code(seeded_db, "5600").id, "INSURANCE")
    lunch = svc.set_tax_category(seeded_db, by_code(seeded_db, "5101").id, "MEDICAL")
    assert lunch.tax_category == "MEDICAL"


def test_tax_category_can_always_be_cleared(seeded_db):
    salary = by_code(seeded_db, "4101")
    assert svc.set_tax_category(seeded_db, salary.id, None).tax_category is None


def test_unknown_tax_category_rejected(seeded_db):
    assert_rule(
        "TAX_CATEGORY", svc.set_tax_category, seeded_db, by_code(seeded_db, "5101").id, "BONUS"
    )


# --- 停用與啟用 ---------------------------------------------------------------------


def test_deactivate_and_reactivate_leaf(seeded_db):
    games = by_code(seeded_db, "5303")
    assert svc.deactivate_account(seeded_db, games.id).is_active is False
    assert svc.activate_account(seeded_db, games.id).is_active is True


def test_cannot_deactivate_parent_with_active_children(seeded_db):
    assert_rule("ACTIVE_CHILDREN", svc.deactivate_account, seeded_db, by_code(seeded_db, "5300").id)


def test_deactivate_parent_after_children(seeded_db):
    for code in ("5301", "5303"):
        svc.deactivate_account(seeded_db, by_code(seeded_db, code).id)
    assert svc.deactivate_account(seeded_db, by_code(seeded_db, "5300").id).is_active is False


def test_cannot_reactivate_child_under_inactive_parent(seeded_db):
    for code in ("5301", "5303", "5300"):
        svc.deactivate_account(seeded_db, by_code(seeded_db, code).id)
    assert_rule("PARENT_INACTIVE", svc.activate_account, seeded_db, by_code(seeded_db, "5301").id)


@pytest.mark.parametrize("code", ["3101", "3102"])
def test_system_accounts_cannot_be_deactivated(seeded_db, code):
    assert_rule("SYSTEM", svc.deactivate_account, seeded_db, by_code(seeded_db, code).id)


def test_cannot_deactivate_account_used_by_active_payment_method(seeded_db):
    assert_rule("PM_ACCOUNT", svc.deactivate_account, seeded_db, by_code(seeded_db, "1101").id)


def test_cannot_deactivate_autopay_account(seeded_db):
    pm = next(p for p in pm_service.list_payment_methods(seeded_db) if p.name == "聯邦轉帳")
    pm_service.deactivate_payment_method(seeded_db, pm.id)
    assert_rule("CARD_AUTOPAY", svc.deactivate_account, seeded_db, by_code(seeded_db, "1103").id)


def test_deactivated_account_with_postings_keeps_history(seeded_db):
    games = by_code(seeded_db, "5303")
    add_posting(seeded_db, games)
    svc.deactivate_account(seeded_db, games.id)
    assert account_repo.has_postings(seeded_db, games.id)


# --- 刪除 ---------------------------------------------------------------------------


def test_delete_unused_leaf(seeded_db):
    games = by_code(seeded_db, "5303")
    svc.delete_account(seeded_db, games.id)
    assert account_repo.get_by_code(seeded_db, "5303") is None


def test_cannot_delete_account_with_postings(seeded_db):
    games = by_code(seeded_db, "5303")
    add_posting(seeded_db, games)
    assert_rule("M01", svc.delete_account, seeded_db, games.id)


def test_postings_on_deleted_transactions_still_block_delete(seeded_db):
    # 已刪除的交易隨時可能被還原，所以仍算「已有分錄」
    games = by_code(seeded_db, "5303")
    add_posting(seeded_db, games)
    txn = seeded_db.query(Transaction).one()
    txn.deleted_at = "2026-10-11T12:00:00+08:00"
    seeded_db.commit()
    assert_rule("M01", svc.delete_account, seeded_db, games.id)


def test_cannot_delete_group_with_children(seeded_db):
    assert_rule("HAS_CHILDREN", svc.delete_account, seeded_db, by_code(seeded_db, "5300").id)


@pytest.mark.parametrize("code", ["3101", "3102"])
def test_system_accounts_cannot_be_deleted(seeded_db, code):
    assert_rule("SYSTEM", svc.delete_account, seeded_db, by_code(seeded_db, code).id)


def test_cannot_delete_account_used_by_payment_method(seeded_db):
    assert_rule("PM_ACCOUNT", svc.delete_account, seeded_db, by_code(seeded_db, "1101").id)


def test_delete_credit_card_removes_its_profile(seeded_db):
    card = by_code(seeded_db, "2101")
    pm = next(p for p in pm_service.list_payment_methods(seeded_db) if p.name == "聯邦信用卡")
    seeded_db.delete(pm)
    seeded_db.commit()
    svc.delete_account(seeded_db, card.id)
    assert credit_cards.get_profile(seeded_db, card.id) is None
    assert account_repo.get_by_code(seeded_db, "2101") is None


def test_cannot_delete_autopay_account(seeded_db):
    pm = next(p for p in pm_service.list_payment_methods(seeded_db) if p.name == "聯邦轉帳")
    seeded_db.delete(pm)
    seeded_db.commit()
    assert_rule("CARD_AUTOPAY", svc.delete_account, seeded_db, by_code(seeded_db, "1103").id)


def test_cannot_delete_merchant_default_account(seeded_db):
    from app.models import Merchant

    games = by_code(seeded_db, "5303")
    seeded_db.add(Merchant(name="Steam", default_account_id=games.id))
    seeded_db.commit()
    assert_rule("MERCHANT_DEFAULT", svc.delete_account, seeded_db, games.id)


def test_no_move_account_api():
    # S8／M04：V1 不提供搬移科目，上層只能在新增時指定既有科目，不會形成迴圈
    assert not any(name.startswith(("move", "change_parent", "set_parent")) for name in dir(svc))
