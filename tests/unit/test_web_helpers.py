import pytest

from app import csrf
from app.deps import login_url, safe_next


@pytest.mark.parametrize("value", ["/", "/txn", "/txn?page=2", "/reports/monthly"])
def test_safe_next_keeps_local_paths(value):
    assert safe_next(value) == value


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "https://evil.example",
        "//evil.example",
        "/\\evil.example",
        "javascript:alert(1)",
        "txn",
        "/\r\nSet-Cookie:x=1",
    ],
)
def test_safe_next_rejects_external_or_malformed(value):
    assert safe_next(value) == "/"


def test_login_url_encodes_next():
    assert login_url("/txn?page=2") == "/login?next=%2Ftxn%3Fpage%3D2"
    assert login_url("https://evil.example") == "/login?next=%2F"


def test_csrf_token_depends_on_secret_and_cookie():
    token = csrf.sign("secret-a", "cookie-1")
    assert csrf.is_valid("secret-a", "cookie-1", token)
    assert not csrf.is_valid("secret-b", "cookie-1", token)
    assert not csrf.is_valid("secret-a", "cookie-2", token)
    assert not csrf.is_valid("secret-a", None, token)
    assert not csrf.is_valid("secret-a", "cookie-1", None)
    assert not csrf.is_valid("secret-a", "cookie-1", "cookie-1")  # 直接回送 Cookie 值不算數
