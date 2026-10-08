"""user_sessions 的資料存取。只查詢與加入 Session，不 commit；交易範圍由服務層決定。"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import UserSession


def add(db: Session, user_session: UserSession) -> None:
    db.add(user_session)


def get_by_token_hash(db: Session, token_hash: str) -> UserSession | None:
    return db.scalar(select(UserSession).where(UserSession.token_hash == token_hash))
