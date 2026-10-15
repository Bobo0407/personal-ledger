"""backup_runs 資料存取（P3-5A）。不放商業規則、不 commit。"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import BackupRun


def add(db: Session, run: BackupRun) -> BackupRun:
    db.add(run)
    db.flush()
    return run


def latest(db: Session) -> BackupRun | None:
    """最後寫入的一筆（依 id）。首頁備份狀態只看它。"""
    return db.scalar(select(BackupRun).order_by(BackupRun.id.desc()).limit(1))
