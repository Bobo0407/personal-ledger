"""預算的純函式（P3-3A；規格 F21、V14）：月份、使用率、狀態、跨過門檻。不碰資料庫。"""

from datetime import date

import pytest

from app.services.budgets import (
    BudgetStatus,
    classify,
    crossing,
    month_of,
    month_range,
    usage_pct,
)
from app.services.errors import RuleViolation

# --- 月份 ------------------------------------------------------------------------------


def test_month_of():
    assert month_of(date(2026, 1, 31)) == "2026-01"
    assert month_of(date(2026, 12, 1)) == "2026-12"


@pytest.mark.parametrize(
    ("month", "first", "last"),
    [
        ("2026-10", date(2026, 10, 1), date(2026, 10, 31)),
        ("2026-02", date(2026, 2, 1), date(2026, 2, 28)),
        ("2028-02", date(2028, 2, 1), date(2028, 2, 29)),  # 閏年
        ("2026-12", date(2026, 12, 1), date(2026, 12, 31)),  # 跨年
        ("2026-04", date(2026, 4, 1), date(2026, 4, 30)),
    ],
)
def test_month_range(month, first, last):
    assert month_range(month) == (first, last)


@pytest.mark.parametrize("bad", ["2026-13", "2026-00", "2026-1", "202610", "2026-10-01", "", "x"])
def test_month_range_rejects_bad_format(bad):
    with pytest.raises(RuleViolation) as exc_info:
        month_range(bad)
    assert exc_info.value.code == "BUDGET_MONTH"


def test_month_range_rejects_non_string():
    with pytest.raises(RuleViolation):
        month_range(None)  # type: ignore[arg-type]


# --- 使用率 ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("used", "budget", "pct"),
    [
        (0, 5000, 0),
        (3900, 5000, 78),
        (4100, 5000, 82),  # T15
        (3999, 5000, 79),  # 無條件捨去：不能顯示成 80
        (4000, 5000, 80),
        (5000, 5000, 100),
        (5001, 5000, 100),
        (7500, 5000, 150),
        (1, 3, 33),
        (-300, 5000, 0),  # used 為負：顯示 0，不是負的百分比
    ],
)
def test_usage_pct(used, budget, pct):
    assert usage_pct(used, budget) == pct


# --- 狀態（整數比較，不看四捨五入後的百分比）------------------------------------------------


@pytest.mark.parametrize(
    ("used", "status"),
    [
        (-500, BudgetStatus.OK),
        (0, BudgetStatus.OK),
        (3999, BudgetStatus.OK),  # 79.98%
        (4000, BudgetStatus.WARN),  # 剛好 80%
        (4999, BudgetStatus.WARN),
        (5000, BudgetStatus.OVER),  # 剛好 100%
        (9000, BudgetStatus.OVER),
    ],
)
def test_classify_default_threshold(used, status):
    assert classify(used, 5000, 80) == status


def test_classify_uses_custom_threshold():
    assert classify(3500, 5000, 70) == BudgetStatus.WARN
    assert classify(3499, 5000, 70) == BudgetStatus.OK


def test_classify_threshold_is_exact_for_uneven_budgets():
    # 預算 3：80% = 2.4；used 2 = 66.7% 還沒到，used 3 才 100%
    assert classify(2, 3, 80) == BudgetStatus.OK
    assert classify(3, 3, 80) == BudgetStatus.OVER
    # 預算 7：80% = 5.6；used 5 = 71.4%，used 6 = 85.7%
    assert classify(5, 7, 80) == BudgetStatus.OK
    assert classify(6, 7, 80) == BudgetStatus.WARN


# --- 跨過門檻（V14）---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("before", "after", "expected"),
    [
        (3900, 4100, 80),  # T15：78% → 82%
        (3900, 4000, 80),  # 剛好到 80% 也算跨過
        (3900, 3999, None),  # 79.98% 還沒到
        (4000, 4100, None),  # 本來就在 80% 上
        (4100, 4900, None),  # 已經高於 80%，再增加不重複提醒
        (4900, 5000, 100),  # 剛好到 100%
        (4900, 5100, 100),
        (5000, 5200, None),  # 已經超支，不重複提醒
        (3000, 5200, 100),  # 一次跨過兩個門檻：只回報較高的
        (4100, 3000, None),  # 用量減少：不提醒
        (5200, 3900, None),
        (4000, 4000, None),  # 沒有變動
        (-300, 4000, 80),  # 前一個狀態是負的（退款）也正確
    ],
)
def test_crossing(before, after, expected):
    assert crossing(before, after, 5000, 80) == expected


def test_crossing_uses_alert_pct_threshold():
    assert crossing(3400, 3600, 5000, 70) == 70
    assert crossing(3400, 3600, 5000, 80) is None


def test_crossing_is_exact_for_uneven_budgets():
    # 預算 7：80% 門檻在 5.6；5 → 6 跨過，4 → 5 沒有
    assert crossing(5, 6, 7, 80) == 80
    assert crossing(4, 5, 7, 80) is None
