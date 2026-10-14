import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text

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
P2_TABLES = {"tags", "transaction_tags"}  # 0003（P2 Step 8A）
P3_TABLES = {"budgets"}  # 0004（P3-3A）
ALL_TABLES = P0_TABLES | P1_TABLES | P2_TABLES | P3_TABLES


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


def test_upgrade_head_creates_all_tables(alembic_cfg, db_url):
    command.upgrade(alembic_cfg, "head")
    assert table_names(db_url) == ALL_TABLES | {"alembic_version"}


def test_revision_0002_has_no_tag_tables(alembic_cfg, db_url):
    command.upgrade(alembic_cfg, "0002")
    assert table_names(db_url) == P0_TABLES | P1_TABLES | {"alembic_version"}


def test_0003_downgrade_keeps_transactions_and_reupgrade(alembic_cfg, db_url):
    """0002 → 0003 → 0002 → 0003：舊交易資料仍在，downgrade 只丟標籤相關表。"""
    command.upgrade(alembic_cfg, "0002")
    engine = make_engine(db_url)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO transactions (id, txn_date, txn_type, status, source,"
                    " exclude_from_budget, created_at, updated_at)"
                    " VALUES (1, '2026-10-13', 'MANUAL', 'POSTED', 'MANUAL', 0, 'x', 'x')"
                )
            )
        command.upgrade(alembic_cfg, "0003")
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO tags (id, name, created_at, updated_at)"
                    " VALUES (1, '旅行', 'x', 'x')"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO transaction_tags (transaction_id, tag_id, created_at, updated_at)"
                    " VALUES (1, 1, 'x', 'x')"
                )
            )
        command.downgrade(alembic_cfg, "0002")
        assert table_names(db_url) == P0_TABLES | P1_TABLES | {"alembic_version"}
        with engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM transactions")).scalar() == 1
        command.upgrade(alembic_cfg, "0003")
        assert P2_TABLES <= table_names(db_url)
        with engine.connect() as conn:
            assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0003"
            assert conn.execute(text("SELECT count(*) FROM tags")).scalar() == 0
            assert conn.execute(text("SELECT count(*) FROM transactions")).scalar() == 1
    finally:
        engine.dispose()


def test_revision_0003_has_no_budgets(alembic_cfg, db_url):
    command.upgrade(alembic_cfg, "0003")
    assert table_names(db_url) == P0_TABLES | P1_TABLES | P2_TABLES | {"alembic_version"}


def test_head_is_0004(alembic_cfg):
    assert ScriptDirectory.from_config(alembic_cfg).get_heads() == ["0004"]


def _insert_account_and_budget(conn):
    conn.execute(
        text(
            "INSERT INTO accounts (id, code, name, account_class, is_active, show_on_home,"
            " sort_order, created_at, updated_at)"
            " VALUES (1, '5100', '餐飲', 'EXPENSE', 1, 0, 0, 'x', 'x')"
        )
    )
    conn.execute(
        text(
            "INSERT INTO budgets (id, account_id, monthly_amount, effective_from,"
            " created_at, updated_at) VALUES (1, 1, 5000, '2026-10', 'x', 'x')"
        )
    )


def test_0004_downgrade_keeps_other_data_and_reupgrade(alembic_cfg, db_url):
    """0003 → 0004 → 0003 → 0004：downgrade 只丟 budgets，帳戶與交易仍在；再升級後預算為空。"""
    command.upgrade(alembic_cfg, "0003")
    engine = make_engine(db_url)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO transactions (id, txn_date, txn_type, status, source,"
                    " exclude_from_budget, created_at, updated_at)"
                    " VALUES (1, '2026-10-14', 'MANUAL', 'POSTED', 'MANUAL', 0, 'x', 'x')"
                )
            )
        command.upgrade(alembic_cfg, "0004")
        with engine.begin() as conn:
            _insert_account_and_budget(conn)
        command.downgrade(alembic_cfg, "0003")
        assert "budgets" not in table_names(db_url)
        with engine.connect() as conn:
            assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0003"
            assert conn.execute(text("SELECT count(*) FROM transactions")).scalar() == 1
            assert conn.execute(text("SELECT count(*) FROM accounts")).scalar() == 1
        command.upgrade(alembic_cfg, "head")
        with engine.connect() as conn:
            assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0004"
            assert conn.execute(text("SELECT count(*) FROM budgets")).scalar() == 0
            assert conn.execute(text("SELECT count(*) FROM transactions")).scalar() == 1
    finally:
        engine.dispose()


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
        ("tags", {"uq_tags_name"}),
        ("budgets", {"uq_budgets_account_id_effective_from"}),
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
        ("transaction_tags", {"ix_transaction_tags_tag_id"}),
    ],
)
def test_indexes_have_stable_names(migrated_engine, table, expected):
    names = {ix["name"] for ix in inspect(migrated_engine).get_indexes(table)}
    assert expected <= names


