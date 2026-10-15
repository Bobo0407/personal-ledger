"""一次性提醒（P3-3C）：把 PostResult.notices 帶過一次 redirect，顯示一次就消失。

用在「儲存並回首頁」（HX-Redirect 到 /）與「刪除」（HX-Redirect 到 /txn/{id}）：這兩條路
成功後會離開當前頁面，V13／V14 的提醒不能因此不見。沒有新的 server 端狀態，也不是通用通知框架。

Cookie `ledger_flash` 值：`<epoch 秒>.<base64url(JSON [[code, message], ...])>.<簽章>`。
簽章是 HMAC-SHA256（金鑰 SECRET_KEY，簽的字串前面加 `flash:` 與 CSRF、entry_run 區隔）。

- 讀取的頁面（GET /、GET /txn/{id}）讀完立刻刪除 cookie，所以重新整理不會再出現。
- 最多 5 則、編碼後不超過 3,000 字元；超過時丟掉後面的。5 分鐘過期。
- 簽章不符、格式不對、過期：當成沒有，不丟例外。
- 與 `ledger_run`（Path=/txn，連續補記）是不同的 cookie：不讀、不寫、不清除它。
- HttpOnly、SameSite=Lax、Secure 依 `session_cookie_secure`、Path=/。
"""

import base64
import hashlib
import hmac
import json
from datetime import datetime

from fastapi import Request, Response

from app import clock
from app.config import Settings
from app.services.issues import Issue

FLASH_COOKIE = "ledger_flash"
FLASH_PATH = "/"
EXPIRE_SECONDS = 5 * 60
MAX_NOTICES = 5
MAX_VALUE_LENGTH = 3000


def _sign(secret_key: str, payload: str) -> str:
    return hmac.new(secret_key.encode(), f"flash:{payload}".encode(), hashlib.sha256).hexdigest()


def _payload(stamp: int, notices: list[Issue]) -> str:
    body = json.dumps([[n.code, n.message] for n in notices], ensure_ascii=False)
    return f"{stamp}.{base64.urlsafe_b64encode(body.encode()).decode().rstrip('=')}"


def encode(secret_key: str, notices: tuple[Issue, ...], now: datetime) -> str:
    kept = list(notices[:MAX_NOTICES])
    while True:
        payload = _payload(int(now.timestamp()), kept)
        value = f"{payload}.{_sign(secret_key, payload)}"
        if len(value) <= MAX_VALUE_LENGTH or len(kept) <= 1:
            return value
        kept.pop()


def decode(secret_key: str, value: str | None, now: datetime) -> tuple[Issue, ...]:
    """有效的提醒；任何不合格都回傳空 tuple。"""
    if not value:
        return ()
    try:
        stamp, body, signature = value.split(".")
        if not hmac.compare_digest(signature, _sign(secret_key, f"{stamp}.{body}")):
            return ()
        if not 0 <= int(now.timestamp()) - int(stamp) <= EXPIRE_SECONDS:
            return ()
        raw = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).decode())
        notices = tuple(Issue(str(code), str(message)) for code, message in raw)
    except (ValueError, TypeError):
        return ()
    return notices[:MAX_NOTICES]


def read(request: Request) -> tuple[Issue, ...]:
    settings: Settings = request.app.state.settings
    return decode(settings.secret_key, request.cookies.get(FLASH_COOKIE), clock.now())


def set_cookie(response: Response, notices: tuple[Issue, ...], settings: Settings) -> None:
    if not notices:
        return
    response.set_cookie(
        FLASH_COOKIE,
        encode(settings.secret_key, notices, clock.now()),
        max_age=EXPIRE_SECONDS,
        path=FLASH_PATH,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
    )


def clear_cookie(response: Response) -> None:
    response.delete_cookie(FLASH_COOKIE, path=FLASH_PATH)


def pop(request: Request, response: Response) -> None:
    """有帶 cookie 才在這次回應刪掉它（讀取與清除成對使用）。"""
    if FLASH_COOKIE in request.cookies:
        clear_cookie(response)
