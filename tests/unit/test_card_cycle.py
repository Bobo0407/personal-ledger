"""信用卡週期的純日期運算（P3-2A；規格 6.2、8.1）。不碰資料庫。"""

from datetime import date, timedelta

import pytest

from app.services.credit_cards import (
    closing_date_in_month,
    compute_cycle,
    due_date_after,
    last_closing_on_or_before,
)

D = date


@pytest.mark.parametrize(
    ("year", "month", "closing_day", "expected"),
    [
        (2026, 10, 15, D(2026, 10, 15)),
        (2027, 2, 31, D(2027, 2, 28)),  # 該月沒有這天 → 月底
        (2028, 2, 31, D(2028, 2, 29)),  # 閏年
        (2028, 2, 30, D(2028, 2, 29)),
        (2026, 4, 31, D(2026, 4, 30)),
        (2026, 12, 31, D(2026, 12, 31)),
        (2026, 1, 1, D(2026, 1, 1)),
    ],
)
def test_closing_date_clamps_to_month_end(year, month, closing_day, expected):
    assert closing_date_in_month(year, month, closing_day) == expected


@pytest.mark.parametrize(
    ("today", "closing_day", "expected"),
    [
        (D(2026, 10, 20), 15, D(2026, 10, 15)),  # 結帳日之後
        (D(2026, 10, 15), 15, D(2026, 10, 15)),  # 今天就是結帳日：算剛結的這一期
        (D(2026, 10, 14), 15, D(2026, 9, 15)),  # 結帳日之前
        (D(2026, 10, 1), 15, D(2026, 9, 15)),
        (D(2027, 1, 5), 15, D(2026, 12, 15)),  # 跨年
        (D(2027, 1, 15), 15, D(2027, 1, 15)),
        (D(2027, 2, 27), 31, D(2027, 1, 31)),  # 31 號：2 月的結帳日是 2/28
        (D(2027, 2, 28), 31, D(2027, 2, 28)),
        (D(2027, 3, 1), 31, D(2027, 2, 28)),
        (D(2027, 3, 30), 31, D(2027, 2, 28)),
        (D(2027, 3, 31), 31, D(2027, 3, 31)),
        (D(2028, 2, 28), 31, D(2028, 1, 31)),  # 閏年 2/28 還沒到 2/29
        (D(2028, 2, 29), 31, D(2028, 2, 29)),
        (D(2027, 1, 1), 31, D(2026, 12, 31)),  # 跨年且 31 號
    ],
)
def test_last_closing_on_or_before(today, closing_day, expected):
    assert last_closing_on_or_before(today, closing_day) == expected


@pytest.mark.parametrize(
    ("closing", "due_day", "expected"),
    [
        (D(2026, 10, 15), 3, D(2026, 11, 3)),  # due_day 比結帳日小 → 下個月
        (D(2026, 10, 5), 20, D(2026, 10, 20)),  # due_day 比結帳日大 → 同月
        (D(2026, 10, 15), 15, D(2026, 11, 15)),  # 等於結帳日：必須嚴格晚於 → 下個月
        (D(2026, 10, 15), 16, D(2026, 10, 16)),
        (D(2027, 1, 31), 15, D(2027, 2, 15)),
        (D(2027, 2, 28), 31, D(2027, 3, 31)),  # 結帳日 2/28（31 截斷）、繳款日 31：2/28 不夠晚
        (D(2027, 2, 28), 30, D(2027, 3, 30)),  # 繳款日 30 在 2 月截斷成 2/28，等於結帳日 → 下個月
        (D(2027, 1, 31), 30, D(2027, 2, 28)),  # 1/30 早於結帳日 → 下個月，2 月截斷
        (D(2026, 12, 15), 3, D(2027, 1, 3)),  # 跨年
        (D(2028, 1, 31), 31, D(2028, 2, 29)),  # 閏年下個月月底
    ],
)
def test_due_date_is_strictly_after_closing(closing, due_day, expected):
    due = due_date_after(closing, due_day)
    assert due == expected
    assert due > closing


def test_compute_cycle_regular():
    cycle = compute_cycle(15, 3, D(2026, 10, 20))
    assert cycle.statement_start == D(2026, 9, 16)
    assert cycle.statement_end == D(2026, 10, 15)
    assert cycle.due_date == D(2026, 11, 3)
    assert cycle.next_closing == D(2026, 11, 15)
    assert cycle.days_until_due == 14


def test_compute_cycle_on_closing_day_includes_today():
    cycle = compute_cycle(15, 3, D(2026, 10, 15))
    assert cycle.statement_end == D(2026, 10, 15)
    assert cycle.statement_start == D(2026, 9, 16)


def test_compute_cycle_due_date_already_passed_is_negative():
    cycle = compute_cycle(15, 3, D(2026, 10, 14))
    assert cycle.statement_end == D(2026, 9, 15)
    assert cycle.due_date == D(2026, 10, 3)
    assert cycle.days_until_due == -11


def test_compute_cycle_days_until_due_is_zero_on_due_date():
    assert compute_cycle(15, 3, D(2026, 11, 3)).days_until_due == 0


def test_compute_cycle_month_end_closing():
    cycle = compute_cycle(31, 15, D(2027, 3, 1))
    assert cycle.statement_start == D(2027, 2, 1)
    assert cycle.statement_end == D(2027, 2, 28)
    assert cycle.due_date == D(2027, 3, 15)
    assert cycle.next_closing == D(2027, 3, 31)


def test_compute_cycle_across_year_boundary():
    cycle = compute_cycle(15, 3, D(2027, 1, 5))
    assert cycle.statement_start == D(2026, 11, 16)
    assert cycle.statement_end == D(2026, 12, 15)
    assert cycle.due_date == D(2027, 1, 3)
    assert cycle.next_closing == D(2027, 1, 15)


@pytest.mark.parametrize("closing_day", [1, 15, 28, 29, 30, 31])
@pytest.mark.parametrize("due_day", [1, 3, 15, 31])
def test_cycles_are_contiguous_and_ordered(closing_day, due_day):
    """掃過三年每一天：今天落在 [statement_end, next_closing) 之間；相鄰週期沒有縫隙也沒有重疊；
    繳款日一定晚於結帳日；週期只會往前走。"""
    previous_end = None
    day = D(2026, 1, 1)
    while day <= D(2028, 12, 31):
        cycle = compute_cycle(closing_day, due_day, day)
        assert cycle.statement_start <= cycle.statement_end <= day < cycle.next_closing
        assert cycle.due_date > cycle.statement_end
        # 以 next_closing 為「今天」得到的下一個週期，起點必須緊接在這個週期的終點之後
        following = compute_cycle(closing_day, due_day, cycle.next_closing)
        assert following.statement_end == cycle.next_closing
        assert following.statement_start == cycle.statement_end + timedelta(days=1)
        if previous_end is not None:
            assert cycle.statement_end >= previous_end
        previous_end = cycle.statement_end
        day += timedelta(days=1)
