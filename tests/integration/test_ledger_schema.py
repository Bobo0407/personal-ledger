"""P1 Step 1：帳務核心資料表的結構與約束（規格第 5、6 節）。

資料庫一律由真正的 migration 建立（migrated_engine）。這裡只驗證資料庫層的約束；
借貸平衡、最末層、啟用中等跨列規則屬於服務層，在之後的 Step 測試。
"""

import re

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError

from app.db import make_sessionmaker
from app.models import (
    Account,
    AuditLog,
    CreditCardProfile,
    Currency,
    Merchant,
    MerchantAlias,
    PaymentMethod,
    Posting,
    Setting,
    Transaction,
)
from app.models.enums import (
    AccountClass,
    AccountSubtype,
    AuditAction,
    LineRole,
    SystemAccountKey,
    TaxCategory,
    TxnSource,
    TxnStatus,
    TxnType,
    VoucherType,
    sql_in,
)

ISO_TAIPEI = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+08:00$")


@pytest.fixture
def session(migrated_engine):
    s = make_sessionmaker(migrated_engine)()
    yield s
    s.rollback()
    s.close()


def add_account(session, code="1101", account_class="ASSET", **kwargs) -> Account:
    account = Account(
        code=code, name=kwargs.pop("name", f"科目{code}"), account_class=account_class
    )
    for key, value in kwargs.items():
        setattr(account, key, value)
    session.add(account)
    session.commit()
    return account


def add_txn(session, **kwargs) -> Transaction:
    txn = Transaction(txn_date=kwargs.pop("txn_date", "2026-10-08"), txn_type="MANUAL")
    for key, value in kwargs.items():
        setattr(txn, key, value)
    session.add(txn)
    session.commit()
    return txn


def assert_rejected(session, obj) -> None:
    session.add(obj)
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


# --- 列舉：model 與 migration 的 CHECK 必須一致 -------------------------------


@pytest.mark.parametrize(
    ("table", "column", "enum"),
    [
        ("accounts", "account_class", AccountClass),
        ("accounts", "subtype", AccountSubtype),
        ("accounts", "tax_category", TaxCategory),
        ("accounts", "system_key", SystemAccountKey),
        ("transactions", "txn_type", TxnType),
        ("transactions", "status", TxnStatus),
        ("transactions", "source", TxnSource),
        ("transactions", "voucher_type", VoucherType),
        ("postings", "line_role", LineRole),
        ("audit_logs", "action", AuditAction),
    ],
)
def test_check_constraint_matches_enum(migrated_engine, table, column, enum):
    with migrated_engine.connect() as conn:
        ddl = conn.execute(
            text("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = :name"),
            {"name": table},
        ).scalar_one()
    assert sql_in(column, enum) in ddl


# 金額欄位（P1 Step 7）：只列真正的金額，不對所有欄位禁止 Numeric
MONEY_COLUMNS = [
    ("postings", "amount"),  # 新台幣元，帶正負號
    ("postings", "original_amount_minor"),  # 原幣最小單位
    ("credit_card_profiles", "credit_limit"),
]


@pytest.mark.parametrize(("table", "column"), MONEY_COLUMNS)
def test_money_columns_are_integer(migrated_engine, table, column):
    with migrated_engine.connect() as conn:
        types = {row[1]: row[2] for row in conn.execute(text(f"PRAGMA table_info({table})"))}
    assert types[column] == "INTEGER"


# --- accounts -----------------------------------------------------------------


def test_account_defaults(session):
    account = add_account(session)
    assert account.is_active is True
    assert account.show_on_home is False
    assert account.sort_order == 0
    assert account.parent_id is None
    assert account.system_key is None
    assert ISO_TAIPEI.match(account.created_at)
    assert ISO_TAIPEI.match(account.updated_at)


