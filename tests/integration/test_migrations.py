import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect

from app.db import make_engine
from app.models import Base

P0_TABLES = {"users", "user_sessions"}
P1_TABLES = {
    "accounts",
    "currencies",
    "payment_methods",
    "credit_card_profiles",
    "merchants",
    "merchant_aliases",
    "transactions",
    "postings",
    "settings",
    "audit_logs",
}
ALL_TABLES = P0_TABLES | P1_TABLES


def table_names(db_url: str) -> set[str]:
    engine = make_engine(db_url)
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_single_head(alembic_cfg):
    assert len(ScriptDirectory.from_config(alembic_cfg).get_heads()) == 1


def test_revision_0001_creates_p0_tables_only(alembic_cfg, db_url):
    command.upgrade(alembic_cfg, "0001")
    assert table_names(db_url) == P0_TABLES | {"alembic_version"}


def test_upgrade_head_creates_p0_and_p1_tables(alembic_cfg, db_url):
    command.upgrade(alembic_cfg, "head")
    assert table_names(db_url) == ALL_TABLES | {"alembic_version"}


def test_downgrade_to_0001_keeps_p0_tables_only(alembic_cfg, db_url):
    command.upgrade(alembic_cfg, "head")
    command.downgrade(alembic_cfg, "0001")
    assert table_names(db_url) == P0_TABLES | {"alembic_version"}


def test_downgrade_removes_tables(alembic_cfg, db_url):
    command.upgrade(alembic_cfg, "head")
    command.downgrade(alembic_cfg, "base")
    assert table_names(db_url) & ALL_TABLES == set()


def test_upgrade_downgrade_upgrade_roundtrip(alembic_cfg, db_url):
    command.upgrade(alembic_cfg, "head")
    command.downgrade(alembic_cfg, "base")
    command.upgrade(alembic_cfg, "head")
    assert ALL_TABLES <= table_names(db_url)


def test_models_match_migrations(migrated_engine):
    # 模型和 migration 不一致時（例如改了模型卻忘記寫 migration），這個測試會失敗
    with migrated_engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == [], f"模型與 migration 不一致：{diff}"


@pytest.mark.parametrize(
    ("table", "expected"),
    [
        ("users", {"uq_users_username"}),
        ("user_sessions", {"uq_user_sessions_token_hash"}),
        ("accounts", {"uq_accounts_code", "uq_accounts_system_key"}),
        ("payment_methods", {"uq_payment_methods_name"}),
        ("merchants", {"uq_merchants_name"}),
        ("merchant_aliases", {"uq_merchant_aliases_alias_normalized"}),
        ("transactions", {"uq_transactions_source_source_ref"}),
        ("postings", {"uq_postings_transaction_id_line_no"}),
    ],
)
def test_unique_constraints_have_stable_names(migrated_engine, table, expected):
    names = {uc["name"] for uc in inspect(migrated_engine).get_unique_constraints(table)}
    assert expected <= names


@pytest.mark.parametrize(
    ("table", "expected"),
    [
        ("accounts", {"ix_accounts_parent_id"}),
        ("payment_methods", {"uq_payment_methods_single_default"}),
        ("merchant_aliases", {"ix_merchant_aliases_merchant_id"}),
        ("transactions", {"uq_transactions_invoice_no_active"}),
        ("postings", {"ix_postings_account_id", "ix_postings_transaction_id"}),
        ("audit_logs", {"ix_audit_logs_entity_entity_id"}),
    ],
)
def test_indexes_have_stable_names(migrated_engine, table, expected):
    names = {ix["name"] for ix in inspect(migrated_engine).get_indexes(table)}
    assert expected <= names


def test_postings_cascade_on_transaction_delete(migrated_engine):
    fks = inspect(migrated_engine).get_foreign_keys("postings")
    txn_fk = next(fk for fk in fks if fk["referred_table"] == "transactions")
    assert txn_fk["options"].get("ondelete") == "CASCADE"


def test_no_balance_columns(migrated_engine):
    # 規格 5.3：餘額不儲存，一律由分錄加總
    insp = inspect(migrated_engine)
    for table in ALL_TABLES:
        columns = {c["name"] for c in insp.get_columns(table)}
        assert not any("balance" in name for name in columns), table
