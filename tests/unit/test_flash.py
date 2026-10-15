"""一次性提醒 Cookie（P3-3C，app/flash.py）：簽章、過期、上限、與 ledger_run 互不影響。

只驗證 Cookie 本身；顯示與流程在 tests/integration/test_web_budget_notices.py。
"""

from datetime import datetime, timedelta

import pytest
from starlette.responses import Response

from app import clock, flash
from app.config import Settings
from app.services.issues import Issue

SECRET = "unit-test-secret"
NOW = datetime(2026, 10, 15, 12, 0, tzinfo=clock.APP_TIMEZONE)
V14 = Issue("V14", "餐飲 預算已用 85%（850 / 1,000），接近上限")
V13 = Issue("V13", "現金餘額變成 −120，可能漏記了提款或收入")


def test_round_trip_keeps_code_message_and_order():
    value = flash.encode(SECRET, (V14, V13), NOW)
    assert flash.decode(SECRET, value, NOW) == (V14, V13)


@pytest.mark.parametrize("empty", [None, ""])
def test_missing_cookie_is_empty(empty):
    assert flash.decode(SECRET, empty, NOW) == ()


def test_tampered_message_is_rejected():
    value = flash.encode(SECRET, (V14,), NOW)
    stamp, body, signature = value.split(".")
    forged = f"{stamp}.{body[:-2]}AA.{signature}"
    assert flash.decode(SECRET, forged, NOW) == ()


def test_other_secret_is_rejected():
    assert flash.decode("other", flash.encode(SECRET, (V14,), NOW), NOW) == ()


def test_other_cookie_signature_is_not_accepted():
    # ledger_run 用 "entry-run:" 前綴；同樣的 payload 換成 flash 的簽章要失敗
    import hashlib
    import hmac

    payload = flash.encode(SECRET, (V14,), NOW).rsplit(".", 1)[0]
    bad = hmac.new(SECRET.encode(), f"entry-run:{payload}".encode(), hashlib.sha256).hexdigest()
    assert flash.decode(SECRET, f"{payload}.{bad}", NOW) == ()


def test_expires_after_five_minutes():
    value = flash.encode(SECRET, (V14,), NOW)
    assert flash.decode(SECRET, value, NOW + timedelta(minutes=5)) == (V14,)
    assert flash.decode(SECRET, value, NOW + timedelta(minutes=5, seconds=1)) == ()


def test_future_stamp_is_rejected():
    value = flash.encode(SECRET, (V14,), NOW + timedelta(hours=1))
    assert flash.decode(SECRET, value, NOW) == ()


@pytest.mark.parametrize("garbage", ["x", "a.b", "a.b.c", "1.2.3.4", "....", "é.é.é"])
def test_garbage_is_empty_not_an_exception(garbage):
    assert flash.decode(SECRET, garbage, NOW) == ()


def test_at_most_five_notices():
    many = tuple(Issue("V14", f"第 {i} 則") for i in range(9))
    kept = flash.decode(SECRET, flash.encode(SECRET, many, NOW), NOW)
    assert kept == many[:5]


def test_value_stays_under_cookie_size_limit():
    long = tuple(Issue("V14", "預算" * 200 + str(i)) for i in range(5))
    value = flash.encode(SECRET, long, NOW)
    assert len(value) <= flash.MAX_VALUE_LENGTH
    kept = flash.decode(SECRET, value, NOW)
    assert 1 <= len(kept) <= 5


def test_set_cookie_attributes_and_empty_sets_nothing():
    settings = Settings(secret_key=SECRET, session_cookie_secure=True)
    response = Response()
    flash.set_cookie(response, (V14,), settings)
    header = response.headers["set-cookie"]
    assert header.startswith("ledger_flash=")
    assert "HttpOnly" in header and "Secure" in header and "Path=/;" in header + ";"
    assert "SameSite=lax" in header

    empty = Response()
    flash.set_cookie(empty, (), settings)
    assert "set-cookie" not in empty.headers


def test_clear_cookie_does_not_touch_ledger_run():
    response = Response()
    flash.clear_cookie(response)
    header = response.headers["set-cookie"]
    assert header.startswith("ledger_flash=") and "ledger_run" not in header
