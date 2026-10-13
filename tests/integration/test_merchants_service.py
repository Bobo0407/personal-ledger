"""services/merchants.py：比對與建立（P2 Step 2 決策 M2）。"""

import pytest
from sqlalchemy import func, select

from app.models import Merchant, MerchantAlias
from app.services import merchants


def merchant_count(db) -> int:
    return db.scalar(select(func.count()).select_from(Merchant))


def test_resolve_by_canonical_name_after_normalization(seeded_db):
    m = Merchant(name="7-11")
    seeded_db.add(m)
    seeded_db.commit()
    for raw in ("7-11", " 7 - 11 ", "７－１１"):
        assert merchants.resolve(seeded_db, raw) is m


def test_resolve_by_alias(seeded_db):
    m = Merchant(name="7-11")
    m.aliases.append(MerchantAlias(alias="統一超商", alias_normalized="統一超商"))
    seeded_db.add(m)
    seeded_db.commit()
    assert merchants.resolve(seeded_db, " 統一 超商 ") is m


def test_resolve_unknown_or_blank(seeded_db):
    assert merchants.resolve(seeded_db, "全家") is None
    assert merchants.resolve(seeded_db, "  ") is None


def test_resolve_or_create_creates_once_without_alias(seeded_db):
    created = merchants.resolve_or_create(seeded_db, "  Costco  ")
    assert created.id is not None
    assert created.name == "Costco"
    assert merchants.resolve_or_create(seeded_db, "ＣＯＳＴＣＯ") is created
    assert merchant_count(seeded_db) == 1
    assert seeded_db.scalar(select(func.count()).select_from(MerchantAlias)) == 0


def test_resolve_or_create_returns_inactive_merchant_for_p1_to_reject(seeded_db):
    m = Merchant(name="舊店", is_active=False)
    seeded_db.add(m)
    seeded_db.commit()
    assert merchants.resolve_or_create(seeded_db, "舊店") is m
    assert merchant_count(seeded_db) == 1


def test_resolve_or_create_rejects_blank(seeded_db):
    with pytest.raises(ValueError):
        merchants.resolve_or_create(seeded_db, " 　 ")


def test_resolve_or_create_leaves_session_clean(seeded_db):
    merchants.resolve_or_create(seeded_db, "全家")
    assert not (seeded_db.new or seeded_db.dirty or seeded_db.deleted)


def test_suggestions_only_active_merchants(seeded_db):
    seeded_db.add_all([Merchant(name="全家"), Merchant(name="舊店", is_active=False)])
    seeded_db.commit()
    assert [o.value for o in merchants.suggestions(seeded_db)] == ["全家"]
