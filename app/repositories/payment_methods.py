"""payment_methods 的資料存取。只查詢與加入 Session，不 commit；不放商業規則。"""

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models import PaymentMethod


def get(db: Session, payment_method_id: int) -> PaymentMethod | None:
    return db.get(PaymentMethod, payment_method_id)


def get_by_name(db: Session, name: str) -> PaymentMethod | None:
    return db.scalar(select(PaymentMethod).where(PaymentMethod.name == name))


def get_default(db: Session) -> PaymentMethod | None:
    return db.scalar(select(PaymentMethod).where(PaymentMethod.is_default.is_(True)))


def list_all(db: Session, active_only: bool = False) -> list[PaymentMethod]:
    stmt = select(PaymentMethod)
    if active_only:
        stmt = stmt.where(PaymentMethod.is_active.is_(True))
    return list(db.scalars(stmt.order_by(PaymentMethod.sort_order, PaymentMethod.id)))


def clear_default(db: Session) -> None:
    db.execute(
        update(PaymentMethod)
        .where(PaymentMethod.is_default.is_(True))
        .values(is_default=False)
        .execution_options(synchronize_session="fetch")
    )
