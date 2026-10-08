import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect

from app.db import make_engine
from app.models import Base

P0_TABLES = {"users", "user_sessions"}


def table_names(db_url: str) -> set[str]:
    engine = make_engine(db_url)
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_single_head(alembic_cfg):
    assert len(ScriptDirectory.from_config(alembic_cfg).get_heads()) == 1


def test_upgrade_creates_p0_tables_only(alembic_cfg, db_url):
    command.upgrade(alembic_cfg, "head")
    assert table_names(db_url) == P0_TABLES | {"alembic_version"}


def test_downgrade_removes_tables(alembic_cfg, db_url):
    command.upgrade(alembic_cfg, "head")
    command.downgrade(alembic_cfg, "base")
    assert table_names(db_url) & P0_TABLES == set()


def test_upgrade_downgrade_upgrade_roundtrip(alembic_cfg, db_url):
    command.upgrade(alembic_cfg, "head")
    command.downgrade(alembic_cfg, "base")
    command.upgrade(alembic_cfg, "head")
    assert P0_TABLES <= table_names(db_url)


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
    ],
)
def test_unique_constraints_have_stable_names(migrated_engine, table, expected):
    names = {uc["name"] for uc in inspect(migrated_engine).get_unique_constraints(table)}
    assert expected <= names
