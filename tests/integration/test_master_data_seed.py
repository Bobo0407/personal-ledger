"""主資料 seed：載入、重複執行、檔案檢查、衝突中止、CLI（P1 Step 2；決策 S1–S6、S11）。"""

import copy
import importlib.util
from pathlib import Path

import pytest
import yaml
from sqlalchemy import func, select

from app.models import Account, CreditCardProfile, Currency, PaymentMethod, Setting
from app.repositories import accounts as account_repo
from app.services import accounts as account_service
from app.services.master_data_seed import (
    DEFAULT_SEED_FILE,
    SeedConflict,
    SeedFileError,
    load_seed_file,
    parse_seed,
    seed_master_data,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TEST_CHART = PROJECT_ROOT / "tests" / "fixtures" / "chart_of_accounts_test.yaml"
SCRIPT = PROJECT_ROOT / "scripts" / "seed_master_data.py"


def count(db, model) -> int:
    return db.scalar(select(func.count()).select_from(model))


def by_code(db, code: str) -> Account:
    account = account_repo.get_by_code(db, code)
    assert account is not None, code
    return account


def raw_chart() -> dict:
    return yaml.safe_load(TEST_CHART.read_text(encoding="utf-8"))


def write_yaml(tmp_path, data: dict) -> Path:
    path = tmp_path / "chart.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return path


# --- 正常載入 -------------------------------------------------------------------


def test_seed_creates_master_data(ledger_db):
    report = seed_master_data(ledger_db, TEST_CHART)

    assert count(ledger_db, Account) == 26
    assert count(ledger_db, Currency) == 2
    assert count(ledger_db, PaymentMethod) == 6
    assert count(ledger_db, Setting) == 2
    assert report.count(report.created, "科目") == 26
    assert report.skipped == []


def test_seed_builds_tree_and_inherits_class(seeded_db):
    dining, lunch = by_code(seeded_db, "5100"), by_code(seeded_db, "5101")
    assert lunch.parent_id == dining.id
    assert lunch.account_class == "EXPENSE"
    assert account_service.is_leaf(seeded_db, lunch.id)
    assert not account_service.is_leaf(seeded_db, dining.id)


def test_seed_system_accounts(seeded_db):
    assert account_repo.get_by_system_key(seeded_db, "OPENING_EQUITY").code == "3101"
    assert account_repo.get_by_system_key(seeded_db, "ADJUSTMENT").code == "3102"


def test_seed_payment_methods_and_default(seeded_db):
    default = seeded_db.scalar(select(PaymentMethod).where(PaymentMethod.is_default.is_(True)))
    assert default.name == "LINE Pay"
    assert default.account.code == "1102"
    names = [
        pm.name
        for pm in seeded_db.scalars(select(PaymentMethod).order_by(PaymentMethod.sort_order))
    ]
    assert names == ["LINE Pay", "台灣Pay", "郵局簽帳卡", "現金", "聯邦信用卡", "聯邦轉帳"]


def test_seed_settings_without_default_payment_method(seeded_db):
    # 決策 D4：預設付款方式只存在 payment_methods.is_default
    keys = {s.key: s.value for s in seeded_db.scalars(select(Setting))}
    assert keys == {"large_amount_threshold": "10000", "budget_alert_pct": "80"}


def test_seed_currencies_exclude_twd(seeded_db):
    assert {c.code: c.minor_unit for c in seeded_db.scalars(select(Currency))} == {
        "USD": 2,
        "JPY": 0,
    }


def test_seed_credit_card_profile_only_when_yaml_provides_it(seeded_db):
    profile = seeded_db.get(CreditCardProfile, by_code(seeded_db, "2101").id)
    assert (profile.closing_day, profile.due_day) == (15, 3)
    assert profile.autopay_account_id == by_code(seeded_db, "1103").id


def test_credit_card_without_block_has_no_profile(ledger_db, tmp_path):
    data = raw_chart()
    card = next(a for a in data["accounts"] if a["code"] == "2101")
    del card["credit_card"]
    seed_master_data(ledger_db, write_yaml(tmp_path, data))
    assert by_code(ledger_db, "2101").subtype == "CREDIT_CARD"
    assert count(ledger_db, CreditCardProfile) == 0  # 尚未設定信用卡週期（S1-a）


# --- 重複執行 -------------------------------------------------------------------


def test_seed_twice_creates_nothing_new(seeded_db):
    report = seed_master_data(seeded_db, TEST_CHART)
    assert count(seeded_db, Account) == 26
    assert sum(len(v) for v in report.created.values()) == 0
    assert report.count(report.unchanged, "科目") == 26


def test_seed_keeps_user_maintained_fields(seeded_db):
    lunch = by_code(seeded_db, "5101")
    account_service.rename_account(seeded_db, lunch.id, "午晚餐")
    lunch.sort_order = 99
    seeded_db.commit()

    report = seed_master_data(seeded_db, TEST_CHART)

    seeded_db.refresh(lunch)
    assert (lunch.name, lunch.sort_order) == ("午晚餐", 99)
    assert any("5101" in line for line in report.skipped)


def test_seed_does_not_change_existing_default(seeded_db):
    cash = seeded_db.scalar(select(PaymentMethod).where(PaymentMethod.name == "現金"))
    from app.services import payment_methods as pm_service

    pm_service.set_default(seeded_db, cash.id)
    seeded_db.delete(seeded_db.scalar(select(PaymentMethod).where(PaymentMethod.name == "台灣Pay")))
    seeded_db.commit()

    seed_master_data(seeded_db, TEST_CHART)

    defaults = seeded_db.scalars(select(PaymentMethod).where(PaymentMethod.is_default.is_(True)))
    assert [pm.name for pm in defaults] == ["現金"]
    assert count(seeded_db, PaymentMethod) == 6  # 被刪掉的「台灣Pay」補回來


def test_seed_adds_missing_accounts_only(seeded_db, tmp_path):
    data = raw_chart()
    dining = next(a for a in data["accounts"] if a["code"] == "5100")
    dining["children"].append({"code": "5104", "name": "宵夜"})
    report = seed_master_data(seeded_db, write_yaml(tmp_path, data))
    assert report.created["科目"] == ["5104 宵夜"]
    assert by_code(seeded_db, "5104").parent_id == by_code(seeded_db, "5100").id


# --- 結構性衝突：整批中止 ---------------------------------------------------------


def _assert_conflict_writes_nothing(db, path) -> None:
    before = (count(db, Account), count(db, PaymentMethod), count(db, Currency))
    with pytest.raises(SeedConflict):
        seed_master_data(db, path)
    db.rollback()
    assert (count(db, Account), count(db, PaymentMethod), count(db, Currency)) == before


@pytest.mark.parametrize(
    ("code", "field", "value"),
    [
        ("1101", "class", "LIABILITY"),
        ("1102", "subtype", "CASH"),
        ("3102", "system_key", None),
    ],
)
def test_structural_conflict_aborts_whole_seed(seeded_db, tmp_path, code, field, value):
    data = raw_chart()
    account = next(a for a in data["accounts"] if a["code"] == code)
    if field == "class" and value == "LIABILITY":
        account["subtype"] = "PAYABLE"
    if value is None:
        account.pop(field)
        # 讓檔案本身仍合法：把 ADJUSTMENT 換到新的科目上
        data["accounts"].append(
            {"code": "3199", "name": "調整2", "class": "EQUITY", "system_key": "ADJUSTMENT"}
        )
    else:
        account[field] = value
    data["accounts"].append(
        {"code": "1999", "name": "新科目", "class": "ASSET", "subtype": "OTHER"}
    )
    _assert_conflict_writes_nothing(seeded_db, write_yaml(tmp_path, data))
    assert account_repo.get_by_code(seeded_db, "1999") is None


def test_parent_change_is_structural_conflict(seeded_db, tmp_path):
    data = raw_chart()
    drinks_group = next(a for a in data["accounts"] if a["code"] == "5200")
    dining = next(a for a in data["accounts"] if a["code"] == "5100")
    drink = next(c for c in dining["children"] if c["code"] == "5102")
    dining["children"].remove(drink)
    drinks_group["children"].append(drink)
    _assert_conflict_writes_nothing(seeded_db, write_yaml(tmp_path, data))


def test_currency_minor_unit_change_is_conflict(seeded_db, tmp_path):
    data = raw_chart()
    data["currencies"][1]["minor_unit"] = 2
    _assert_conflict_writes_nothing(seeded_db, write_yaml(tmp_path, data))


def test_currency_name_change_is_only_skipped(seeded_db, tmp_path):
    data = raw_chart()
    data["currencies"][0]["name"] = "US Dollar"
    report = seed_master_data(seeded_db, write_yaml(tmp_path, data))
    assert seeded_db.get(Currency, "USD").name == "美元"
    assert any("USD" in line for line in report.skipped)


def test_new_child_under_account_with_postings_is_conflict(seeded_db, tmp_path):
    from app.models import Posting, Transaction

    lunch = by_code(seeded_db, "5101")
    txn = Transaction(txn_date="2026-10-11", txn_type="MANUAL")
    txn.postings = [Posting(line_no=1, account_id=lunch.id, amount=100)]  # 測試專用：直接寫分錄
    seeded_db.add(txn)
    seeded_db.commit()

    data = raw_chart()
    dining = next(a for a in data["accounts"] if a["code"] == "5100")
    lunch_seed = next(c for c in dining["children"] if c["code"] == "5101")
    lunch_seed["children"] = [{"code": "510101", "name": "便當"}]
    _assert_conflict_writes_nothing(seeded_db, write_yaml(tmp_path, data))


# --- 檔案檢查：問題一次列出，不寫入 -------------------------------------------------


def _mutate(data: dict, case: str) -> None:
    accounts = data["accounts"]
    dining = next(a for a in accounts if a["code"] == "5100")
    if case == "duplicate_code":
        accounts.append({"code": "1101", "name": "重複", "class": "ASSET", "subtype": "CASH"})
    elif case == "integer_code":
        accounts.append({"code": 1999, "name": "沒加引號", "class": "ASSET", "subtype": "CASH"})
    elif case == "child_class_mismatch":
        dining["children"].append({"code": "5199", "name": "錯類", "class": "INCOME"})
    elif case == "missing_system_key":
        next(a for a in accounts if a["code"] == "3101").pop("system_key")
    elif case == "subtype_on_expense":
        dining["children"][0]["subtype"] = "OTHER"
    elif case == "subtype_wrong_class":
        next(a for a in accounts if a["code"] == "1101")["subtype"] = "CREDIT_CARD"
    elif case == "asset_without_subtype":
        next(a for a in accounts if a["code"] == "1101").pop("subtype")
    elif case == "tax_category_on_group":
        dining["tax_category"] = "MEDICAL"
    elif case == "sibling_name_duplicate":
        dining["children"].append({"code": "5199", "name": "正餐"})
    elif case == "pm_to_group":
        data["payment_methods"][0]["account"] = "5100"
    elif case == "pm_to_equity":
        data["payment_methods"][0]["account"] = "3101"
    elif case == "two_defaults":
        data["payment_methods"][1]["default"] = True
    elif case == "card_missing_due_day":
        next(a for a in accounts if a["code"] == "2101")["credit_card"].pop("due_day")
    elif case == "card_day_out_of_range":
        next(a for a in accounts if a["code"] == "2101")["credit_card"]["closing_day"] = 32
    elif case == "card_autopay_not_bank":
        next(a for a in accounts if a["code"] == "2101")["credit_card"]["autopay"] = "1101"
    elif case == "card_block_on_non_card":
        next(a for a in accounts if a["code"] == "1101")["credit_card"] = {
            "closing_day": 1,
            "due_day": 2,
        }
    elif case == "unknown_setting":
        data["settings"]["default_payment_method_id"] = 1
    elif case == "unknown_field":
        dining["budget"] = 5000
    else:  # pragma: no cover
        raise AssertionError(case)


FILE_ERROR_CASES = [
    "duplicate_code",
    "integer_code",
    "child_class_mismatch",
    "missing_system_key",
    "subtype_on_expense",
    "subtype_wrong_class",
    "asset_without_subtype",
    "tax_category_on_group",
    "sibling_name_duplicate",
    "pm_to_group",
    "pm_to_equity",
    "two_defaults",
    "card_missing_due_day",
    "card_day_out_of_range",
    "card_autopay_not_bank",
    "card_block_on_non_card",
    "unknown_setting",
    "unknown_field",
]


@pytest.mark.parametrize("case", FILE_ERROR_CASES)
def test_invalid_seed_file_is_rejected(case):
    data = copy.deepcopy(raw_chart())
    _mutate(data, case)
    with pytest.raises(SeedFileError):
        parse_seed(data)


def test_invalid_file_writes_nothing(ledger_db, tmp_path):
    data = raw_chart()
    _mutate(data, "two_defaults")
    with pytest.raises(SeedFileError):
        seed_master_data(ledger_db, write_yaml(tmp_path, data))
    assert count(ledger_db, Account) == 0


def test_file_errors_are_reported_together():
    data = raw_chart()
    _mutate(data, "duplicate_code")
    _mutate(data, "two_defaults")
    with pytest.raises(SeedFileError) as exc_info:
        parse_seed(data)
    assert len(exc_info.value.problems) >= 2


def test_yaml_is_loaded_with_safe_load(tmp_path):
    path = tmp_path / "evil.yaml"
    path.write_text("!!python/object/apply:os.system ['echo hacked']\n", encoding="utf-8")
    with pytest.raises(SeedFileError):
        load_seed_file(path)


def test_production_chart_is_valid_and_loads(ledger_db):
    data = load_seed_file(DEFAULT_SEED_FILE)
    report = seed_master_data(ledger_db, DEFAULT_SEED_FILE)

    assert report.count(report.created, "科目") == len(data.accounts) == 42
    card = by_code(ledger_db, "2101")
    assert card.subtype == "CREDIT_CARD"
    assert ledger_db.get(CreditCardProfile, card.id) is None  # S1：真實日期確認前不建立設定
    first_level_expenses = [
        a.name
        for a in ledger_db.scalars(
            select(Account).where(Account.account_class == "EXPENSE", Account.parent_id.is_(None))
        )
    ]
    assert len(first_level_expenses) == 9


# --- CLI ------------------------------------------------------------------------


def load_script():
    spec = importlib.util.spec_from_file_location("seed_master_data_script", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_script(db_url, *args):
    messages: list[str] = []
    code = load_script().main(list(args), database_url=db_url, out=messages.append)
    return code, messages


def test_cli_dry_run_writes_nothing(migrated_engine, db_url, ledger_db):
    code, messages = run_script(db_url, "--dry-run", "--file", str(TEST_CHART))
    assert code == 0
    assert messages[0] == "【試算，未寫入】"
    assert "科目：新增 26、已存在 0" in messages
    assert count(ledger_db, Account) == 0


def test_cli_seeds_and_is_repeatable(migrated_engine, db_url, ledger_db):
    assert run_script(db_url, "--file", str(TEST_CHART))[0] == 0
    code, messages = run_script(db_url, "--file", str(TEST_CHART))
    assert code == 0
    assert "科目：新增 0、已存在 26" in messages
    assert count(ledger_db, Account) == 26


def test_cli_refuses_unmigrated_database(alembic_cfg, db_url):
    from alembic import command

    command.upgrade(alembic_cfg, "0001")
    code, messages = run_script(db_url, "--file", str(TEST_CHART))
    assert code == 1
    assert "alembic upgrade head" in messages[0]


def test_cli_reports_conflict(migrated_engine, db_url, ledger_db, tmp_path):
    assert run_script(db_url, "--file", str(TEST_CHART))[0] == 0
    data = raw_chart()
    data["currencies"][1]["minor_unit"] = 2
    code, messages = run_script(db_url, "--file", str(write_yaml(tmp_path, data)))
    assert code == 1
    assert "衝突" in messages[0]
