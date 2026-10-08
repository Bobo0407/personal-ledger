"""users 的資料存取。只查詢與加入 Session，不 commit；交易範圍由服務層決定。"""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import User


def get_by_username(db: Session, username: str) -> User | None:
    return db.scalar(select(User).where(User.username == username))


def count(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(User)) or 0
