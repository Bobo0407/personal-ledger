"""accounts 與主資料引用關係的資料存取。只查詢與加入 Session，不 commit；不放商業規則。

注意：has_postings() 刻意檢查「全部」分錄（含已刪除與草稿交易），
因為那些交易隨時可能被還原或確認入帳；這不是帳務加總，所以不走 active_postings()。
"""

from sqlalchemy import exists, func, or_, select
from sqlalchemy.orm import Session

from app.models import Account, CreditCardProfile, Merchant, PaymentMethod, Posting


def get(db: Session, account_id: int) -> Account | None:
    return db.get(Account, account_id)


def get_by_code(db: Session, code: str) -> Account | None:
    return db.scalar(select(Account).where(Account.code == code))


def get_by_system_key(db: Session, system_key: str) -> Account | None:
    return db.scalar(select(Account).where(Account.system_key == system_key))


def list_all(db: Session) -> list[Account]:
    return list(db.scalars(select(Account).order_by(Account.sort_order, Account.code)))


def list_shown_on_home(db: Session) -> list[Account]:
    """show_on_home = 1 的科目（含停用），依 (sort_order, code) 排序；不判斷五大類（P3-1）。"""
    stmt = select(Account).where(Account.show_on_home.is_(True))
    return list(db.scalars(stmt.order_by(Account.sort_order, Account.code)))


def has_children(db: Session, account_id: int) -> bool:
    return bool(db.scalar(select(exists().where(Account.parent_id == account_id))))


def has_active_children(db: Session, account_id: int) -> bool:
    return bool(
        db.scalar(
            select(exists().where(Account.parent_id == account_id, Account.is_active.is_(True)))
        )
    )


def has_postings(db: Session, account_id: int) -> bool:
    return bool(db.scalar(select(exists().where(Posting.account_id == account_id))))


def sibling_name_taken(
    db: Session, parent_id: int | None, name: str, exclude_id: int | None = None
) -> bool:
    stmt = select(func.count()).select_from(Account).where(Account.name == name)
    stmt = stmt.where(
        Account.parent_id.is_(None) if parent_id is None else Account.parent_id == parent_id
    )
    if exclude_id is not None:
        stmt = stmt.where(Account.id != exclude_id)
    return (db.scalar(stmt) or 0) > 0


def leaf_accounts(db: Session, active_only: bool = True) -> list[Account]:
    """沒有任何子科目的科目（決策 D3）。"""
    child = Account.__table__.alias("child")
    stmt = select(Account).where(~exists().where(child.c.parent_id == Account.id))
    if active_only:
        stmt = stmt.where(Account.is_active.is_(True))
    return list(db.scalars(stmt.order_by(Account.sort_order, Account.code)))


def payment_methods_using(
    db: Session, account_id: int, active_only: bool = False
) -> list[PaymentMethod]:
    stmt = select(PaymentMethod).where(PaymentMethod.account_id == account_id)
    if active_only:
        stmt = stmt.where(PaymentMethod.is_active.is_(True))
    return list(db.scalars(stmt.order_by(PaymentMethod.sort_order, PaymentMethod.id)))


def card_profiles_using_as_autopay(db: Session, account_id: int) -> list[CreditCardProfile]:
    return list(
        db.scalars(
            select(CreditCardProfile).where(CreditCardProfile.autopay_account_id == account_id)
        )
    )


def merchants_using(db: Session, account_id: int) -> list[Merchant]:
    return list(db.scalars(select(Merchant).where(Merchant.default_account_id == account_id)))


def is_referenced_by_card_profile(db: Session, account_id: int) -> bool:
    return bool(
        db.scalar(
            select(
                exists().where(
                    or_(
                        CreditCardProfile.account_id == account_id,
                        CreditCardProfile.autopay_account_id == account_id,
                    )
                )
            )
        )
    )
