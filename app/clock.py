"""時間工具：所有寫入資料庫的時間一律是台北時區的 ISO 8601 字串（規格 5.3、第 6 節）。

格式固定為 `YYYY-MM-DDTHH:MM:SS+08:00`（秒為單位、帶時區位移），
因為格式與時區都固定，字串可以直接比較大小與排序。
"""

from datetime import datetime
from zoneinfo import ZoneInfo

APP_TIMEZONE = ZoneInfo("Asia/Taipei")


def now() -> datetime:
    return datetime.now(APP_TIMEZONE).replace(microsecond=0)


def to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise ValueError("不接受沒有時區的 datetime")
    return dt.astimezone(APP_TIMEZONE).replace(microsecond=0).isoformat()


def now_iso() -> str:
    return to_iso(now())