def test_account_code_is_unique(session):
    add_account(session, code="5101")
    assert_rejected(session, Account(code="5101", name="重複", account_class="EXPENSE"))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("account_class", "EXPENSES"),
        ("subtype", "WALLET"),
        ("tax_category", "BONUS"),
        ("system_key", "RETAINED_EARNINGS"),
    ],
)
def test_account_rejects_unknown_enum_value(session, field, value):
    account = Account(code="9999", name="錯誤", account_class="ASSET")
    setattr(account, field, value)
    assert_rejected(session, account)


@pytest.mark.parametrize("account_class", ["EQUITY", "INCOME", "EXPENSE"])
def test_subtype_only_for_asset_and_liability(session, account_class):
    assert_rejected(
        session, Account(code="9999", name="錯誤", account_class=account_class, subtype="OTHER")
    )


@pytest.mark.parametrize(
    ("account_class", "subtype"), [("ASSET", "BANK"), ("LIABILITY", "CREDIT_CARD")]
)
def test_subtype_allowed_for_asset_and_liability(session, account_class, subtype):
    account = add_account(session, account_class=account_class, subtype=subtype)
    assert account.subtype == subtype


def test_system_key_is_unique_but_nullable(session):
    add_account(session, code="3101", account_class="EQUITY", system_key="OPENING_EQUITY")
    add_account(session, code="3102", account_class="EQUITY")  # NULL 可以有很多筆
    add_account(session, code="3103", account_class="EQUITY")
    assert_rejected(
        session,
        Account(code="3104", name="重複", account_class="EQUITY", system_key="OPENING_EQUITY"),
    )


def test_account_hierarchy(session):
    parent = add_account(session, code="5100", account_class="EXPENSE", name="餐飲")
    child = add_account(session, code="5101", account_class="EXPENSE", name="正餐", parent=parent)
    assert child.parent_id == parent.id
    assert parent.children == [child]


def test_account_cannot_be_its_own_parent(session):
    account = add_account(session)
    account.parent_id = account.id
    with pytest.raises(IntegrityError):
        session.commit()


def test_account_parent_must_exist(session):
    assert_rejected(
        session, Account(code="5101", name="孤兒", account_class="EXPENSE", parent_id=999)
    )


# --- currencies ---------------------------------------------------------------


def test_currency_minor_unit(session):
    session.add_all(
        [
            Currency(code="USD", name="美元", minor_unit=2),
            Currency(code="JPY", name="日圓", minor_unit=0),
        ]
    )
    session.commit()
    assert_rejected(session, Currency(code="XXX", name="錯誤", minor_unit=-1))


# --- payment_methods ----------------------------------------------------------


def test_payment_method_maps_to_account(session):
    post_office = add_account(session, code="1102", subtype="BANK", name="郵局")
    method = PaymentMethod(name="LINE Pay", account=post_office)
    session.add(method)
    session.commit()
    assert method.account_id == post_office.id
    assert method.is_default is False
    assert method.is_active is True


def test_payment_method_requires_existing_account(session):
    assert_rejected(session, PaymentMethod(name="幽靈", account_id=999))


def test_payment_method_name_is_unique(session):
    account = add_account(session)
    session.add(PaymentMethod(name="現金", account=account))
    session.commit()
    assert_rejected(session, PaymentMethod(name="現金", account_id=account.id))


def test_only_one_default_payment_method(session):
    account = add_account(session)
    session.add_all(
        [
            PaymentMethod(name="LINE Pay", account=account, is_default=True),
            PaymentMethod(name="台灣Pay", account=account),  # 非預設可以有很多筆
            PaymentMethod(name="簽帳卡", account=account),
        ]
    )
    session.commit()
    assert_rejected(session, PaymentMethod(name="現金", account_id=account.id, is_default=True))


# --- credit_card_profiles -----------------------------------------------------


