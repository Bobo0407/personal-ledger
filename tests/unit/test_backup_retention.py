"""備份檔名與保留規則（P3-5A；規格 F29：每日 30 天 + 每月第一份 12 個月）。純函式與檔案清理。"""

from datetime import datetime, timedelta

import pytest

from app import clock
from app.services import backup
from app.services.errors import RuleViolation  # noqa: F401  （確認 services 套件可匯入）

NOW = datetime(2026, 10, 15, 3, 0, 0, tzinfo=clock.APP_TIMEZONE)


def name(stamp: str, seq: int | None = None) -> str:
    return f"ledger-{stamp[:8]}-{stamp[8:]}" + (f"-{seq}" if seq else "") + ".db.gz.age"


def doomed(*stamps: str, now=NOW) -> list[str]:
    return backup.select_deletions([name(s) for s in stamps], now)


# --- 檔名 ------------------------------------------------------------------------------------


def test_parse_file_name_roundtrip():
    moment, seq = backup.parse_file_name("ledger-20261015-030000.db.gz.age")
    assert moment == NOW and seq == 1
    assert backup.parse_file_name("ledger-20261015-030000-3.db.gz.age")[1] == 3


@pytest.mark.parametrize(
    "bad",
    [
        "notes.txt",
        "ledger-2026.db",
        "ledger-20261015-030000.db.gz",
        "ledger-20261015-030000.db.gz.age.part",
        ".ledger-20261015-030000-1.part",
        "ledger-20261301-000000.db.gz.age",
        "ledger-20261015-250000.db.gz.age",
        "xledger-20261015-030000.db.gz.age",
        "ledger-20261015-030000.db.gz.age ",
    ],
)
def test_parse_file_name_rejects_other_names(bad):
    assert backup.parse_file_name(bad) is None


# --- 每日 30 天 ------------------------------------------------------------------------------


def test_daily_backups_within_30_days_are_kept():
    assert doomed("20261014030000", "20261001030000", "20260916030000") == []


def test_exactly_30_days_is_kept_and_one_second_older_is_not():
    # 20260901 是 9 月最早的一份（每月保留），讓另外兩份只能靠 30 天規則
    result = doomed("20260901030000", "20260915030000", "20260915025959")
    assert result == [name("20260915025959")]


def test_old_daily_backups_are_deleted_but_monthly_first_survives():
    result = doomed("20260801030000", "20260802030000", "20260815030000")
    assert result == [name("20260802030000"), name("20260815030000")]


# --- 每月第一份、12 個月 -----------------------------------------------------------------------


def test_each_month_keeps_only_its_earliest_present_file():
    result = doomed("20260301030000", "20260302030000", "20260401030000", "20260402030000")
    assert result == [name("20260302030000"), name("20260402030000")]


def test_earliest_present_is_kept_when_the_first_of_month_is_missing():
    assert doomed("20260305030000", "20260306030000") == [name("20260306030000")]


def test_month_window_is_current_plus_previous_11():
    assert doomed("20251101030000") == []  # 2025-11：差 11 個月，保留
    assert doomed("20251001030000") == [name("20251001030000")]  # 2025-10：差 12，刪除


def test_month_window_across_year_boundary():
    jan = datetime(2027, 1, 20, 3, 0, 0, tzinfo=clock.APP_TIMEZONE)
    assert doomed("20260201030000", now=jan) == []  # 差 11
    assert doomed("20260101030000", now=jan) == [name("20260101030000")]  # 差 12


def test_same_second_suffix_orders_after_base_name():
    names = [name("20260301030000", 2), name("20260301030000"), name("20260301030000", 3)]
    assert backup.select_deletions(names, NOW) == [
        name("20260301030000", 2),
        name("20260301030000", 3),
    ]


def test_non_backup_names_are_never_selected():
    old = name("20200101030000")  # 12 個月之外的備份：會被選到；其他檔名完全忽略
    names = ["notes.txt", "ledger.db", ".ledger-x.part", old]
    assert backup.select_deletions(names, NOW) == [old]


def test_future_dated_file_is_kept():
    future = (NOW + timedelta(days=3)).strftime("%Y%m%d%H%M%S")
    assert doomed(future) == []


# --- cleanup() 實際刪檔 ----------------------------------------------------------------------


def test_cleanup_deletes_only_expired_backup_files(tmp_path):
    keep = [name("20260801030000"), name("20261014030000"), "notes.txt"]
    drop = [name("20260802030000"), name("20260815030000")]
    for file in keep + drop:
        (tmp_path / file).write_bytes(b"x")
    (tmp_path / "sub").mkdir()
    removed = backup.cleanup(tmp_path, NOW)
    assert removed == sorted(drop)
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted([*keep, "sub"])


def test_cleanup_never_deletes_the_protected_file(tmp_path):
    old = name("20260802030000")
    (tmp_path / name("20260801030000")).write_bytes(b"x")
    (tmp_path / old).write_bytes(b"x")
    assert backup.cleanup(tmp_path, NOW, protect=old) == []
    assert (tmp_path / old).exists()


def test_cleanup_tries_every_file_then_reports_failure(tmp_path, monkeypatch):
    from pathlib import Path

    names = [name("20260801030000"), name("20260802030000"), name("20260803030000")]
    for file in names:
        (tmp_path / file).write_bytes(b"x")
    real_unlink = Path.unlink

    def flaky(self, *args, **kwargs):
        if self.name == names[1]:
            raise PermissionError("locked")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", flaky)
    with pytest.raises(backup.BackupStepError):
        backup.cleanup(tmp_path, NOW)
    assert sorted(p.name for p in tmp_path.iterdir()) == names[:2]  # 第 3 份仍被刪除
