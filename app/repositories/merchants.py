"""merchants 與 merchant_aliases 的資料存取。只查詢與加入 Session，不 commit；不放商業規則。"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Merchant, MerchantAlias


def get(db: Session, merchant_id: int) -> Merchant | None:
    return db.get(Merchant, merchant_id)


def get_alias_by_normalized(db: Session, alias_normalized: str) -> MerchantAlias | None:
    return db.scalar(
        select(MerchantAlias).where(MerchantAlias.alias_normalized == alias_normalized)
    )


def get_by_alias_normalized(db: Session, alias_normalized: str) -> Merchant | None:
    alias = get_alias_by_normalized(db, alias_normalized)
    return alias.merchant if alias is not None else None


def get_alias(db: Session, alias_id: int) -> MerchantAlias | None:
    return db.get(MerchantAlias, alias_id)


def list_aliases(db: Session) -> list[MerchantAlias]:
    """全部別名，依商家、建立順序排列（畫面分組用）。"""
    return list(
        db.scalars(select(MerchantAlias).order_by(MerchantAlias.merchant_id, MerchantAlias.id))
    )


def list_all(db: Session, active_only: bool = False) -> list[Merchant]:
    stmt = select(Merchant)
    if active_only:
        stmt = stmt.where(Merchant.is_active.is_(True))
    return list(db.scalars(stmt.order_by(Merchant.name)))


def add(db: Session, merchant: Merchant) -> None:
    db.add(merchant)


def add_alias(db: Session, alias: MerchantAlias) -> None:
    db.add(alias)


def delete_alias(db: Session, alias: MerchantAlias) -> None:
    db.delete(alias)
