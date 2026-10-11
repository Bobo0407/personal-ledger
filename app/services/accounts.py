"""科目樹與科目主資料維護（規格 7.1 accounts.py、9.2 M01–M04；P1 決策 D1–D3、S3、S8–S10）。

規則摘要：
- 最末層 = 沒有任何子科目（D3），不論子科目是否啟用；只有最末層可以記帳（V03，在 Step 3 檢查）。
- 新增子科目：上層必須存在且啟用中；五大類與上層相同（M02）；上層不能已有分錄（M03）；
  上層不能帶 tax_category（S10，需先用 set_tax_category(None) 清除）；
  上層不能是付款方式或信用卡設定正在使用的帳戶（否則它們會指向非末層科目）。
- 子類型：資產、負債必填，且必須屬於該類（SUBTYPE_CLASSES）；權益、收入、費用不能填。
- 同一個上層底下名稱不能重複（S9，第一層也比照）。
- account_class、subtype、system_key 建立後沒有修改的方法；V1 不提供搬移科目（S8），
  上層只能在新增時指定既有科目，所以不會形成迴圈（M04）。
- 系統科目（有 system_key）只由 seed 建立，不能停用、不能刪除。
- 停用：不能還有啟用中的子科目、不能還有啟用中的付款方式或信用卡設定使用它。
- 重新啟用：上層必須是啟用中。
- 刪除（實體刪除）：不能有任何分錄（M01）、不能有子科目、不能被付款方式、
  其他信用卡的扣繳設定或商家預設科目引用；信用卡自己的設定會一併刪除。
- P1 不為主資料異動寫 audit_logs（S7）。
"""

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.models import Account, CreditCardProfile
from app.models.enums import AccountClass, AccountSubtype, TaxCategory
from app.repositories import accounts as account_repo
from app.services.errors import NotFound, RuleViolation
from app.services.ledger_queries import balance  # noqa: F401  規格 7.1：accounts.balance()

# 子類型與五大類的歸屬（Step 2 拍板）
SUBTYPE_CLASSES: dict[str, frozenset[str]] = {
    AccountSubtype.CASH: frozenset({AccountClass.ASSET}),
    AccountSubtype.BANK: frozenset({AccountClass.ASSET}),
    AccountSubtype.RECEIVABLE: frozenset({AccountClass.ASSET}),
    AccountSubtype.PREPAID_TAX: frozenset({AccountClass.ASSET}),
    AccountSubtype.PENSION: frozenset({AccountClass.ASSET}),
    AccountSubtype.INVESTMENT: frozenset({AccountClass.ASSET}),
    AccountSubtype.CREDIT_CARD: frozenset({AccountClass.LIABILITY}),
    AccountSubtype.PAYABLE: frozenset({AccountClass.LIABILITY}),
    AccountSubtype.OTHER: frozenset({AccountClass.ASSET, AccountClass.LIABILITY}),
}

BALANCE_SHEET_CLASSES = frozenset({AccountClass.ASSET, AccountClass.LIABILITY})


# --- 純規則檢查（seed 也共用）--------------------------------------------------


def check_account_class(account_class: str | None) -> str:
    if account_class not in set(AccountClass):
        raise RuleViolation("CLASS", f"五大類必須是 {', '.join(AccountClass)} 其中之一")
    return str(account_class)


def check_subtype(account_class: str, subtype: str | None) -> None:
    if account_class in BALANCE_SHEET_CLASSES:
        if subtype is None:
            raise RuleViolation("SUBTYPE", "資產與負債科目必須指定子類型")
        if subtype not in SUBTYPE_CLASSES:
            raise RuleViolation("SUBTYPE", f"不認識的子類型：{subtype}")
        if account_class not in SUBTYPE_CLASSES[subtype]:
            raise RuleViolation("SUBTYPE", f"子類型 {subtype} 不能用在 {account_class} 科目")
    elif subtype is not None:
        raise RuleViolation("SUBTYPE", "只有資產與負債科目可以指定子類型")


