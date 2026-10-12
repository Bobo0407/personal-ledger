"""記帳表單的下拉選單資料（P2 Step 1）。唯讀，不寫入、不做帳務判斷。

- 科目：沿用 P1 的末層定義 accounts.leaf_accounts()（D3：沒有任何子科目），
  這裡只再篩出啟用中的 EXPENSE 科目，並依上層科目分組給 <optgroup> 使用。
- 付款方式：啟用中的付款方式；預設值取 payment_methods.get_default()（D4、P11），
  預設付款方式已停用時不預選。
"""

from sqlalchemy.orm import Session

from app.models import Account, PaymentMethod
from app.models.enums import AccountClass
from app.services import accounts, payment_methods


def expense_account_groups(db: Session) -> list[tuple[str | None, list[Account]]]:
    """[(上層科目名稱, [末層科目, ...]), ...]；第一層本身就是末層時，名稱為 None。

    順序沿用 leaf_accounts() 的排序（sort_order、code）。
    """
    groups: dict[int | None, tuple[str | None, list[Account]]] = {}
    for account in accounts.leaf_accounts(db, active_only=True):
        if account.account_class != AccountClass.EXPENSE:
            continue
        parent = account.parent
        key = parent.id if parent is not None else None
        label = parent.name if parent is not None else None
        groups.setdefault(key, (label, []))[1].append(account)
    return list(groups.values())


def payment_method_choices(db: Session) -> tuple[list[PaymentMethod], int | None]:
    """(啟用中的付款方式, 預設付款方式 id 或 None)。"""
    methods = payment_methods.list_payment_methods(db, active_only=True)
    default = payment_methods.get_default(db)
    default_id = default.id if default is not None and default.is_active else None
    return methods, default_id
