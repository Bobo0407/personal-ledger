"""首頁備份狀態 backup_status()（P3-5A；規格 8.1：只在失敗或超過 36 小時未備份時顯示警示）。

狀態與文字全部在 service 決定；只看最後一筆 backup_runs；不顯示原始 error。
"""

from datetime import datetime, timedelta

import pytest

from app import clock
from app.services import backup
from tests.backup_helpers import add_run

NOW = datetime(2026, 10, 15, 12, 0, 0, tzinfo=clock.APP_TIMEZONE)
CLEANUP_TEXT = "最後備份成功，但舊備份清理失敗，請檢查 log"


def iso(moment: datetime) -> str:
    return clock.to_iso(moment)


def status(db):
    return backup.backup_status(db, NOW)


def test_no_record_is_a_yellow_warning(ledger_db):
    result = status(ledger_db)
    assert (result.level, result.message) == ("warn", "尚未有備份紀錄")


def test_recent_success_is_ok_one_liner(ledger_db):
    add_run(ledger_db, finished="2026-10-15T03:00:00+08:00")
    result = status(ledger_db)
    assert (result.level, result.message) == ("ok", "最後備份：今天 03:00 ✓")


@pytest.mark.parametrize(
    "finished, label",
    [
        ("2026-10-14T20:00:00+08:00", "昨天 20:00"),
        ("2026-10-14T03:00:00+08:00", "昨天 03:00"),
        ("2026-10-13T22:30:00+08:00", "10-13 22:30"),
    ],
)
def test_date_labels(ledger_db, finished, label):
    add_run(ledger_db, finished=finished)
    assert label in status(ledger_db).message


def test_exactly_36_hours_is_still_ok(ledger_db):
    add_run(ledger_db, finished=iso(NOW - timedelta(hours=36)))
    assert status(ledger_db).level == "ok"


def test_36_hours_and_one_second_is_stale(ledger_db):
    add_run(ledger_db, finished=iso(NOW - timedelta(hours=36, seconds=1)))
    result = status(ledger_db)
    assert result.level == "warn"
    assert result.message.startswith("已超過 36 小時未備份，最後成功：")


def test_35_hours_59_minutes_is_ok(ledger_db):
    add_run(ledger_db, finished=iso(NOW - timedelta(hours=35, minutes=59)))
    assert status(ledger_db).level == "ok"


def test_failed_is_red_and_hides_the_raw_error(ledger_db):
    add_run(
        ledger_db, "FAILED", finished="2026-10-15T03:00:00+08:00", error="OSError: C:\\secret\\p"
    )
    result = status(ledger_db)
    assert result.level == "alert"
    assert result.message == "最近一次備份失敗（今天 03:00），請檢查 log"
    assert "secret" not in result.message


def test_failed_stays_red_even_if_an_older_success_is_recent(ledger_db):
    add_run(ledger_db, finished="2026-10-15T02:00:00+08:00")
    add_run(ledger_db, "FAILED", finished="2026-10-15T03:00:00+08:00")
    assert status(ledger_db).level == "alert"


def test_a_later_success_clears_the_failure(ledger_db):
    add_run(ledger_db, "FAILED", finished="2026-10-15T03:00:00+08:00")
    add_run(ledger_db, finished="2026-10-15T09:00:00+08:00")
    assert status(ledger_db).level == "ok"


def test_cleanup_warning_is_yellow_with_fixed_text_and_no_error_text(ledger_db):
    add_run(ledger_db, finished="2026-10-15T03:00:00+08:00", error=backup.CLEANUP_WARNING)
    result = status(ledger_db)
    assert (result.level, result.message) == ("warn", CLEANUP_TEXT)
    assert backup.CLEANUP_WARNING not in result.message


def test_cleanup_warning_never_leaks_arbitrary_error_text(ledger_db):
    add_run(ledger_db, finished="2026-10-15T03:00:00+08:00", error="C:\\private\\dir denied")
    result = status(ledger_db)
    assert result.message == CLEANUP_TEXT and "private" not in result.message


def test_next_clean_success_returns_to_ok(ledger_db):
    add_run(ledger_db, finished="2026-10-14T03:00:00+08:00", error=backup.CLEANUP_WARNING)
    assert status(ledger_db).level == "warn"
    add_run(ledger_db, finished="2026-10-15T03:00:00+08:00")
    assert status(ledger_db).level == "ok"


def test_stale_takes_precedence_over_cleanup_warning(ledger_db):
    add_run(ledger_db, finished=iso(NOW - timedelta(hours=40)), error=backup.CLEANUP_WARNING)
    assert status(ledger_db).message.startswith("已超過 36 小時未備份")


def test_default_now_uses_the_app_clock(ledger_db, monkeypatch):
    from tests.ledger_helpers import freeze_today

    freeze_today(monkeypatch)  # 2026-10-15 12:00
    add_run(ledger_db, finished="2026-10-15T03:00:00+08:00")
    assert backup.backup_status(ledger_db).level == "ok"
