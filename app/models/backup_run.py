"""backup_runs：備份執行紀錄（規格 6、F29；P3-5A）。

- 只在一次備份結束時寫一筆；沒有 RUNNING 狀態（強制中斷不會留下卡住的紀錄，
  「超過 36 小時沒備份」的首頁警示已經涵蓋這個情況）。
- status 只有 SUCCESS／FAILED。SUCCESS 必須有 file_name、size_bytes；FAILED 必須有 error。
- SUCCESS 的 error 通常是 NULL；備份檔成功、但清理舊備份失敗時，存一則簡短警告（首頁顯示黃色）。
- file_name 只存檔名，不存目錄路徑。P3-5B 起 SUCCESS 代表「本機加密檔＋雲端上傳驗證都完成」；
  雲端失敗是 FAILED，file_name／size_bytes 保留（表示本機檔已完成）。
  P3-5B 之前的 SUCCESS 視為「僅本機」。
  語意細節見 docs/decisions/p3.md 與 ADR-009；不需要新欄位。
- 與帳務資料沒有任何關聯（沒有外鍵）。
"""

from sqlalchemy import CheckConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin

STATUS_CHECK = "status IN ('SUCCESS', 'FAILED')"
SHAPE_CHECK = (
    "(status = 'SUCCESS' AND file_name IS NOT NULL AND size_bytes IS NOT NULL"
    " AND size_bytes >= 0) OR (status = 'FAILED' AND error IS NOT NULL)"
)


class BackupRun(TimestampMixin, Base):
    __tablename__ = "backup_runs"
    __table_args__ = (
        CheckConstraint(STATUS_CHECK, name="status_valid"),
        CheckConstraint(SHAPE_CHECK, name="status_shape"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[str]
    finished_at: Mapped[str]
    status: Mapped[str]
    file_name: Mapped[str | None] = mapped_column(default=None)
    size_bytes: Mapped[int | None] = mapped_column(default=None)
    error: Mapped[str | None] = mapped_column(default=None)

    def __repr__(self) -> str:
        return f"<BackupRun {self.id} {self.status} {self.finished_at}>"
