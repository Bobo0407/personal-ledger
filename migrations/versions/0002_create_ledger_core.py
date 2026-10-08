"""建立 P1 帳務核心資料表

accounts、currencies、payment_methods、credit_card_profiles、merchants、merchant_aliases、
transactions、postings、settings、audit_logs（規格第 5、6 節；決策 D1–D6、D8）。

列舉值刻意寫成字面常數，不 import app.models.enums：
migration 是歷史紀錄，日後改列舉時要另寫新的 migration，不能回頭改這一支。

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-08

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _in(column: str, *values: str) -> str:
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
    ]


ACCOUNT_CLASSES = ("ASSET", "LIABILITY", "EQUITY", "INCOME", "EXPENSE")
ACCOUNT_SUBTYPES = (
    "CASH",
    "BANK",
    "CREDIT_CARD",
    "RECEIVABLE",
    "PAYABLE",
    "PREPAID_TAX",
    "PENSION",
    "INVESTMENT",
    "OTHER",
)
TAX_CATEGORIES = ("SALARY", "INTEREST", "INSURANCE", "MEDICAL", "WITHHOLDING", "PENSION_SELF")
SYSTEM_KEYS = ("OPENING_EQUITY", "ADJUSTMENT")
TXN_TYPES = (
    "EXPENSE",
    "INCOME",
    "TRANSFER",
    "CARD_PAYMENT",
    "REFUND",
    "PAYROLL",
    "OPENING",
    "ADJUSTMENT",
    "MANUAL",
)
TXN_STATUSES = ("POSTED", "DRAFT")
TXN_SOURCES = ("MANUAL", "RECURRING", "CARD_AUTOPAY", "IMPORT")
VOUCHER_TYPES = ("EINVOICE", "PAPER_INVOICE", "RECEIPT", "NONE")
LINE_ROLES = ("MAIN", "FEE", "INTEREST", "TAX", "INSURANCE", "PENSION")
AUDIT_ACTIONS = ("CREATE", "UPDATE", "DELETE", "RESTORE")

DATE_GLOB = "[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]"
INVOICE_NO_GLOB = "[A-Z][A-Z]" + "[0-9]" * 8


def upgrade() -> None:
    op.create_table(
        "accounts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("account_class", sa.String(), nullable=False),
        sa.Column("subtype", sa.String(), nullable=True),
        sa.Column("parent_id", sa.Integer(), nullable=True),
        sa.Column("system_key", sa.String(), nullable=True),
        sa.Column("tax_category", sa.String(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("1"), nullable=False),
        sa.Column("show_on_home", sa.Boolean(), server_default=sa.text("0"), nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default=sa.text("0"), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            _in("account_class", *ACCOUNT_CLASSES), name=op.f("ck_accounts_account_class")
        ),
        sa.CheckConstraint(
            "subtype IS NULL OR " + _in("subtype", *ACCOUNT_SUBTYPES),
            name=op.f("ck_accounts_subtype"),
        ),
        sa.CheckConstraint(
            "subtype IS NULL OR account_class IN ('ASSET', 'LIABILITY')",
            name=op.f("ck_accounts_subtype_only_asset_liability"),
        ),
        sa.CheckConstraint(
            "tax_category IS NULL OR " + _in("tax_category", *TAX_CATEGORIES),
            name=op.f("ck_accounts_tax_category"),
        ),
        sa.CheckConstraint(
            "system_key IS NULL OR " + _in("system_key", *SYSTEM_KEYS),
            name=op.f("ck_accounts_system_key"),
        ),
        sa.CheckConstraint(
            "parent_id IS NULL OR parent_id <> id", name=op.f("ck_accounts_not_own_parent")
        ),
        sa.CheckConstraint("is_active IN (0, 1)", name=op.f("ck_accounts_is_active_bool")),
        sa.CheckConstraint(
            "show_on_home IN (0, 1)", name=op.f("ck_accounts_show_on_home_bool")
        ),
        sa.ForeignKeyConstraint(
            ["parent_id"], ["accounts.id"], name=op.f("fk_accounts_parent_id_accounts")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_accounts")),
        sa.UniqueConstraint("code", name=op.f("uq_accounts_code")),
        sa.UniqueConstraint("system_key", name=op.f("uq_accounts_system_key")),
    )
    op.create_index(op.f("ix_accounts_parent_id"), "accounts", ["parent_id"], unique=False)

    op.create_table(
        "currencies",
        sa.Column("code", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("minor_unit", sa.Integer(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "minor_unit >= 0", name=op.f("ck_currencies_minor_unit_non_negative")
        ),
        sa.PrimaryKeyConstraint("code", name=op.f("pk_currencies")),
    )

    op.create_table(
        "payment_methods",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("is_default", sa.Boolean(), server_default=sa.text("0"), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("1"), nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default=sa.text("0"), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "is_default IN (0, 1)", name=op.f("ck_payment_methods_is_default_bool")
        ),
        sa.CheckConstraint("is_active IN (0, 1)", name=op.f("ck_payment_methods_is_active_bool")),
        sa.ForeignKeyConstraint(
            ["account_id"], ["accounts.id"], name=op.f("fk_payment_methods_account_id_accounts")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_payment_methods")),
        sa.UniqueConstraint("name", name=op.f("uq_payment_methods_name")),
    )
    op.create_index(
        op.f("uq_payment_methods_single_default"),
        "payment_methods",
        ["is_default"],
        unique=True,
        sqlite_where=sa.text("is_default = 1"),
    )

    op.create_table(
        "credit_card_profiles",
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("closing_day", sa.Integer(), nullable=False),
        sa.Column("due_day", sa.Integer(), nullable=False),
        sa.Column("autopay_account_id", sa.Integer(), nullable=True),
        sa.Column("credit_limit", sa.Integer(), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "closing_day BETWEEN 1 AND 31", name=op.f("ck_credit_card_profiles_closing_day_range")
        ),
        sa.CheckConstraint(
            "due_day BETWEEN 1 AND 31", name=op.f("ck_credit_card_profiles_due_day_range")
        ),
        sa.CheckConstraint(
            "credit_limit IS NULL OR credit_limit > 0",
            name=op.f("ck_credit_card_profiles_credit_limit_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
            name=op.f("fk_credit_card_profiles_account_id_accounts"),
        ),
        sa.ForeignKeyConstraint(
            ["autopay_account_id"],
            ["accounts.id"],
            name=op.f("fk_credit_card_profiles_autopay_account_id_accounts"),
        ),
        sa.PrimaryKeyConstraint("account_id", name=op.f("pk_credit_card_profiles")),
    )

    op.create_table(
        "merchants",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("default_account_id", sa.Integer(), nullable=True),
        sa.Column("default_payment_method_id", sa.Integer(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("1"), nullable=False),
        *_timestamps(),
        sa.CheckConstraint("is_active IN (0, 1)", name=op.f("ck_merchants_is_active_bool")),
        sa.ForeignKeyConstraint(
            ["default_account_id"],
            ["accounts.id"],
            name=op.f("fk_merchants_default_account_id_accounts"),
        ),
        sa.ForeignKeyConstraint(
            ["default_payment_method_id"],
            ["payment_methods.id"],
            name=op.f("fk_merchants_default_payment_method_id_payment_methods"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_merchants")),
        sa.UniqueConstraint("name", name=op.f("uq_merchants_name")),
    )

    op.create_table(
        "merchant_aliases",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("merchant_id", sa.Integer(), nullable=False),
        sa.Column("alias", sa.String(), nullable=False),
        sa.Column("alias_normalized", sa.String(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["merchant_id"],
            ["merchants.id"],
            name=op.f("fk_merchant_aliases_merchant_id_merchants"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_merchant_aliases")),
        sa.UniqueConstraint(
            "alias_normalized", name=op.f("uq_merchant_aliases_alias_normalized")
        ),
    )
    op.create_index(
        op.f("ix_merchant_aliases_merchant_id"), "merchant_aliases", ["merchant_id"], unique=False
    )

    op.create_table(
        "transactions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("txn_date", sa.String(), nullable=False),
        sa.Column("txn_type", sa.String(), nullable=False),
        sa.Column("status", sa.String(), server_default=sa.text("'POSTED'"), nullable=False),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column("merchant_id", sa.Integer(), nullable=True),
        sa.Column("payment_method_id", sa.Integer(), nullable=True),
        sa.Column("voucher_type", sa.String(), nullable=True),
        sa.Column("invoice_no", sa.String(), nullable=True),
        sa.Column(
            "exclude_from_budget", sa.Boolean(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("related_txn_id", sa.Integer(), nullable=True),
        sa.Column("source", sa.String(), server_default=sa.text("'MANUAL'"), nullable=False),
        sa.Column("source_ref", sa.String(), nullable=True),
        sa.Column("deleted_at", sa.String(), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(_in("txn_type", *TXN_TYPES), name=op.f("ck_transactions_txn_type")),
        sa.CheckConstraint(_in("status", *TXN_STATUSES), name=op.f("ck_transactions_status")),
        sa.CheckConstraint(_in("source", *TXN_SOURCES), name=op.f("ck_transactions_source")),
        sa.CheckConstraint(
            "voucher_type IS NULL OR " + _in("voucher_type", *VOUCHER_TYPES),
            name=op.f("ck_transactions_voucher_type"),
        ),
        sa.CheckConstraint(
            f"txn_date GLOB '{DATE_GLOB}'", name=op.f("ck_transactions_txn_date_format")
        ),
        sa.CheckConstraint(
            f"invoice_no IS NULL OR invoice_no GLOB '{INVOICE_NO_GLOB}'",
            name=op.f("ck_transactions_invoice_no_format"),
        ),
        sa.CheckConstraint(
            "related_txn_id IS NULL OR related_txn_id <> id",
            name=op.f("ck_transactions_not_related_to_self"),
        ),
        sa.CheckConstraint(
            "exclude_from_budget IN (0, 1)",
            name=op.f("ck_transactions_exclude_from_budget_bool"),
        ),
        sa.ForeignKeyConstraint(
            ["merchant_id"], ["merchants.id"], name=op.f("fk_transactions_merchant_id_merchants")
        ),
        sa.ForeignKeyConstraint(
            ["payment_method_id"],
            ["payment_methods.id"],
            name=op.f("fk_transactions_payment_method_id_payment_methods"),
        ),
        sa.ForeignKeyConstraint(
            ["related_txn_id"],
            ["transactions.id"],
            name=op.f("fk_transactions_related_txn_id_transactions"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_transactions")),
        sa.UniqueConstraint(
            "source", "source_ref", name=op.f("uq_transactions_source_source_ref")
        ),
    )
    op.create_index(
        op.f("uq_transactions_invoice_no_active"),
        "transactions",
        ["invoice_no"],
        unique=True,
        sqlite_where=sa.text("deleted_at IS NULL AND invoice_no IS NOT NULL"),
    )

    op.create_table(
        "postings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("transaction_id", sa.Integer(), nullable=False),
        sa.Column("line_no", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("line_role", sa.String(), nullable=True),
        sa.Column("original_currency", sa.String(), nullable=True),
        sa.Column("original_amount_minor", sa.Integer(), nullable=True),
        sa.Column("memo", sa.String(), nullable=True),
        *_timestamps(),
        sa.CheckConstraint("amount <> 0", name=op.f("ck_postings_amount_nonzero")),
        sa.CheckConstraint("line_no >= 1", name=op.f("ck_postings_line_no_positive")),
        sa.CheckConstraint(
            "line_role IS NULL OR " + _in("line_role", *LINE_ROLES),
            name=op.f("ck_postings_line_role"),
        ),
        sa.CheckConstraint(
            "(original_currency IS NULL) = (original_amount_minor IS NULL)",
            name=op.f("ck_postings_original_currency_pair"),
        ),
        sa.ForeignKeyConstraint(
            ["account_id"], ["accounts.id"], name=op.f("fk_postings_account_id_accounts")
        ),
        sa.ForeignKeyConstraint(
            ["original_currency"],
            ["currencies.code"],
            name=op.f("fk_postings_original_currency_currencies"),
        ),
        sa.ForeignKeyConstraint(
            ["transaction_id"],
            ["transactions.id"],
            name=op.f("fk_postings_transaction_id_transactions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_postings")),
        sa.UniqueConstraint(
            "transaction_id", "line_no", name=op.f("uq_postings_transaction_id_line_no")
        ),
    )
    op.create_index(op.f("ix_postings_account_id"), "postings", ["account_id"], unique=False)
    op.create_index(
        op.f("ix_postings_transaction_id"), "postings", ["transaction_id"], unique=False
    )

    op.create_table(
        "settings",
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("value", sa.String(), nullable=False),
        *_timestamps(),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_settings")),
    )

    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("occurred_at", sa.String(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("entity", sa.String(), nullable=False),
        sa.Column("entity_id", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("before_json", sa.String(), nullable=True),
        sa.Column("after_json", sa.String(), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(_in("action", *AUDIT_ACTIONS), name=op.f("ck_audit_logs_action")),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_audit_logs_user_id_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_logs")),
    )
    op.create_index(
        op.f("ix_audit_logs_entity_entity_id"),
        "audit_logs",
        ["entity", "entity_id"],
        unique=False,
    )


def downgrade() -> None:
    # 依外鍵相依的反向順序刪除；drop_table 會一併刪除該表的索引
    op.drop_table("audit_logs")
    op.drop_table("settings")
    op.drop_table("postings")
    op.drop_table("transactions")
    op.drop_table("merchant_aliases")
    op.drop_table("merchants")
    op.drop_table("credit_card_profiles")
    op.drop_table("payment_methods")
    op.drop_table("currencies")
    op.drop_table("accounts")
