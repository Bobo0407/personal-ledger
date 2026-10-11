"""信用卡設定（規格 9.2 M07；決策 S1、S1-a、S1-b）。"""

import pytest

from app.repositories import accounts as account_repo
from app.services import accounts as account_service
from app.services import credit_cards
from app.services.errors import RuleViolation


def code_id(db, code: str) -> int:
    return account_repo.get_by_code(db, code).id


@pytest.fixture
def card_id(seeded_db):
    """尚未設定信用卡週期的新信用卡帳戶。"""
    card = account_service.create_account(
        seeded_db, code="2102", name="新信用卡", account_class="LIABILITY", subtype="CREDIT_CARD"
    )
    return card.id


def assert_rule(rule: str, fn, /, *args, **kwargs) -> None:
    with pytest.raises(RuleViolation) as exc_info:
        fn(*args, **kwargs)
    assert exc_info.value.code == rule, exc_info.value.message


def test_card_without_profile_is_unset(seeded_db, card_id):
    assert credit_cards.get_profile(seeded_db, card_id) is None


def test_create_profile(seeded_db, card_id):
    profile = credit_cards.set_profile(
        seeded_db,
        card_id,
        closing_day=20,
        due_day=8,
        autopay_account_id=code_id(seeded_db, "1103"),
        credit_limit=50_000,
    )
    assert (profile.closing_day, profile.due_day, profile.credit_limit) == (20, 8, 50_000)
    assert credit_cards.get_profile(seeded_db, card_id) is profile


def test_profile_without_autopay_and_limit(seeded_db, card_id):
    profile = credit_cards.set_profile(seeded_db, card_id, closing_day=31, due_day=1)
    assert profile.autopay_account_id is None
    assert profile.credit_limit is None


def test_update_profile_and_clear_autopay(seeded_db):
    card = code_id(seeded_db, "2101")
    profile = credit_cards.set_profile(seeded_db, card, closing_day=25, due_day=10)
    assert (profile.closing_day, profile.due_day) == (25, 10)
    assert profile.autopay_account_id is None


@pytest.mark.parametrize(
    ("closing_day", "due_day"), [(None, 3), (15, None), (0, 3), (15, 32), ("15", 3), (True, 3)]
)
def test_m07_days_required_and_in_range(seeded_db, card_id, closing_day, due_day):
    assert_rule(
        "M07",
        credit_cards.set_profile,
        seeded_db,
        card_id,
        closing_day=closing_day,
        due_day=due_day,
    )
    assert credit_cards.get_profile(seeded_db, card_id) is None


@pytest.mark.parametrize("autopay_code", ["1101", "1201", "2101", "5101"])
def test_m07_autopay_must_be_bank(seeded_db, card_id, autopay_code):
    assert_rule(
        "M07",
        credit_cards.set_profile,
        seeded_db,
        card_id,
        closing_day=15,
        due_day=3,
        autopay_account_id=code_id(seeded_db, autopay_code),
    )


def test_m07_autopay_must_be_active(seeded_db, card_id):
    post_office = account_service.create_account(
        seeded_db, code="1104", name="其他銀行", account_class="ASSET", subtype="BANK"
    )
    account_service.deactivate_account(seeded_db, post_office.id)
    assert_rule(
        "M07",
        credit_cards.set_profile,
        seeded_db,
        card_id,
        closing_day=15,
        due_day=3,
        autopay_account_id=post_office.id,
    )


def test_profile_only_for_credit_card_accounts(seeded_db):
    assert_rule(
        "NOT_CREDIT_CARD",
        credit_cards.set_profile,
        seeded_db,
        code_id(seeded_db, "1102"),
        closing_day=15,
        due_day=3,
    )


def test_profile_rejects_non_positive_credit_limit(seeded_db, card_id):
    assert_rule(
        "CREDIT_LIMIT",
        credit_cards.set_profile,
        seeded_db,
        card_id,
        closing_day=15,
        due_day=3,
        credit_limit=0,
    )


def test_profile_cannot_be_set_on_inactive_card(seeded_db, card_id):
    account_service.deactivate_account(seeded_db, card_id)
    assert_rule("INACTIVE", credit_cards.set_profile, seeded_db, card_id, closing_day=15, due_day=3)
