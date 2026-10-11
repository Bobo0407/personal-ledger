"""稽核紀錄（規格 6.10、7.1 audit.py；決策 D6）。

record() 只把 AuditLog 加進 Session，不 commit、不 flush：
由 LedgerService 在同一個資料庫交易內和交易、分錄一起提交或一起回滾。
稽核紀錄是資料異動的正式紀錄，可以包含金額；應用程式日誌則不記金額（規格 9.4）。
"""

import json
from typing import Any

from sqlalchemy.orm import Session

from app.models import AuditLog


def to_json(data: dict[str, Any] | None) -> str | None:
    if data is None:
        return None
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


def record(
    db: Session,
    *,
    entity: str,
    entity_id: int,
    action: str,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    user_id: int | None = None,
) -> AuditLog:
    log = AuditLog(
        entity=entity,
        entity_id=entity_id,
        action=action,
        before_json=to_json(before),
        after_json=to_json(after),
        user_id=user_id,
    )
    db.add(log)
    return log
