"""標籤名稱正規化與解析（P2 Step 8B）：tags 專用，不共用商家的規則。"""

import pytest

from app.services.tags import key, normalize, parse


@pytest.mark.parametrize(
    "raw", ["Tokyo Trip", "tokyo trip", "Ｔｏｋｙｏ　Ｔｒｉｐ", "Tokyo  Trip", "  Tokyo\tTrip  "]
)
def test_same_tag(raw):
    assert key(raw) == "tokyo trip"


def test_inner_space_is_meaningful():
    assert key("TokyoTrip") != key("Tokyo Trip")
    assert key("東京旅行") != key("東京 旅行")


def test_canonical_display_keeps_case():
    assert normalize("  Ｔｏｋｙｏ　　Trip ") == "Tokyo Trip"
    assert normalize("演唱會２０２６") == "演唱會2026"


def test_no_nfkc():
    assert normalize("①旅行") == "①旅行"
    assert normalize("㈱") == "㈱"


def test_parse_separators_dedupe_and_order():
    assert parse("旅行、聚餐, 東京，旅行 ,  ,tokyo trip、Tokyo  Trip") == [
        "旅行",
        "聚餐",
        "東京",
        "tokyo trip",
    ]
    assert parse("") == [] and parse(None) == [] and parse(" 、 ,") == []
