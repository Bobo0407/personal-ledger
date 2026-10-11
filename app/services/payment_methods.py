"""付款方式主資料（規格 6.3、9.2 M08；P1 決策 D4）。

- 付款方式是「管道」，account_id 指向實際扣款的帳戶：必須是啟用中、最末層的資產或負債科目。
- 預設付款方式只存在 payment_methods.is_default（D4），全系統最多一個；
  設定新的預設時自動取消舊的（M08）。
- 目前的預設付款方式不能停用，請先把預設改給其他付款方式。
- 扣款帳戶允許日後修改（Step 2 拍板 S6-a）。
"""

from sqlalchemy.orm import Session

from app.models import Account, PaymentMethod
from app.repositories import accounts as account_repo
from app.repositories import payment_methods as pm_repo
from app.services.accounts import BALANCE_SHEET_CLASSES, clean_text, get_account
from app.services.errors import NotFound, RuleViolation


def check_payment_account(db: Session, account: Account) -> None:
    if account.account_class not in BALANCE_SHEET_CLASSES:
        raise RuleViolation("PM_ACCOUNT", "付款方式的扣款帳戶必須是資產或負債科目")
    if not account.is_active:
        raise RuleViolation("PM_ACCOUNT", f"「{account.name}」已停用，不能作為扣款帳戶")
    if account_repo.has_children(db, account.id):
        raise RuleViolation("PM_ACCOUNT", f"「{account.name}」不是最末層科目，不能作為扣款帳戶")


def get_payment_method(db: Session, payment_method_id: int) -> PaymentMethod:
    pm = pm_repo.get(db, payment_method_id)
    if pm is None:
        raise NotFound("付款方式", payment_method_id)
    return pm


def get_default(db: Session) -> PaymentMethod | None:
    return pm_repo.get_default(db)


def list_payment_methods(db: Session, active_only: bool = True) -> list[PaymentMethod]:
    return pm_repo.list_all(db, active_only=active_only)


def _make_default(db: Session, pm: PaymentMethod) -> None:
    # 先清掉舊的預設再設定新的，避免同時有兩個 is_default = 1 觸發部分唯一索引
    pm_repo.clear_default(db)
    db.flush()
    pm.is_default = True


def create_payment_method(
    db: Session,
    *,
    name: str,
    account_id: int,
    is_default: bool = False,
    sort_order: int = 0,
) -> PaymentMethod:
    name = clean_text(name, "付款方式名稱")
    if pm_repo.get_by_name(db, name) is not None:
        raise RuleViolation("PM_NAME", f"付款方式「{name}」已經存在")
    check_payment_account(db, get_account(db, account_id))
    pm = PaymentMethod(name=name, account_id=account_id, sort_order=sort_order)
    db.add(pm)
    if is_default:
        _make_default(db, pm)
    db.commit()
    return pm


def change_account(db: Session, payment_method_id: int, account_id: int) -> PaymentMethod:
    pm = get_payment_method(db, payment_method_id)
    check_payment_account(db, get_account(db, account_id))
    pm.account_id = account_id
    db.commit()
    return pm


def set_default(db: Session, payment_method_id: int) -> PaymentMethod:
    pm = get_payment_method(db, payment_method_id)
    if not pm.is_active:
        raise RuleViolation("M08", f"「{pm.name}」已停用，不能設為預設付款方式")
    _make_default(db, pm)
    db.commit()
    return pm


def deactivate_payment_method(db: Session, payment_method_id: int) -> PaymentMethod:
    pm = get_payment_method(db, payment_method_id)
    if pm.is_default:
        raise RuleViolation("M08", f"「{pm.name}」是預設付款方式，請先把預設改給其他付款方式")
    pm.is_active = False
    db.commit()
    return pm


def activate_payment_method(db: Session, payment_method_id: int) -> PaymentMethod:
    pm = get_payment_method(db, payment_method_id)
    check_payment_account(db, get_account(db, pm.account_id))
    pm.is_active = True
    db.commit()
    return pm
