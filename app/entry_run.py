"""連續補記的「本次」（P2 Step 10，UF02）：只是 UI／session 的暫存，不是帳務資料。

「本次」= 這一輪「儲存並記下一筆」成功建立的交易 id 清單，放在簽章 Cookie `ledger_run`，
資料庫不新增任何東西。Cookie 只存最後活動時間與 transaction id；金額、商家、科目
每次顯示時都重新從資料庫讀（services/txn_run.py），不會有第二份事實來源。

Cookie 值：`<最後活動時間 epoch 秒>.<id>-<id>-…（舊→新）.<簽章>`，只用 [0-9a-f.-]，不需要引號。
簽章是 HMAC-SHA256（金鑰 SECRET_KEY，簽的字串前面加 `entry-run:` 與 CSRF 權杖區隔）。

- 閒置 30 分鐘過期；每次成功的「儲存並記下一筆」延長 30 分鐘。
- 最多 50 筆；超過時丟掉最舊的。
- 簽章不符、格式不對、過期：一律當成「沒有這一輪」，不丟例外。
- HttpOnly、SameSite=Lax、Secure 依 `session_cookie_secure`、Path=/txn。
"""

import hashlib
import hmac
from datetime import datetime

from fastapi import Request, Response

from app import clock
from app.config import Settings

RUN_COOKIE = "ledger_run"
RUN_PATH = "/txn"
IDLE_SECONDS = 30 * 60
MAX_IDS = 50


def _sign(secret_key: str, payload: str) -> str:
    message = f"entry-run:{payload}".encode()
    return hmac.new(secret_key.encode(), message, hashlib.sha256).hexdigest()


def encode(secret_key: str, ids: tuple[int, ...], now: datetime) -> str:
    payload = f"{int(now.timestamp())}.{'-'.join(str(i) for i in ids)}"
    return f"{payload}.{_sign(secret_key, payload)}"


def decode(secret_key: str, value: str | None, now: datetime) -> tuple[int, ...]:
    """有效的 id 清單（舊→新）；任何不合格都回傳空 tuple。"""
    if not value:
        return ()
    try:
        stamp, id_part, signature = value.split(".")
        if not hmac.compare_digest(signature, _sign(secret_key, f"{stamp}.{id_part}")):
            return ()
        if int(now.timestamp()) - int(stamp) > IDLE_SECONDS:
            return ()
        ids = tuple(int(part) for part in id_part.split("-"))
    except ValueError:
        return ()
    if not ids or len(ids) > MAX_IDS or any(i <= 0 for i in ids):
        return ()
    return ids


def include(ids: tuple[int, ...], txn_id: int) -> tuple[int, ...]:
    """加入一筆（已在清單就不重複），超過上限時丟掉最舊的。"""
    if txn_id in ids:
        return ids
    return (*ids, txn_id)[-MAX_IDS:]


def read(request: Request) -> tuple[int, ...]:
    settings: Settings = request.app.state.settings
    return decode(settings.secret_key, request.cookies.get(RUN_COOKIE), clock.now())


def set_cookie(response: Response, ids: tuple[int, ...], settings: Settings) -> None:
    response.set_cookie(
        RUN_COOKIE,
        encode(settings.secret_key, ids, clock.now()),
        max_age=IDLE_SECONDS,
        path=RUN_PATH,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
    )


def clear_cookie(response: Response) -> None:
    response.delete_cookie(RUN_COOKIE, path=RUN_PATH)
