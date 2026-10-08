import hashlib
import re

from app import security


def test_hash_password_uses_argon2id():
    assert security.hash_password("pw").startswith("$argon2id$")


def test_hash_password_is_salted():
    a = security.hash_password("same-password")
    b = security.hash_password("same-password")
    assert a != b
    assert "same-password" not in a


def test_verify_password():
    h = security.hash_password("correct horse")
    assert security.verify_password(h, "correct horse") is True
    assert security.verify_password(h, "wrong horse") is False
    assert security.verify_password(h, "") is False


def test_verify_password_with_broken_hash_returns_false():
    assert security.verify_password("not-a-hash", "anything") is False


def test_session_token_is_random_and_urlsafe():
    tokens = {security.generate_session_token() for _ in range(50)}
    assert len(tokens) == 50
    for t in tokens:
        assert re.fullmatch(r"[A-Za-z0-9_-]{43}", t)


def test_hash_session_token_is_sha256():
    token = security.generate_session_token()
    digest = security.hash_session_token(token)
    assert digest == hashlib.sha256(token.encode()).hexdigest()
    assert digest != token
