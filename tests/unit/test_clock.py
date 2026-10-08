import re
from datetime import UTC, datetime

import pytest

from app import clock

ISO_TAIPEI = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+08:00$")


def test_now_iso_format():
    assert ISO_TAIPEI.match(clock.now_iso())


def test_to_iso_converts_to_taipei():
    utc = datetime(2026, 10, 7, 16, 30, 0, 123456, tzinfo=UTC)
    assert clock.to_iso(utc) == "2026-10-08T00:30:00+08:00"


def test_to_iso_rejects_naive_datetime():
    with pytest.raises(ValueError):
        clock.to_iso(datetime(2026, 10, 7, 12, 0, 0))


def test_iso_strings_sort_chronologically():
    earlier = clock.to_iso(datetime(2026, 1, 9, 23, 0, tzinfo=UTC))
    later = clock.to_iso(datetime(2026, 1, 10, 1, 0, tzinfo=UTC))
    assert earlier < later