def check_tax_category(tax_category: str | None) -> None:
    if tax_category is not None and tax_category not in set(TaxCategory):
        raise RuleViolation("TAX_CATEGORY", f"不認識的稅務分類：{tax_category}")


def clean_text(value: str | None, what: str) -> str:
    text = (value or "").strip()
    if not text:
        raise RuleViolation("REQUIRED", f"{what}不可空白")
    return text


# --- 查詢 -----------------------------------------------------------------------


@dataclass
class AccountNode:
    account: Account
    children: list["AccountNode"] = field(default_factory=list)

    @property
    def is_leaf(self) -> bool:
        return not self.children


def tree(db: Session, include_inactive: bool = True) -> list[AccountNode]:
    """整棵科目樹；同層依 sort_order、code 排序。

    include_inactive=False 時省略停用的科目，但「是否為最末層」仍以完整樹判斷，
    所以要用 is_leaf() 判斷能否記帳，不要用畫面上看到的子節點數。
    """
    accounts = account_repo.list_all(db)
    nodes = {a.id: AccountNode(a) for a in accounts if include_inactive or a.is_active}
    roots: list[AccountNode] = []
    for account in accounts:
        node = nodes.get(account.id)
        if node is None:
            continue
        parent = nodes.get(account.parent_id) if account.parent_id is not None else None
        (parent.children if parent else roots).append(node)
    return roots


def get_account(db: Session, account_id: int) -> Account:
    account = account_repo.get(db, account_id)
    if account is None:
        raise NotFound("科目", account_id)
    return account


def is_leaf(db: Session, account_id: int) -> bool:
    get_account(db, account_id)
    return not account_repo.has_children(db, account_id)


def leaf_accounts(db: Session, active_only: bool = True) -> list[Account]:
    return account_repo.leaf_accounts(db, active_only=active_only)


# --- 新增子科目前的檢查（seed 也共用）-------------------------------------------


def check_can_add_child(db: Session, parent: Account) -> None:
    if not parent.is_active:
        raise RuleViolation("PARENT_INACTIVE", f"「{parent.name}」已停用，不能新增子科目")
    if account_repo.has_postings(db, parent.id):
        raise RuleViolation(
            "M03", f"「{parent.name}」已經有分錄，不能再新增子科目（搬移分錄功能在 V1.5）"
        )
    if parent.tax_category is not None:
        raise RuleViolation(
            "S10",
            f"「{parent.name}」帶有稅務分類，只有最末層可以使用；請先清除它的稅務分類再新增子科目",
        )
    users = [pm.name for pm in account_repo.payment_methods_using(db, parent.id)]
    if users:
        raise RuleViolation(
            "PM_ACCOUNT",
            f"「{parent.name}」是付款方式「{'、'.join(users)}」的扣款帳戶，不能新增子科目",
        )
    if account_repo.is_referenced_by_card_profile(db, parent.id):
        raise RuleViolation("CARD_ACCOUNT", f"「{parent.name}」用於信用卡設定，不能新增子科目")


# --- 維護 -----------------------------------------------------------------------


def create_account(
    db: Session,
    *,
    code: str,
    name: str,
    account_class: str | None = None,
    parent_id: int | None = None,
    subtype: str | None = None,
    tax_category: str | None = None,
    show_on_home: bool = False,
    sort_order: int = 0,
) -> Account:
    """新增科目。系統科目（system_key）不能用這個方法建立，只由 seed 建立。

    信用卡帳戶可以先建立、暫無信用卡設定（S1-a），之後再用 credit_cards.set_profile() 補上。
    """
    code = clean_text(code, "科目代碼")
    name = clean_text(name, "科目名稱")
    if account_repo.get_by_code(db, code) is not None:
        raise RuleViolation("CODE_TAKEN", f"科目代碼 {code} 已經存在")

    if parent_id is not None:
        parent = get_account(db, parent_id)
        if account_class is not None and account_class != parent.account_class:
            raise RuleViolation(
                "M02", f"子科目的五大類必須與上層「{parent.name}」相同（{parent.account_class}）"
            )
        account_class = parent.account_class
        check_can_add_child(db, parent)
    account_class = check_account_class(account_class)
    check_subtype(account_class, subtype)
    check_tax_category(tax_category)
    if account_repo.sibling_name_taken(db, parent_id, name):
        raise RuleViolation("S9", f"同一層已經有名為「{name}」的科目")

    account = Account(
        code=code,
        name=name,
        account_class=account_class,
        parent_id=parent_id,
        subtype=subtype,
        tax_category=tax_category,
        show_on_home=show_on_home,
        sort_order=sort_order,
    )
    db.add(account)
    db.commit()
    return account


