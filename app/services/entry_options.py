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
- 快速記帳的主科目排序（P2 Step 11，UF01）：有使用紀錄的啟用中末層支出科目放進最上方的「常用」群組，
  依使用次數由多到少，同次數照原本的 sort_order、code；從未使用過的留在各自的上層分組，順序不變。
  使用次數的定義見 repositories/ledger.expense_main_usage_counts()。
- 修改表單（P2 Step 4A／5B）的「目前值」：交易正在使用的科目或付款方式已停用時，P1 允許沒有變更的
  分錄繼續使用（D14），所以 keep_id 指定的那一個停用值也列出（畫面標示「已停用」）；
  其他停用值一律不列，不能切換到另一個停用值。
"""

from sqlalchemy.orm import Session

from app.models import Account, PaymentMethod
from app.models.enums import AccountClass, AccountSubtype, VoucherType
from app.repositories import ledger as ledger_repo
from app.services import accounts, merchants, payment_methods

# 憑證類型（規格 F18、6.6）的畫面標籤；值本身由 VoucherType 定義
VOUCHER_LABELS = {
    VoucherType.EINVOICE: "載具電子發票",
    VoucherType.PAPER_INVOICE: "紙本發票",
    VoucherType.RECEIPT: "收據",
    VoucherType.NONE: "無憑證",
}


AccountGroups = list[tuple[str | None, list[Account]]]


def _leaves(db: Session, keep_id: int | None) -> list[Account]:
    """啟用中的末層科目，加上 keep_id 指定的那一個停用末層科目（修改時的目前值）。"""
    return [
        a
        for a in accounts.leaf_accounts(db, active_only=False)
        if a.is_active or (keep_id is not None and a.id == keep_id)
    ]


def _leaf_groups_by_parent(
    db: Session, account_class: AccountClass, keep_id: int | None = None
) -> AccountGroups:
    """[(上層科目名稱, [末層科目, ...]), ...]；第一層本身就是末層時，名稱為 None。

    順序沿用 leaf_accounts() 的排序（sort_order、code）。
    """
    groups: dict[int | None, tuple[str | None, list[Account]]] = {}
    for account in _leaves(db, keep_id):
        if account.account_class != account_class:
            continue
        parent = account.parent
        key = parent.id if parent is not None else None
        label = parent.name if parent is not None else None
        groups.setdefault(key, (label, []))[1].append(account)
    return list(groups.values())


def expense_account_groups(db: Session, keep_id: int | None = None) -> AccountGroups:
    """支出科目，也用於轉帳手續費科目。"""
    return _leaf_groups_by_parent(db, AccountClass.EXPENSE, keep_id)


FREQUENT_LABEL = "常用"


def ranked_expense_account_groups(db: Session) -> AccountGroups:
    """快速記帳建立表單的主科目選單（P2 Step 11，UF01）。

    有使用紀錄的科目移進最上方的「常用」群組（不重複出現在原本的上層分組）；
    沒有任何使用紀錄時，結果和 expense_account_groups() 完全相同。
    候選科目與 expense_account_groups() 一樣：啟用中、末層、EXPENSE。
    """
    groups = expense_account_groups(db)
    ordered = [a for a in _leaves(db, None) if a.account_class == AccountClass.EXPENSE]
    counts = ledger_repo.expense_main_usage_counts(db, [a.id for a in ordered])
    # sorted() 是穩定排序：同次數保留 leaf_accounts() 的 (sort_order, code) 順序
    used = sorted((a for a in ordered if a.id in counts), key=lambda a: -counts[a.id])
    if not used:
        return groups
    used_ids = {a.id for a in used}
    rest = [(label, [a for a in items if a.id not in used_ids]) for label, items in groups]
    return [(FREQUENT_LABEL, used), *[(label, items) for label, items in rest if items]]


def income_account_groups(db: Session, keep_id: int | None = None) -> AccountGroups:
    return _leaf_groups_by_parent(db, AccountClass.INCOME, keep_id)


def balance_sheet_groups(
    db: Session, *, exclude_credit_cards: bool = False, keep_id: int | None = None
) -> AccountGroups:
    """收款帳戶、轉出／轉入帳戶：啟用中的資產、負債末層科目，分成「資產」「負債」兩組。"""
    groups: dict[str, list[Account]] = {AccountClass.ASSET: [], AccountClass.LIABILITY: []}
    for account in _leaves(db, keep_id):
        if account.account_class not in groups:
            continue
        if exclude_credit_cards and account.subtype == AccountSubtype.CREDIT_CARD:
            continue
        groups[account.account_class].append(account)
    labels = {AccountClass.ASSET: "資產", AccountClass.LIABILITY: "負債"}
    return [(labels[cls], items) for cls, items in groups.items() if items]


def default_cash_account_id(db: Session) -> int | None:
    """提款入口（V1.1）預選的轉入帳戶：啟用中、ASSET、subtype=CASH 的末層科目剛好一個時才回傳。

    零個或多個都回 None（不猜），由使用者自己選；只是預設值，規則仍由 P1 檢查。
    """
    candidates = [
        a.id
        for a in accounts.leaf_accounts(db, active_only=True)
        if a.is_active
        and a.account_class == AccountClass.ASSET
        and a.subtype == AccountSubtype.CASH
    ]
    return candidates[0] if len(candidates) == 1 else None


def credit_card_groups(db: Session, keep_id: int | None = None) -> AccountGroups:
    """修改繳卡費（P2 Step 5B）的信用卡選單：只列信用卡。只是選項過濾，規則仍由 P1 檢查。"""
    cards = [a for a in _leaves(db, keep_id) if a.subtype == AccountSubtype.CREDIT_CARD]
    return [("信用卡", cards)] if cards else []


def payment_method_choices(
    db: Session, keep_id: int | None = None
) -> tuple[list[PaymentMethod], int | None]:
    """(啟用中的付款方式（加上 keep_id 指定的停用付款方式）, 預設付款方式 id 或 None)。"""
    methods = [
        pm
        for pm in payment_methods.list_payment_methods(db, active_only=False)
        if pm.is_active or (keep_id is not None and pm.id == keep_id)
    ]
    default = payment_methods.get_default(db)
    default_id = default.id if default is not None and default.is_active else None
    return methods, default_id


def voucher_choices() -> list[tuple[str, str]]:
    """[(值, 畫面標籤), ...]，依 VoucherType 定義順序。"""
    return [(v.value, VOUCHER_LABELS[v]) for v in VoucherType]


def merchant_options(db: Session) -> list[merchants.MerchantOption]:
    """商家欄位 <datalist> 用：啟用中商家的標準名稱與別名（P2 Step 9）。"""
    return merchants.suggestions(db)
