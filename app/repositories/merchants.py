"""merchants 與 merchant_aliases 的資料存取。只查詢與加入 Session，不 commit；不放商業規則。"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Merchant, MerchantAlias


def get_by_alias_normalized(db: Session, alias_normalized: str) -> Merchant | None:
    alias = db.scalar(
        select(MerchantAlias).where(MerchantAlias.alias_normalized == alias_normalized)
    )
    return alias.merchant if alias is not None else None


def list_all(db: Session, active_only: bool = False) -> list[Merchant]:
    stmt = select(Merchant)
    if active_only:
        stmt = stmt.where(Merchant.is_active.is_(True))
    return list(db.scalars(stmt.order_by(Merchant.name)))


def add(db: Session, merchant: Merchant) -> None:
    db.add(merchant)
