"""記帳表單的下拉選單資料（P2 Step 1）。唯讀，不寫入、不做帳務判斷。

- 科目：沿用 P1 的末層定義 accounts.leaf_accounts()（D3：沒有任何子科目），
  這裡只再篩出啟用中的 EXPENSE 科目，並依上層科目分組給 <optgroup> 使用。
- 付款方式：啟用中的付款方式；預設值取 payment_methods.get_default()（D4、P11），
  預設付款方式已停用時不預選。
- 商家（P2 Step 2）：啟用中的商家名稱，給原生 <datalist> 使用。
- 憑證類型（P2 Step 2）：VoucherType 的值與畫面標籤。
- 收入科目、收款帳戶、轉出／轉入帳戶、手續費科目（P2 Step 5A）：一樣只列啟用中的末層科目。
  轉出帳戶不列信用卡：只是不提供一定會被 P6（不能從信用卡轉出）擋下的選項，
  規則本身仍由 P1 的結構檢查負責。
"""

from sqlalchemy.orm import Session

from app.models import Account, PaymentMethod
from app.models.enums import AccountClass, AccountSubtype, VoucherType
from app.services import accounts, merchants, payment_methods

# 憑證類型（規格 F18、6.6）的畫面標籤；值本身由 VoucherType 定義
VOUCHER_LABELS = {
    VoucherType.EINVOICE: "載具電子發票",
    VoucherType.PAPER_INVOICE: "紙本發票",
    VoucherType.RECEIPT: "收據",
    VoucherType.NONE: "無憑證",
}


AccountGroups = list[tuple[str | None, list[Account]]]


def _leaf_groups_by_parent(db: Session, account_class: AccountClass) -> AccountGroups:
    """[(上層科目名稱, [末層科目, ...]), ...]；第一層本身就是末層時，名稱為 None。

    順序沿用 leaf_accounts() 的排序（sort_order、code）。
    """
    groups: dict[int | None, tuple[str | None, list[Account]]] = {}
    for account in accounts.leaf_accounts(db, active_only=True):
        if account.account_class != account_class:
            continue
        parent = account.parent
        key = parent.id if parent is not None else None
        label = parent.name if parent is not None else None
        groups.setdefault(key, (label, []))[1].append(account)
    return list(groups.values())


def expense_account_groups(db: Session) -> AccountGroups:
    """支出科目，也用於轉帳手續費科目。"""
    return _leaf_groups_by_parent(db, AccountClass.EXPENSE)


def income_account_groups(db: Session) -> AccountGroups:
    return _leaf_groups_by_parent(db, AccountClass.INCOME)


def balance_sheet_groups(db: Session, *, exclude_credit_cards: bool = False) -> AccountGroups:
    """收款帳戶、轉出／轉入帳戶：啟用中的資產、負債末層科目，分成「資產」「負債」兩組。"""
    groups: dict[str, list[Account]] = {AccountClass.ASSET: [], AccountClass.LIABILITY: []}
    for account in accounts.leaf_accounts(db, active_only=True):
        if account.account_class not in groups:
            continue
        if exclude_credit_cards and account.subtype == AccountSubtype.CREDIT_CARD:
            continue
        groups[account.account_class].append(account)
    labels = {AccountClass.ASSET: "資產", AccountClass.LIABILITY: "負債"}
    return [(labels[cls], items) for cls, items in groups.items() if items]


def payment_method_choices(db: Session) -> tuple[list[PaymentMethod], int | None]:
    """(啟用中的付款方式, 預設付款方式 id 或 None)。"""
    methods = payment_methods.list_payment_methods(db, active_only=True)
    default = payment_methods.get_default(db)
    default_id = default.id if default is not None and default.is_active else None
    return methods, default_id


def voucher_choices() -> list[tuple[str, str]]:
    """[(值, 畫面標籤), ...]，依 VoucherType 定義順序。"""
    return [(v.value, VOUCHER_LABELS[v]) for v in VoucherType]


def merchant_names(db: Session) -> list[str]:
    """商家欄位 <datalist> 用：啟用中的商家名稱。"""
    return merchants.active_names(db)