def test_postings_cascade_on_transaction_delete(migrated_engine):
    fks = inspect(migrated_engine).get_foreign_keys("postings")
    txn_fk = next(fk for fk in fks if fk["referred_table"] == "transactions")
    assert txn_fk["options"].get("ondelete") == "CASCADE"


def test_transaction_tags_foreign_keys_do_not_cascade(migrated_engine):
    fks = inspect(migrated_engine).get_foreign_keys("transaction_tags")
    assert {fk["referred_table"] for fk in fks} == {"transactions", "tags"}
    assert all(not fk["options"].get("ondelete") for fk in fks)
    pk = inspect(migrated_engine).get_pk_constraint("transaction_tags")
    assert pk["constrained_columns"] == ["transaction_id", "tag_id"]


def test_no_balance_columns(migrated_engine):
    # 規格 5.3：餘額不儲存，一律由分錄加總
    insp = inspect(migrated_engine)
    for table in ALL_TABLES:
        columns = {c["name"] for c in insp.get_columns(table)}
        assert not any("balance" in name for name in columns), table


# --- budgets（0004）-----------------------------------------------------------------------


def test_budgets_columns_store_no_totals(migrated_engine):
    """預算只存金額與生效月；已用金額、使用率不儲存（P3-3A）。"""
    columns = {c["name"] for c in inspect(migrated_engine).get_columns("budgets")}
    assert columns == {
        "id",
        "account_id",
        "monthly_amount",
        "effective_from",
        "created_at",
        "updated_at",
    }


def test_budgets_foreign_key_does_not_cascade(migrated_engine):
    fks = inspect(migrated_engine).get_foreign_keys("budgets")
    assert [fk["referred_table"] for fk in fks] == ["accounts"]
    assert not fks[0]["options"].get("ondelete")


def _run(migrated_engine, sql: str, **params):
    with migrated_engine.begin() as conn:
        conn.execute(text(sql), params)


def _seed_account(migrated_engine):
    _run(
        migrated_engine,
        "INSERT INTO accounts (id, code, name, account_class, is_active, show_on_home,"
        " sort_order, created_at, updated_at)"
        " VALUES (1, '5100', '餐飲', 'EXPENSE', 1, 0, 0, 'x', 'x')",
    )


INSERT_BUDGET = (
    "INSERT INTO budgets (account_id, monthly_amount, effective_from, created_at, updated_at)"
    " VALUES (:a, :m, :e, 'x', 'x')"
)


def test_budgets_accepts_valid_row(migrated_engine):
    _seed_account(migrated_engine)
    _run(migrated_engine, INSERT_BUDGET, a=1, m=5000, e="2026-10")
    _run(migrated_engine, INSERT_BUDGET, a=1, m=6000, e="2026-12")


@pytest.mark.parametrize("amount", [0, -1])
def test_budgets_rejects_non_positive_amount(migrated_engine, amount):
    from sqlalchemy.exc import IntegrityError

    _seed_account(migrated_engine)
    with pytest.raises(IntegrityError):
        _run(migrated_engine, INSERT_BUDGET, a=1, m=amount, e="2026-10")


@pytest.mark.parametrize("month", ["2026-13", "2026-00", "2026-1", "202610", "2026-10-01", ""])
def test_budgets_rejects_bad_month_format(migrated_engine, month):
    from sqlalchemy.exc import IntegrityError

    _seed_account(migrated_engine)
    with pytest.raises(IntegrityError):
        _run(migrated_engine, INSERT_BUDGET, a=1, m=5000, e=month)


def test_budgets_unique_per_account_and_month(migrated_engine):
    from sqlalchemy.exc import IntegrityError

    _seed_account(migrated_engine)
    _run(migrated_engine, INSERT_BUDGET, a=1, m=5000, e="2026-10")
    with pytest.raises(IntegrityError):
        _run(migrated_engine, INSERT_BUDGET, a=1, m=7000, e="2026-10")


def test_budgets_requires_existing_account(migrated_engine):
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        _run(migrated_engine, INSERT_BUDGET, a=999, m=5000, e="2026-10")
