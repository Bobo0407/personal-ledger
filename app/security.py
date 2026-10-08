"""密碼雜湊與 Session 權杖（規格 1.6）。

- 密碼：Argon2id（argon2-cffi 預設參數），資料庫只存雜湊字串。
- Session 權杖：32 bytes 隨機值放在 Cookie；資料庫只存它的 SHA-256。
  權杖本身已有 256 bits 隨機性，不需要再加鹽或用慢雜湊。
"""

import hashlib
import secrets
from functools import lru_cache

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """密碼正確回傳 True；密碼錯誤或雜湊格式壞掉一律回傳 False，不丟例外。"""
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


@lru_cache
def _dummy_hash() -> str:
    return _hasher.hash(secrets.token_urlsafe(16))


def burn_password_check(password: str) -> None:
    """帳號不存在時也做一次同等成本的驗證，讓回應時間不洩漏帳號是否存在。"""
    verify_password(_dummy_hash(), password)


def generate_session_token() -> str:
    return secrets.token_urlsafe(32)


def hash_session_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