def test_credit_card_profile(session):
    bank = add_account(session, code="1103", subtype="BANK", name="聯邦活存")
    card = add_account(
        session, code="2101", account_class="LIABILITY", subtype="CREDIT_CARD", name="聯邦信用卡"
    )
    profile = CreditCardProfile(account=card, closing_day=15, due_day=3, autopay_account=bank)
    session.add(profile)
    session.commit()
    assert profile.account_id == card.id
    assert profile.autopay_account_id == bank.id
    assert profile.credit_limit is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("closing_day", 0),
        ("closing_day", 32),
        ("due_day", 0),
        ("due_day", 32),
        ("credit_limit", 0),
    ],
)
def test_credit_card_profile_ranges(session, field, value):
    card = add_account(session, code="2101", account_class="LIABILITY", subtype="CREDIT_CARD")
    profile = CreditCardProfile(account_id=card.id, closing_day=15, due_day=3)
    setattr(profile, field, value)
    assert_rejected(session, profile)


def test_credit_card_profile_is_one_per_account(session):
    card = add_account(session, code="2101", account_class="LIABILITY", subtype="CREDIT_CARD")
    session.add(CreditCardProfile(account_id=card.id, closing_day=15, due_day=3))
    session.commit()
    session.expunge_all()  # 讓重複的主鍵真正送到資料庫，而不是被 ORM 的 identity map 先攔下
    assert_rejected(session, CreditCardProfile(account_id=card.id, closing_day=20, due_day=8))


# --- merchants / merchant_aliases ---------------------------------------------


def test_merchant_with_aliases(session):
    merchant = Merchant(name="7-11")
    merchant.aliases.append(
        MerchantAlias(alias="統一超商股份有限公司", alias_normalized="統一超商股份有限公司")
    )
    session.add(merchant)
    session.commit()
    assert merchant.is_active is True
    assert merchant.default_account_id is None
    assert [a.alias for a in merchant.aliases] == ["統一超商股份有限公司"]


def test_merchant_name_is_unique(session):
    session.add(Merchant(name="7-11"))
    session.commit()
    assert_rejected(session, Merchant(name="7-11"))


def test_alias_normalized_is_unique_across_merchants(session):
    a, b = Merchant(name="7-11"), Merchant(name="全家")
    session.add_all([a, b])
    session.commit()
    session.add(MerchantAlias(merchant=a, alias="統一超商", alias_normalized="統一超商"))
    session.commit()
    assert_rejected(
        session, MerchantAlias(merchant_id=b.id, alias="統一超商 ", alias_normalized="統一超商")
    )


# --- transactions -------------------------------------------------------------


def test_transaction_defaults(session):
    txn = add_txn(session)
    assert txn.status == "POSTED"
    assert txn.source == "MANUAL"
    assert txn.exclude_from_budget is False
    assert txn.deleted_at is None
    assert txn.related_txn_id is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("txn_type", "PURCHASE"),
        ("status", "VOID"),
        ("source", "EMAIL"),
        ("voucher_type", "PHOTO"),
    ],
)
def test_transaction_rejects_unknown_enum_value(session, field, value):
    txn = Transaction(txn_date="2026-10-08", txn_type="MANUAL")
    setattr(txn, field, value)
    assert_rejected(session, txn)


@pytest.mark.parametrize("txn_date", ["2026/10/08", "2026-1-8", "20261008", "2026-10-08T12:00"])
def test_txn_date_must_be_iso_date(session, txn_date):
    assert_rejected(session, Transaction(txn_date=txn_date, txn_type="EXPENSE"))


@pytest.mark.parametrize(
    "invoice_no", ["ab12345678", "A123456789", "AB1234567", "AB123456789", "AB-1234567"]
)
def test_invoice_no_format_rejected(session, invoice_no):
    assert_rejected(
        session, Transaction(txn_date="2026-10-08", txn_type="EXPENSE", invoice_no=invoice_no)
    )


def test_invoice_no_unique_among_active_transactions(session):
    add_txn(session, invoice_no="AB12345678")
    assert_rejected(
        session, Transaction(txn_date="2026-10-09", txn_type="EXPENSE", invoice_no="AB12345678")
    )


