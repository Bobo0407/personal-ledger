"""audit_logs 的讀取。只查詢，不 commit，不做顯示格式（可讀摘要在 services/audit_history.py）。"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AuditLog


def list_for(db: Session, entity: str, entity_id: int) -> list[AuditLog]:
    """某個實體的全部稽核紀錄，由舊到新。

    occurred_at 只精確到秒，同一秒內可能有多筆，所以再用 id 排序，保證順序固定。
    """
    stmt = (
        select(AuditLog)
        .where(AuditLog.entity == entity, AuditLog.entity_id == entity_id)
        .order_by(AuditLog.occurred_at, AuditLog.id)
    )
    return list(db.scalars(stmt))
