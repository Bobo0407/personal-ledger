"""信用卡設定頁的 service（P3-2B；規格 F07、M07）。

重點：update_card_settings() 保留既有 credit_limit（設定頁沒有額度欄位），
驗證仍由 set_profile() 負責，失敗時資料不變。
"""

import pytest

from app.models import Account
from app.services import accounts as account_service
from app.services import credit_cards
from app.services.errors import RuleViolation
from tests.ledger_helpers import acc


@pytest.fixture
def card(seeded_db) -> int:
    return acc(seeded_db, "2101")


def new_card(db, code="2102", name="另一張卡", **flags) -> Account:
    return account_service.create_account(
        db, code=code, name=name, account_class="LIABILITY", subtype="CREDIT_CARD", **flags
    )


def deactivate(db, account_id: int) -> None:
    db.get(Account, account_id).is_active = False
    db.commit()


def snapshot(db, card_id):
    p = credit_cards.get_profile(db, card_id)
    return (p.closing_day, p.due_day, p.autopay_account_id, p.credit_limit)


# --- update_card_settings ------------------------------------------------------------------


def test_creates_profile_for_unset_card(seeded_db):
    other = new_card(seeded_db)
    assert credit_cards.get_profile(seeded_db, other.id) is None
    credit_cards.update_card_settings(
        seeded_db, other.id, closing_day=20, due_day=5, autopay_account_id=acc(seeded_db, "1102")
    )
    assert snapshot(seeded_db, other.id) == (20, 5, acc(seeded_db, "1102"), None)


def test_updates_existing_profile(seeded_db, card):
    credit_cards.update_card_settings(seeded_db, card, closing_day=25, due_day=10)
    assert snapshot(seeded_db, card) == (25, 10, None, None)


def test_keeps_existing_credit_limit(seeded_db, card):
    """設定頁沒有額度欄位：儲存時不能把既有額度洗成 NULL（核准決策 3）。"""
    credit_cards.set_profile(
        seeded_db, card, closing_day=15, due_day=3, autopay_account_id=None, credit_limit=80000
    )
    credit_cards.update_card_settings(
        seeded_db, card, closing_day=20, due_day=8, autopay_account_id=acc(seeded_db, "1102")
    )
    assert snapshot(seeded_db, card) == (20, 8, acc(seeded_db, "1102"), 80000)


def test_autopay_can_be_cleared(seeded_db, card):
    assert credit_cards.get_profile(seeded_db, card).autopay_account_id == acc(seeded_db, "1103")
    credit_cards.update_card_settings(seeded_db, card, closing_day=15, due_day=3)
    assert credit_cards.get_profile(seeded_db, card).autopay_account_id is None


@pytest.mark.parametrize(
    ("closing", "due"),
    [(None, 3), (15, None), (0, 3), (15, 32), (-1, 3), (15, 0)],
)
def test_invalid_days_rejected_and_data_unchanged(seeded_db, card, closing, due):
    before = snapshot(seeded_db, card)
    with pytest.raises(RuleViolation) as exc_info:
        credit_cards.update_card_settings(seeded_db, card, closing_day=closing, due_day=due)
    assert exc_info.value.code == "M07"
    assert snapshot(seeded_db, card) == before


@pytest.mark.parametrize("code", ["1101", "2101", "5201"])
def test_ineligible_autopay_rejected(seeded_db, card, code):
    """現金、信用卡、費用科目都不能當扣繳帳戶。"""
    before = snapshot(seeded_db, card)
    with pytest.raises(RuleViolation) as exc_info:
        credit_cards.update_card_settings(
            seeded_db, card, closing_day=15, due_day=3, autopay_account_id=acc(seeded_db, code)
        )
    assert exc_info.value.code == "M07"
    assert snapshot(seeded_db, card) == before


def test_inactive_autopay_rejected(seeded_db, card):
    deactivate(seeded_db, acc(seeded_db, "1102"))
    with pytest.raises(RuleViolation) as exc_info:
        credit_cards.update_card_settings(
            seeded_db, card, closing_day=15, due_day=3, autopay_account_id=acc(seeded_db, "1102")
        )
    assert exc_info.value.code == "M07"


def test_unknown_autopay_id_is_rule_violation(seeded_db, card):
    with pytest.raises(RuleViolation) as exc_info:
        credit_cards.update_card_settings(
            seeded_db, card, closing_day=15, due_day=3, autopay_account_id=999999
        )
    assert exc_info.value.code == "M07"


def test_non_card_and_inactive_card_rejected(seeded_db, card):
    with pytest.raises(RuleViolation) as exc_info:
        credit_cards.update_card_settings(
            seeded_db, acc(seeded_db, "1102"), closing_day=15, due_day=3
        )
    assert exc_info.value.code == "NOT_CREDIT_CARD"
    deactivate(seeded_db, card)
    with pytest.raises(RuleViolation) as exc_info:
        credit_cards.update_card_settings(seeded_db, card, closing_day=20, due_day=3)
    assert exc_info.value.code == "INACTIVE"


# --- 設定頁清單 ----------------------------------------------------------------------------


def test_list_card_settings_active_cards_with_profile(seeded_db, card):
    other = new_card(seeded_db)
    gone = new_card(seeded_db, code="2103", name="停用卡")
    deactivate(seeded_db, gone.id)
    rows = credit_cards.list_card_settings(seeded_db)
    assert {r.card.id: r.profile is not None for r in rows} == {card: True, other.id: False}


def test_list_card_settings_includes_cards_not_shown_on_home(seeded_db):
    other = new_card(seeded_db, show_on_home=False)
    assert other.id in [r.card.id for r in credit_cards.list_card_settings(seeded_db)]


def test_autopay_candidates_are_active_leaf_bank_assets(seeded_db):
    names = [a.name for a in credit_cards.autopay_candidates(seeded_db)]
    assert names == ["郵局", "聯邦活存"]  # 沒有現金、信用卡、費用科目
    deactivate(seeded_db, acc(seeded_db, "1102"))
    assert [a.name for a in credit_cards.autopay_candidates(seeded_db)] == ["聯邦活存"]