def test_invoice_no_reusable_after_soft_delete(session):
    first = add_txn(session, invoice_no="AB12345678")
    first.deleted_at = "2026-10-08T12:00:00+08:00"
    session.commit()
    second = add_txn(session, invoice_no="AB12345678")
    assert second.id != first.id


def test_many_transactions_without_invoice_no(session):
    add_txn(session)
    add_txn(session)
    assert len(session.scalars(select(Transaction)).all()) == 2


def test_source_ref_unique_per_source(session):
    add_txn(session, source="RECURRING", source_ref="rule:3:2026-11")
    add_txn(session, source="IMPORT", source_ref="rule:3:2026-11")  # 不同來源可以同名
    add_txn(session, source="RECURRING")  # source_ref 為 NULL 不受限制
    add_txn(session, source="RECURRING")
    assert_rejected(
        session,
        Transaction(
            txn_date="2026-10-08",
            txn_type="TRANSFER",
            source="RECURRING",
            source_ref="rule:3:2026-11",
        ),
    )


def test_refund_links_to_original_transaction(session):
    original = add_txn(session, txn_type="EXPENSE")
    refund = add_txn(session, txn_type="REFUND", related_txn=original)
    assert refund.related_txn_id == original.id


def test_transaction_cannot_relate_to_itself(session):
    txn = add_txn(session)
    txn.related_txn_id = txn.id
    with pytest.raises(IntegrityError):
        session.commit()


@pytest.mark.parametrize("field", ["merchant_id", "payment_method_id", "related_txn_id"])
def test_transaction_references_must_exist(session, field):
    txn = Transaction(txn_date="2026-10-08", txn_type="EXPENSE")
    setattr(txn, field, 999)
    assert_rejected(session, txn)


# --- postings -----------------------------------------------------------------


@pytest.fixture
def two_accounts(session):
    return (
        add_account(session, code="5101", account_class="EXPENSE", name="正餐"),
        add_account(session, code="1102", subtype="BANK", name="郵局"),
    )


def test_balanced_transaction_with_postings(session, two_accounts):
    lunch, post_office = two_accounts
    txn = Transaction(txn_date="2026-10-08", txn_type="EXPENSE")
    txn.postings = [
        Posting(line_no=1, account=lunch, amount=120, line_role="MAIN"),
        Posting(line_no=2, account=post_office, amount=-120),
    ]
    session.add(txn)
    session.commit()

    assert [p.amount for p in txn.postings] == [120, -120]
    assert sum(p.amount for p in txn.postings) == 0


def test_postings_ordered_by_line_no(session, two_accounts):
    lunch, post_office = two_accounts
    txn = add_txn(session)
    session.add_all(
        [
            Posting(transaction=txn, line_no=2, account=post_office, amount=-120),
            Posting(transaction=txn, line_no=1, account=lunch, amount=120),
        ]
    )
    session.commit()
    session.expire(txn, ["postings"])
    assert [p.line_no for p in txn.postings] == [1, 2]


def test_posting_amount_cannot_be_zero(session, two_accounts):
    txn = add_txn(session)
    assert_rejected(
        session, Posting(transaction_id=txn.id, line_no=1, account_id=two_accounts[0].id, amount=0)
    )


def test_posting_line_no_starts_from_one(session, two_accounts):
    txn = add_txn(session)
    assert_rejected(
        session, Posting(transaction_id=txn.id, line_no=0, account_id=two_accounts[0].id, amount=1)
    )


def test_posting_line_no_unique_within_transaction(session, two_accounts):
    lunch, post_office = two_accounts
    txn = add_txn(session)
    session.add(Posting(transaction=txn, line_no=1, account=lunch, amount=120))
    session.commit()
    assert_rejected(
        session, Posting(transaction_id=txn.id, line_no=1, account_id=post_office.id, amount=-120)
    )


