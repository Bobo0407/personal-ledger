"""商家名稱正規化（規格 6.4：去除空白、全形轉半形、轉小寫；P2 Step 2 不做完整 NFKC）。"""

import pytest

from app.services.merchants import normalize


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("7-11", "7-11"),
        (" 7 - 11 ", "7-11"),
        ("７－１１", "7-11"),  # 全形數字與全形連字號
        ("ＣＯＳＴＣＯ", "costco"),
        ("Starbucks　Coffee", "starbuckscoffee"),  # 全形空白
        ("全家\t便利商店\n", "全家便利商店"),
        ("今井美樹", "今井美樹"),
    ],
)
def test_normalize(raw, expected):
    assert normalize(raw) == expected


@pytest.mark.parametrize("raw", ["①", "㈱", "ｶ", "ﬁ"])
def test_other_compatibility_characters_are_untouched(raw):
    """NFKC 會把這些換成別的字；規格沒有要求，所以原樣保留。"""
    assert normalize(raw) == raw


def test_blank_becomes_empty():
    assert normalize(" 　\t") == ""
