"""P3-3B 畫面用的 service：budget_cards()、settings_rows()、later_versions()、BudgetCard.state。"""

from datetime import date

import pytest
from sqlalchemy import select

from app.models import Account, Setting
from app.repositories import budgets as budget_repo
from app.services import budgets
from app.services.errors import InvalidSetting
from tests.integration.test_budgets_service import (
    NOV,
    OCT,
    _today,  # noqa: F401
    spend,
)
from tests.ledger_helpers import TODAY, acc


@pytest.fixture
def seeded(seeded_db):
    return seeded_db


def card_state(db, code, amount, spent):
    budgets.set_budget(db, acc(db, code), OCT, amount)
    if spent:
        spend(db, TODAY, spent, "5101")
    (card,) = budgets.budget_cards(db, OCT)
    return card


@pytest.mark.parametrize(
    ("spent", "state", "over"),
    [
        (0, "ok", 0),
        (799, "ok", 0),
        (800, "warn", 0),
        (999, "warn", 0),
        (1000, "used_up", 0),
        (1001, "over", 1),
        (1500, "over", 500),
    ],
)
def test_card_state(seeded, spent, state, over):
    card = card_state(seeded, "5100", 1000, spent)
    assert (card.state, card.over_amount) == (state, over)


def test_cards_empty_without_budgets(seeded):
    assert budgets.budget_cards(seeded, OCT) == ()


def test_cards_invalid_alert_pct_raises(seeded):
    budgets.set_budget(seeded, acc(seeded, "5100"), OCT, 1000)
    seeded.get(Setting, "budget_alert_pct").value = "0"
    with pytest.raises(InvalidSetting):
        budgets.budget_cards(seeded, OCT)


def test_settings_rows_lists_only_first_level_expense(seeded):
    rows = budgets.settings_rows(seeded, OCT).rows
    ids = {r.account_id for r in rows}
    expected = set(
        seeded.scalars(
            select(Account.id).where(
                Account.account_class == "EXPENSE",
                Account.parent_id.is_(None),
                Account.is_active.is_(True),
            )
        )
    )
    assert ids == expected and ids


def test_settings_rows_current_and_scheduled(seeded):
    aid = acc(seeded, "5100")
    budgets.set_budget(seeded, aid, OCT, 1000)
    budgets.set_budget(seeded, aid, NOV, 2000)
    row = next(r for r in budgets.settings_rows(seeded, OCT).rows if r.account_id == aid)
    assert row.current.monthly_amount == 1000
    assert [(v.effective_from, v.monthly_amount) for v in row.scheduled] == [(NOV, 2000)]
    assert row.card is not None


def test_settings_rows_without_budget(seeded):
    row = budgets.settings_rows(seeded, OCT).rows[0]
    assert row.current is None and row.scheduled == () and row.card is None


def test_settings_rows_inactive_hidden_without_budget_shown_with(seeded):
    aid = acc(seeded, "5100")
    seeded.get(Account, aid).is_active = False
    seeded.commit()
    assert aid not in {r.account_id for r in budgets.settings_rows(seeded, OCT).rows}


def test_settings_rows_inactive_with_budget_shown(seeded):
    aid = acc(seeded, "5100")
    budgets.set_budget(seeded, aid, OCT, 1000)
    seeded.get(Account, aid).is_active = False
    seeded.commit()
    row = next(r for r in budgets.settings_rows(seeded, OCT).rows if r.account_id == aid)
    assert row.is_active is False and row.current.monthly_amount == 1000


def test_settings_rows_invalid_alert_pct_sets_progress_error(seeded):
    budgets.set_budget(seeded, acc(seeded, "5100"), OCT, 1000)
    seeded.get(Setting, "budget_alert_pct").value = "100"
    result = budgets.settings_rows(seeded, OCT)
    assert result.progress_error and result.rows
    assert all(r.card is None for r in result.rows)


def test_later_versions_only_after_month(seeded):
    aid = acc(seeded, "5100")
    budgets.set_budget(seeded, aid, OCT, 1000)
    budgets.set_budget(seeded, aid, NOV, 2000)
    later = budget_repo.later_versions(seeded, OCT)
    assert [(b.account_id, b.effective_from) for b in later] == [(aid, NOV)]
    assert budget_repo.later_versions(seeded, NOV) == []


def test_today_constant_matches_oct(seeded):
    assert TODAY == date(2026, 10, 15)