def test_posting_rejects_unknown_line_role(session, two_accounts):
    txn = add_txn(session)
    assert_rejected(
        session,
        Posting(
            transaction_id=txn.id,
            line_no=1,
            account_id=two_accounts[0].id,
            amount=1,
            line_role="DISCOUNT",
        ),
    )


@pytest.mark.parametrize(("currency", "amount_minor"), [("JPY", None), (None, 4800)])
def test_original_currency_and_amount_must_be_paired(session, two_accounts, currency, amount_minor):
    session.add(Currency(code="JPY", name="日圓", minor_unit=0))
    txn = add_txn(session)
    assert_rejected(
        session,
        Posting(
            transaction_id=txn.id,
            line_no=1,
            account_id=two_accounts[0].id,
            amount=1000,
            original_currency=currency,
            original_amount_minor=amount_minor,
        ),
    )


def test_original_currency_stored(session, two_accounts):
    session.add(Currency(code="JPY", name="日圓", minor_unit=0))
    txn = add_txn(session)
    posting = Posting(
        transaction=txn,
        line_no=1,
        account=two_accounts[0],
        amount=1000,
        line_role="MAIN",
        original_currency="JPY",
        original_amount_minor=4800,
    )
    session.add(posting)
    session.commit()
    assert (posting.original_currency, posting.original_amount_minor) == ("JPY", 4800)


def test_original_currency_must_exist(session, two_accounts):
    txn = add_txn(session)
    assert_rejected(
        session,
        Posting(
            transaction_id=txn.id,
            line_no=1,
            account_id=two_accounts[0].id,
            amount=1000,
            original_currency="XYZ",
            original_amount_minor=1,
        ),
    )


@pytest.mark.parametrize("field", ["transaction_id", "account_id"])
def test_posting_references_must_exist(session, two_accounts, field):
    txn = add_txn(session)
    posting = Posting(transaction_id=txn.id, line_no=1, account_id=two_accounts[0].id, amount=1)
    setattr(posting, field, 999)
    assert_rejected(session, posting)


def test_deleting_transaction_row_cascades_postings(session, two_accounts):
    # 正常流程只做軟刪除；這裡驗證的是資料庫層的 ON DELETE CASCADE
    lunch, post_office = two_accounts
    txn = add_txn(session)
    session.add_all(
        [
            Posting(transaction=txn, line_no=1, account=lunch, amount=120),
            Posting(transaction=txn, line_no=2, account=post_office, amount=-120),
        ]
    )
    session.commit()
    session.execute(delete(Transaction).where(Transaction.id == txn.id))
    session.commit()
    assert session.scalars(select(Posting)).all() == []


def test_account_with_postings_cannot_be_hard_deleted(session, two_accounts):
    # 已有分錄的科目只能停用（M01）；資料庫的外鍵是最後一道防線
    lunch, _ = two_accounts
    txn = add_txn(session)
    session.add(Posting(transaction=txn, line_no=1, account=lunch, amount=120))
    session.commit()
    with pytest.raises(IntegrityError):
        session.execute(delete(Account).where(Account.id == lunch.id))
        session.commit()


# --- settings / audit_logs ----------------------------------------------------


def test_setting_key_is_primary_key(session):
    session.add(Setting(key="large_amount_threshold", value="10000"))
    session.commit()
    session.expunge_all()
    assert_rejected(session, Setting(key="large_amount_threshold", value="20000"))


def test_audit_log_without_user(session):
    log = AuditLog(entity="transaction", entity_id=1, action="CREATE", after_json="{}")
    session.add(log)
    session.commit()
    assert log.user_id is None
    assert ISO_TAIPEI.match(log.occurred_at)


def test_audit_log_rejects_unknown_action(session):
    assert_rejected(session, AuditLog(entity="transaction", entity_id=1, action="PURGE"))


def test_audit_log_user_must_exist(session):
    assert_rejected(
        session, AuditLog(entity="transaction", entity_id=1, action="CREATE", user_id=999)
    )
