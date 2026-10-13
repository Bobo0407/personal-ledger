"""連續補記「本次」Cookie（P2 Step 10，app/entry_run.py）：簽章、過期、上限、去重。

只驗證 Cookie 本身；顯示與流程在 tests/integration/test_web_txn_run.py。
"""

import re
from datetime import datetime, timedelta

import pytest
from starlette.responses import Response

from app import clock, entry_run
from app.config import Settings

SECRET = "unit-test-secret"
NOW = datetime(2026, 10, 13, 12, 0, tzinfo=clock.APP_TIMEZONE)


def test_round_trip_keeps_order():
    value = entry_run.encode(SECRET, (3, 1, 2), NOW)
    assert entry_run.decode(SECRET, value, NOW) == (3, 1, 2)


def test_value_uses_only_cookie_safe_characters():
    value = entry_run.encode(SECRET, (12, 345), NOW)
    assert re.fullmatch(r"[0-9a-f.\-]+", value)


@pytest.mark.parametrize("empty", [None, ""])
def test_missing_cookie_is_empty(empty):
    assert entry_run.decode(SECRET, empty, NOW) == ()


def test_wrong_secret_is_ignored():
    value = entry_run.encode(SECRET, (1, 2), NOW)
    assert entry_run.decode("another-secret", value, NOW) == ()


def test_tampered_ids_or_signature_are_ignored():
    stamp, ids, signature = entry_run.encode(SECRET, (1, 2), NOW).split(".")
    assert entry_run.decode(SECRET, f"{stamp}.1-2-3.{signature}", NOW) == ()
    assert entry_run.decode(SECRET, f"{stamp}.{ids}.{'0' * len(signature)}", NOW) == ()
    assert entry_run.decode(SECRET, f"{int(stamp) + 60}.{ids}.{signature}", NOW) == ()


@pytest.mark.parametrize(
    "garbage",
    ["x", "1.2", "a.b.c", "..", "1..2", "1.-.2", "1.a.2", "1.2.3.4", "你好", "1.1-2.\x00"],
)
def test_garbage_is_ignored_without_error(garbage):
    assert entry_run.decode(SECRET, garbage, NOW) == ()


def test_signed_but_invalid_ids_are_ignored():
    """簽章正確但內容不合格（理論上只有程式 bug 才會出現）也不能丟例外。"""
    stamp = str(int(NOW.timestamp()))
    for ids in ("0", "-1", "1--2", "1-x", ""):
        payload = f"{stamp}.{ids}"
        value = f"{payload}.{entry_run._sign(SECRET, payload)}"
        assert entry_run.decode(SECRET, value, NOW) == ()


def test_expires_after_thirty_idle_minutes():
    value = entry_run.encode(SECRET, (1,), NOW)
    assert entry_run.IDLE_SECONDS == 30 * 60
    assert entry_run.decode(SECRET, value, NOW + timedelta(minutes=30)) == (1,)
    assert entry_run.decode(SECRET, value, NOW + timedelta(minutes=30, seconds=1)) == ()


def test_more_than_fifty_ids_in_cookie_is_rejected():
    ids = tuple(range(1, entry_run.MAX_IDS + 2))
    assert entry_run.decode(SECRET, entry_run.encode(SECRET, ids, NOW), NOW) == ()


def test_include_appends_new_id():
    assert entry_run.include((), 5) == (5,)
    assert entry_run.include((5,), 7) == (5, 7)


def test_include_does_not_duplicate():
    assert entry_run.include((5, 7), 5) == (5, 7)


def test_include_caps_at_fifty_and_drops_the_oldest():
    ids = tuple(range(1, entry_run.MAX_IDS + 1))
    result = entry_run.include(ids, 999)
    assert len(result) == entry_run.MAX_IDS
    assert result[0] == 2 and result[-1] == 999
    assert entry_run.include(ids, 50) == ids  # 已在清單：原樣


def _header(response: Response) -> str:
    return response.headers["set-cookie"]


def test_set_cookie_attributes():
    settings = Settings(_env_file=None, secret_key=SECRET)
    response = Response()
    entry_run.set_cookie(response, (1, 2), settings)
    header = _header(response)
    assert header.startswith(f"{entry_run.RUN_COOKIE}=")
    assert "Path=/txn" in header
    assert "HttpOnly" in header
    assert "SameSite=lax" in header
    assert f"Max-Age={entry_run.IDLE_SECONDS}" in header
    assert "Secure" not in header


def test_set_cookie_secure_follows_setting():
    settings = Settings(_env_file=None, secret_key=SECRET, session_cookie_secure=True)
    response = Response()
    entry_run.set_cookie(response, (1,), settings)
    assert "Secure" in _header(response)


def test_clear_cookie_expires_it_on_the_same_path():
    response = Response()
    entry_run.clear_cookie(response)
    header = _header(response)
    assert header.startswith(f'{entry_run.RUN_COOKIE}=""') or f"{entry_run.RUN_COOKIE}=;" in header
    assert "Path=/txn" in header
    assert "Max-Age=0" in header
