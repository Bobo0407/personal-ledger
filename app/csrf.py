"""CSRF 防護（規格 1.6、T29）：簽章式 double-submit。

- Cookie `ledger_csrf` 放一個隨機值（HttpOnly）。
- 表單隱藏欄位 `csrf_token`（或 HTMX 用的 `X-CSRF-Token` 標頭）放這個值的 HMAC-SHA256，
  金鑰是 SECRET_KEY。沒有金鑰的人無法從 Cookie 算出正確的表單權杖。
- 不綁 Session：登入頁還沒有 Session，也必須受 CSRF 保護（避免 login CSRF）。
- 以全域依賴套用在所有路由上，GET／HEAD／OPTIONS 直接放行，其餘方法一律檢查，
  新增路由時不會漏掉。
"""

import hashlib
import hmac
import secrets

from fastapi import HTTPException, Request, Response

from app.config import Settings

CSRF_COOKIE = "ledger_csrf"
CSRF_FIELD = "csrf_token"
CSRF_HEADER = "x-csrf-token"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def sign(secret_key: str, value: str) -> str:
    return hmac.new(secret_key.encode(), value.encode(), hashlib.sha256).hexdigest()


def is_valid(secret_key: str, cookie_value: str | None, submitted: str | None) -> bool:
    if not cookie_value or not submitted:
        return False
    return hmac.compare_digest(submitted, sign(secret_key, cookie_value))


def ensure_cookie_value(request: Request) -> tuple[str, bool]:
    """回傳 (Cookie 值, 是否為新產生)。"""
    existing = request.cookies.get(CSRF_COOKIE)
    if existing:
        return existing, False
    return secrets.token_urlsafe(32), True


def set_cookie(response: Response, value: str, settings: Settings) -> None:
    response.set_cookie(
        CSRF_COOKIE,
        value,
        max_age=settings.session_ttl_days * 24 * 60 * 60,
        path="/",
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
    )


async def csrf_protect(request: Request) -> None:
    """全域依賴：非安全方法缺少或帶錯 CSRF 權杖時回傳 403。"""
    if request.method in SAFE_METHODS:
        return
    settings: Settings = request.app.state.settings
    submitted = request.headers.get(CSRF_HEADER)
    if submitted is None:
        form = await request.form()
        value = form.get(CSRF_FIELD)
        submitted = value if isinstance(value, str) else None
    if not is_valid(settings.secret_key, request.cookies.get(CSRF_COOKIE), submitted):
        raise HTTPException(status_code=403, detail="CSRF 驗證失敗，請重新整理頁面後再試一次。")