def rename_account(db: Session, account_id: int, new_name: str) -> Account:
    account = get_account(db, account_id)
    new_name = clean_text(new_name, "科目名稱")
    if account_repo.sibling_name_taken(db, account.parent_id, new_name, exclude_id=account.id):
        raise RuleViolation("S9", f"同一層已經有名為「{new_name}」的科目")
    account.name = new_name
    db.commit()
    return account


def set_tax_category(db: Session, account_id: int, tax_category: str | None) -> Account:
    """設定或清除（傳 None）稅務分類。只有最末層可以設定（S10）；清除永遠允許。"""
    account = get_account(db, account_id)
    check_tax_category(tax_category)
    if tax_category is not None and account_repo.has_children(db, account.id):
        raise RuleViolation("S10", f"「{account.name}」不是最末層科目，不能設定稅務分類")
    account.tax_category = tax_category
    db.commit()
    return account


def deactivate_account(db: Session, account_id: int) -> Account:
    account = get_account(db, account_id)
    if account.system_key is not None:
        raise RuleViolation("SYSTEM", f"「{account.name}」是系統科目，不能停用")
    if account_repo.has_active_children(db, account.id):
        raise RuleViolation("ACTIVE_CHILDREN", f"請先停用「{account.name}」底下的所有子科目")
    users = [pm.name for pm in account_repo.payment_methods_using(db, account.id, True)]
    if users:
        raise RuleViolation(
            "PM_ACCOUNT",
            f"付款方式「{'、'.join(users)}」還在使用「{account.name}」，請先停用或改設",
        )
    if account_repo.card_profiles_using_as_autopay(db, account.id):
        raise RuleViolation(
            "CARD_AUTOPAY", f"「{account.name}」是信用卡的扣繳帳戶，請先修改信用卡設定"
        )
    account.is_active = False
    db.commit()
    return account


def activate_account(db: Session, account_id: int) -> Account:
    account = get_account(db, account_id)
    if account.parent_id is not None:
        parent = get_account(db, account.parent_id)
        if not parent.is_active:
            raise RuleViolation("PARENT_INACTIVE", f"請先啟用上層科目「{parent.name}」")
    account.is_active = True
    db.commit()
    return account


def delete_account(db: Session, account_id: int) -> None:
    account = get_account(db, account_id)
    if account.system_key is not None:
        raise RuleViolation("SYSTEM", f"「{account.name}」是系統科目，不能刪除")
    if account_repo.has_postings(db, account.id):
        raise RuleViolation("M01", f"「{account.name}」已經有分錄，不能刪除，只能停用")
    if account_repo.has_children(db, account.id):
        raise RuleViolation("HAS_CHILDREN", f"「{account.name}」底下還有子科目，不能刪除")
    users = [pm.name for pm in account_repo.payment_methods_using(db, account.id)]
    if users:
        raise RuleViolation(
            "PM_ACCOUNT", f"付款方式「{'、'.join(users)}」使用「{account.name}」，不能刪除"
        )
    if account_repo.card_profiles_using_as_autopay(db, account.id):
        raise RuleViolation("CARD_AUTOPAY", f"「{account.name}」是信用卡的扣繳帳戶，不能刪除")
    if account_repo.merchants_using(db, account.id):
        raise RuleViolation("MERCHANT_DEFAULT", f"「{account.name}」是商家的預設科目，不能刪除")

    # 信用卡自己的設定（一對一）跟著刪除
    profile = db.get(CreditCardProfile, account.id)
    if profile is not None:
        db.delete(profile)
        db.flush()
    db.delete(account)
    db.commit()
