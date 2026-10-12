"""/txn 搜尋條件的解析（app/schemas/search.py）與本月範圍。"""

from datetime import date

import pytest

from app.schemas.search import parse_page, parse_search
from app.schemas.txn import FormParseError
from app.services.search import month_range


def test_parses_all_fields():
    data = parse_search(
        {
            "date_from": "2026-10-01",
            "date_to": "2026-10-31",
            "merchant": " 7-11 ",
            "min_amount": "100",
            "max_amount": "500",
        }
    )
    f = data.to_filters()
    assert (f.date_from, f.date_to) == (date(2026, 10, 1), date(2026, 10, 31))
    assert (f.merchant_name, f.min_amount, f.max_amount) == ("7-11", 100, 500)


def test_blank_means_unbounded():
    f = parse_search(
        dict.fromkeys(("date_from", "date_to", "merchant", "min_amount", "max_amount"), "")
    ).to_filters()
    assert f == type(f)()


def test_equal_bounds_are_fine():
    parse_search(
        {"date_from": "2026-10-01", "date_to": "2026-10-01", "min_amount": "5", "max_amount": "5"}
    )


@pytest.mark.parametrize(
    ("raw", "errors"),
    [
        ({"date_from": "2026/10/01"}, {"date_from": "日期格式不正確"}),
        ({"date_to": "2026-13-01"}, {"date_to": "日期格式不正確"}),
        ({"min_amount": "12.5"}, {"min_amount": "請輸入整數金額"}),
        ({"max_amount": "abc"}, {"max_amount": "請輸入整數金額"}),
        ({"min_amount": "-1"}, {"min_amount": "金額必須是 0 以上的整數"}),
        (
            {"date_from": "2026-10-31", "date_to": "2026-10-01"},
            {"date_to": "結束日期不能早於開始日期"},
        ),
        ({"min_amount": "500", "max_amount": "100"}, {"max_amount": "最高金額不能小於最低金額"}),
    ],
)
def test_invalid_conditions(raw, errors):
    with pytest.raises(FormParseError) as exc:
        parse_search(raw)
    assert exc.value.errors == errors


@pytest.mark.parametrize(
    ("value", "page"), [(None, 1), ("", 1), ("3", 3), ("0", 1), ("-2", 1), ("x", 1)]
)
def test_parse_page(value, page):
    assert parse_page(value) == page


@pytest.mark.parametrize(
    ("today", "expected"),
    [
        (date(2026, 10, 15), (date(2026, 10, 1), date(2026, 10, 31))),
        (date(2026, 12, 31), (date(2026, 12, 1), date(2026, 12, 31))),
        (date(2027, 2, 1), (date(2027, 2, 1), date(2027, 2, 28))),
        (date(2028, 2, 10), (date(2028, 2, 1), date(2028, 2, 29))),
    ],
)
def test_month_range(today, expected):
    assert month_range(today) == expected
