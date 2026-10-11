"""主資料初始資料（seed）：科目表、幣別、付款方式、系統設定（P1 Step 2；決策 S1–S6、S11）。

流程分三段，任何一段失敗都不寫入資料庫：
1. load_seed_file()：讀 YAML（只用 yaml.safe_load）並檢查檔案本身，問題一次列出，丟 SeedFileError。
2. plan_seed()：和資料庫比對，算出要新增什麼、哪些已存在、哪些與 YAML 不同。
   結構性欄位不同是真正的衝突，丟 SeedConflict，整批中止。
3. apply_seed()：在單一資料庫交易內寫入；dry-run 只做到第 2 段。

重複執行策略（S6、S6-a）：只新增缺少的資料，不覆寫、不刪除使用者已維護的主資料。
- 結構性欄位（不同 → 整批中止）：
  accounts 的 account_class、subtype、system_key、上層科目；currencies 的 minor_unit。
- 使用者可維護的欄位（不同 → 保留資料庫的值，列為「略過」）：
  accounts 的 name、sort_order、show_on_home、is_active、tax_category；
  payment_methods 的扣款帳戶、is_default、is_active、sort_order；
  settings 的 value；currencies 的 name。
- 信用卡設定只有在 YAML 明確提供 credit_card 區塊時才建立與檢查（S1-a）；已有設定時不覆寫。
- 資料庫已有其他預設付款方式時，不改動它。
- seed 不寫 audit_logs（S7）。
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy.orm import Session

from app.models import Account, CreditCardProfile, Currency, PaymentMethod, Setting
from app.models.enums import AccountClass, AccountSubtype, SystemAccountKey
from app.repositories import accounts as account_repo
from app.repositories import payment_methods as pm_repo
from app.services.accounts import (
    BALANCE_SHEET_CLASSES,
    check_account_class,
    check_can_add_child,
    check_subtype,
    check_tax_category,
)
from app.services.errors import RuleViolation

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SEED_FILE = PROJECT_ROOT / "seeds" / "chart_of_accounts.yaml"

# 系統設定只接受這些 key，值必須是正整數（規格 6.10；D4：不含預設付款方式）
KNOWN_SETTINGS = ("large_amount_threshold", "budget_alert_pct")

TOP_LEVEL_KEYS = {"accounts", "currencies", "payment_methods", "settings"}
ACCOUNT_KEYS = {
    "code",
    "name",
    "class",
    "subtype",
    "system_key",
    "tax_category",
    "show_on_home",
    "sort_order",
    "children",
    "credit_card",
}
CARD_KEYS = {"closing_day", "due_day", "autopay", "credit_limit"}
CURRENCY_KEYS = {"code", "name", "minor_unit"}
PM_KEYS = {"name", "account", "default", "sort_order"}


class SeedFileError(Exception):
    """YAML 檔本身有問題。problems 是全部問題的清單。"""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("初始資料檔有問題：\n" + "\n".join(f"- {p}" for p in problems))
        self.problems = problems


class SeedConflict(Exception):
    """資料庫既有資料與 YAML 的結構性欄位衝突，整批中止。"""

    def __init__(self, conflicts: list[str]) -> None:
        super().__init__("與資料庫既有資料衝突，未寫入任何資料：\n" + "\n".join(conflicts))
        self.conflicts = conflicts


# --- 檔案內容 -------------------------------------------------------------------


@dataclass(frozen=True)
class CardSeed:
    closing_day: int
    due_day: int
    autopay: str | None
    credit_limit: int | None


@dataclass(frozen=True)
class AccountSeed:
    code: str
    name: str
    account_class: str
    parent_code: str | None
    subtype: str | None
    system_key: str | None
    tax_category: str | None
    show_on_home: bool
    sort_order: int
    is_leaf: bool
    credit_card: CardSeed | None


@dataclass(frozen=True)
class CurrencySeed:
    code: str
    name: str
    minor_unit: int


@dataclass(frozen=True)
class PaymentMethodSeed:
    name: str
    account_code: str
    is_default: bool
    sort_order: int


@dataclass(frozen=True)
class SeedData:
    accounts: list[AccountSeed]  # 上層一定排在子科目之前
    currencies: list[CurrencySeed]
    payment_methods: list[PaymentMethodSeed]
    settings: dict[str, str]


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _unknown_keys(where: str, data: dict, allowed: set[str], problems: list[str]) -> None:
    extra = sorted(set(data) - allowed)
    if extra:
        problems.append(f"{where}：不認識的欄位 {', '.join(extra)}")


def _parse_accounts(raw: Any, problems: list[str]) -> list[AccountSeed]:
    result: list[AccountSeed] = []
    seen_codes: set[str] = set()

    def walk(items: Any, parent: AccountSeed | None, where: str) -> None:
        if not isinstance(items, list):
            problems.append(f"{where}：必須是清單")
            return
        sibling_names: set[str] = set()
        for index, item in enumerate(items, start=1):
            if not isinstance(item, dict):
                problems.append(f"{where} 第 {index} 筆：必須是物件")
                continue
            code = item.get("code")
            label = f"科目 {code}" if code is not None else f"{where} 第 {index} 筆"
            _unknown_keys(label, item, ACCOUNT_KEYS, problems)
            if not isinstance(code, str) or not code.strip():
                problems.append(f'{label}：code 必須是字串（YAML 請加引號，例如 "1101"）')
                continue
            code = code.strip()
            if code in seen_codes:
                problems.append(f"{label}：代碼重複")
                continue
            seen_codes.add(code)

            name = item.get("name")
            if not isinstance(name, str) or not name.strip():
                problems.append(f"{label}：name 必填")
                continue
            name = name.strip()
            if name in sibling_names:
                problems.append(f"{label}：同一層已經有名為「{name}」的科目（S9）")
            sibling_names.add(name)

            account_class = item.get("class")
            if parent is not None:
                if account_class is not None and account_class != parent.account_class:
                    problems.append(f"{label}：五大類必須與上層 {parent.code} 相同（M02）")
                account_class = parent.account_class
            try:
                account_class = check_account_class(account_class)
                check_subtype(account_class, item.get("subtype"))
                check_tax_category(item.get("tax_category"))
            except RuleViolation as exc:
                problems.append(f"{label}：{exc.message}")
                continue

            children = item.get("children")
            is_leaf = not children
            if item.get("tax_category") is not None and not is_leaf:
                problems.append(f"{label}：只有最末層科目可以設定稅務分類（S10）")

            system_key = item.get("system_key")
            if system_key is not None:
                if system_key not in set(SystemAccountKey):
                    problems.append(f"{label}：不認識的 system_key {system_key}")
                elif account_class != AccountClass.EQUITY or not is_leaf:
                    problems.append(f"{label}：系統科目必須是最末層的權益科目")

            card = _parse_card(label, item, problems)

            seed = AccountSeed(
                code=code,
                name=name,
                account_class=account_class,
                parent_code=parent.code if parent else None,
                subtype=item.get("subtype"),
                system_key=system_key,
                tax_category=item.get("tax_category"),
                show_on_home=bool(item.get("show_on_home", False)),
                sort_order=item.get("sort_order", index),
                is_leaf=is_leaf,
                credit_card=card,
            )
            if not _is_int(seed.sort_order):
                problems.append(f"{label}：sort_order 必須是整數")
            result.append(seed)
            if children:
                walk(children, seed, f"科目 {code} 的 children")

    walk(raw, None, "accounts")

    for key in SystemAccountKey:
        count = sum(1 for a in result if a.system_key == key)
        if count != 1:
            problems.append(f"system_key {key} 必須恰好出現一次（目前 {count} 次）")
    return result


def _parse_card(label: str, item: dict, problems: list[str]) -> CardSeed | None:
    raw = item.get("credit_card")
    if raw is None:
        return None  # 信用卡帳戶可以先不提供設定（S1-a）
    if item.get("subtype") != AccountSubtype.CREDIT_CARD:
        problems.append(f"{label}：只有信用卡帳戶可以有 credit_card 區塊")
        return None
    if not isinstance(raw, dict):
        problems.append(f"{label}：credit_card 必須是物件")
        return None
    _unknown_keys(f"{label} 的 credit_card", raw, CARD_KEYS, problems)
    ok = True
    for key, what in (("closing_day", "結帳日"), ("due_day", "繳款日")):
        value = raw.get(key)
        if not _is_int(value) or not 1 <= value <= 31:
            problems.append(
                f"{label}：credit_card.{key}（{what}）必填，且必須是 1 到 31 的整數（M07）"
            )
            ok = False
    autopay = raw.get("autopay")
    if autopay is not None and not isinstance(autopay, str):
        problems.append(f"{label}：credit_card.autopay 必須是科目代碼字串")
        ok = False
    credit_limit = raw.get("credit_limit")
    if credit_limit is not None and (not _is_int(credit_limit) or credit_limit <= 0):
        problems.append(f"{label}：credit_card.credit_limit 必須是大於 0 的整數")
        ok = False
    if not ok:
        return None
    return CardSeed(raw["closing_day"], raw["due_day"], autopay, credit_limit)


def _parse_currencies(raw: Any, problems: list[str]) -> list[CurrencySeed]:
    result: list[CurrencySeed] = []
    if not isinstance(raw, list):
        problems.append("currencies：必須是清單")
        return result
    seen: set[str] = set()
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            problems.append(f"currencies 第 {index} 筆：必須是物件")
            continue
        _unknown_keys(f"幣別 {item.get('code')}", item, CURRENCY_KEYS, problems)
        code, name, minor = item.get("code"), item.get("name"), item.get("minor_unit")
        if not isinstance(code, str) or len(code) != 3 or not code.isalpha() or not code.isupper():
            problems.append(f"currencies 第 {index} 筆：code 必須是 3 個大寫英文字母（ISO 4217）")
            continue
        if code in seen:
            problems.append(f"幣別 {code}：重複")
            continue
        seen.add(code)
        if not isinstance(name, str) or not name.strip():
            problems.append(f"幣別 {code}：name 必填")
            continue
        if not _is_int(minor) or minor < 0:
            problems.append(f"幣別 {code}：minor_unit 必須是 0 以上的整數")
            continue
        result.append(CurrencySeed(code, name.strip(), minor))
    return result


def _parse_payment_methods(
    raw: Any, accounts: list[AccountSeed], problems: list[str]
) -> list[PaymentMethodSeed]:
    result: list[PaymentMethodSeed] = []
    if not isinstance(raw, list):
        problems.append("payment_methods：必須是清單")
        return result
    by_code = {a.code: a for a in accounts}
    seen: set[str] = set()
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            problems.append(f"payment_methods 第 {index} 筆：必須是物件")
            continue
        name = item.get("name")
        _unknown_keys(f"付款方式 {name}", item, PM_KEYS, problems)
        if not isinstance(name, str) or not name.strip():
            problems.append(f"payment_methods 第 {index} 筆：name 必填")
            continue
        name = name.strip()
        if name in seen:
            problems.append(f"付款方式「{name}」：重複")
            continue
        seen.add(name)
        code = item.get("account")
        account = by_code.get(code) if isinstance(code, str) else None
        if account is None:
            problems.append(f"付款方式「{name}」：account 必須是本檔中的科目代碼字串")
            continue
        if account.account_class not in BALANCE_SHEET_CLASSES or not account.is_leaf:
            problems.append(f"付款方式「{name}」：扣款帳戶必須是最末層的資產或負債科目")
            continue
        sort_order = item.get("sort_order", index)
        if not _is_int(sort_order):
            problems.append(f"付款方式「{name}」：sort_order 必須是整數")
            continue
        result.append(PaymentMethodSeed(name, code, bool(item.get("default", False)), sort_order))
    defaults = [pm.name for pm in result if pm.is_default]
    if len(defaults) > 1:
        problems.append(f"預設付款方式只能有一個，目前有：{'、'.join(defaults)}（M08）")
    return result


def _parse_settings(raw: Any, problems: list[str]) -> dict[str, str]:
    if not isinstance(raw, dict):
        problems.append("settings：必須是物件")
        return {}
    result: dict[str, str] = {}
    for key, value in raw.items():
        if key not in KNOWN_SETTINGS:
            problems.append(
                f"settings：不認識的設定 {key}（預設付款方式改由 payment_methods 指定）"
            )
            continue
        if not _is_int(value) or value <= 0:
            problems.append(f"settings.{key}：必須是正整數")
            continue
        result[key] = str(value)
    return result


def parse_seed(raw: Any) -> SeedData:
    problems: list[str] = []
    if not isinstance(raw, dict):
        raise SeedFileError(
            ["檔案內容必須是物件（accounts、currencies、payment_methods、settings）"]
        )
    _unknown_keys("檔案", raw, TOP_LEVEL_KEYS, problems)
    accounts = _parse_accounts(raw.get("accounts"), problems)
    by_code = {a.code: a for a in accounts}
    for account in accounts:
        card = account.credit_card
        if card and card.autopay is not None:
            autopay = by_code.get(card.autopay)
            if autopay is None or autopay.subtype != AccountSubtype.BANK or not autopay.is_leaf:
                problems.append(
                    f"科目 {account.code}：扣繳帳戶 {card.autopay} "
                    "必須是本檔中最末層的 BANK 帳戶（M07）"
                )
    currencies = _parse_currencies(raw.get("currencies", []), problems)
    payment_methods = _parse_payment_methods(raw.get("payment_methods", []), accounts, problems)
    settings = _parse_settings(raw.get("settings", {}), problems)
    if problems:
        raise SeedFileError(problems)
    return SeedData(accounts, currencies, payment_methods, settings)


def load_seed_file(path: Path | str = DEFAULT_SEED_FILE) -> SeedData:
    try:
        with open(path, encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
    except FileNotFoundError as exc:
        raise SeedFileError([f"找不到初始資料檔：{path}"]) from exc
    except yaml.YAMLError as exc:
        raise SeedFileError([f"YAML 格式錯誤：{exc}"]) from exc
    return parse_seed(raw)


# --- 與資料庫比對 ---------------------------------------------------------------


@dataclass
class SeedReport:
    """每一類資料的處理結果。

    created、unchanged 是名稱清單；skipped 是「與 YAML 不同而保留資料庫值」的說明。
    """

    created: dict[str, list[str]] = field(default_factory=dict)
    unchanged: dict[str, list[str]] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)

    def add(self, bucket: dict[str, list[str]], kind: str, label: str) -> None:
        bucket.setdefault(kind, []).append(label)

    def count(self, bucket: dict[str, list[str]], kind: str) -> int:
        return len(bucket.get(kind, []))

    def lines(self) -> list[str]:
        kinds = ("科目", "信用卡設定", "幣別", "付款方式", "系統設定")
        out = [
            f"{kind}：新增 {self.count(self.created, kind)}、"
            f"已存在 {self.count(self.unchanged, kind)}"
            for kind in kinds
        ]
        out.append(f"與 YAML 不同而保留資料庫值：{len(self.skipped)}")
        out.extend(f"  - {line}" for line in self.skipped)
        return out


@dataclass
class SeedPlan:
    data: SeedData
    report: SeedReport
    new_accounts: list[AccountSeed]
    new_cards: list[AccountSeed]
    new_currencies: list[CurrencySeed]
    new_payment_methods: list[PaymentMethodSeed]
    new_settings: dict[str, str]
    keep_existing_default: bool


def plan_seed(db: Session, data: SeedData) -> SeedPlan:
    report = SeedReport()
    conflicts: list[str] = []

    new_accounts: list[AccountSeed] = []
    new_cards: list[AccountSeed] = []
    for seed in data.accounts:
        existing = account_repo.get_by_code(db, seed.code)
        if existing is None:
            _plan_new_account(db, seed, conflicts)
            new_accounts.append(seed)
            report.add(report.created, "科目", f"{seed.code} {seed.name}")
        else:
            _compare_account(db, seed, existing, conflicts, report)
            report.add(report.unchanged, "科目", f"{seed.code} {existing.name}")
        if seed.credit_card is not None:
            profile = db.get(CreditCardProfile, existing.id) if existing is not None else None
            if profile is None:
                new_cards.append(seed)
                report.add(report.created, "信用卡設定", seed.code)
            else:
                report.add(report.unchanged, "信用卡設定", seed.code)
                if (profile.closing_day, profile.due_day) != (
                    seed.credit_card.closing_day,
                    seed.credit_card.due_day,
                ):
                    report.skipped.append(f"信用卡 {seed.code}：結帳日／繳款日與 YAML 不同")
            _plan_autopay(db, seed, conflicts)

    new_currencies: list[CurrencySeed] = []
    for cur in data.currencies:
        existing_cur = db.get(Currency, cur.code)
        if existing_cur is None:
            new_currencies.append(cur)
            report.add(report.created, "幣別", cur.code)
            continue
        report.add(report.unchanged, "幣別", cur.code)
        if existing_cur.minor_unit != cur.minor_unit:
            conflicts.append(
                f"幣別 {cur.code}：minor_unit 資料庫為 {existing_cur.minor_unit}，"
                f"YAML 為 {cur.minor_unit}"
            )
        if existing_cur.name != cur.name:
            report.skipped.append(f"幣別 {cur.code}：名稱「{existing_cur.name}」與 YAML 不同")

    new_pms: list[PaymentMethodSeed] = []
    for pm in data.payment_methods:
        existing_pm = pm_repo.get_by_name(db, pm.name)
        if existing_pm is None:
            _plan_new_payment_method(db, pm, conflicts)
            new_pms.append(pm)
            report.add(report.created, "付款方式", pm.name)
            continue
        report.add(report.unchanged, "付款方式", pm.name)
        account = account_repo.get(db, existing_pm.account_id)
        if account is not None and account.code != pm.account_code:
            report.skipped.append(f"付款方式「{pm.name}」：扣款帳戶與 YAML 不同")
        if existing_pm.is_default != pm.is_default:
            report.skipped.append(f"付款方式「{pm.name}」：預設設定與 YAML 不同")
    current_default = pm_repo.get_default(db)
    keep_existing_default = current_default is not None
    if keep_existing_default and any(pm.is_default for pm in new_pms):
        report.skipped.append(
            f"資料庫已有預設付款方式「{current_default.name}」，新增的付款方式不設為預設"
        )

    new_settings: dict[str, str] = {}
    for key, value in data.settings.items():
        existing_setting = db.get(Setting, key)
        if existing_setting is None:
            new_settings[key] = value
            report.add(report.created, "系統設定", key)
        else:
            report.add(report.unchanged, "系統設定", key)
            if existing_setting.value != value:
                report.skipped.append(f"系統設定 {key}：資料庫為 {existing_setting.value}")

    if conflicts:
        raise SeedConflict(conflicts)
    return SeedPlan(
        data,
        report,
        new_accounts,
        new_cards,
        new_currencies,
        new_pms,
        new_settings,
        keep_existing_default,
    )


def _plan_new_account(db: Session, seed: AccountSeed, conflicts: list[str]) -> None:
    label = f"科目 {seed.code}"
    if seed.system_key is not None:
        holder = account_repo.get_by_system_key(db, seed.system_key)
        if holder is not None:
            conflicts.append(f"{label}：system_key {seed.system_key} 已屬於科目 {holder.code}")
    if seed.parent_code is None:
        parent_id = None
    else:
        parent = account_repo.get_by_code(db, seed.parent_code)
        if parent is None:
            return  # 上層也是這次新增的，名稱重複等已在檔案檢查處理
        try:
            check_can_add_child(db, parent)
        except RuleViolation as exc:
            conflicts.append(f"{label}：{exc.message}")
        parent_id = parent.id
    if account_repo.sibling_name_taken(db, parent_id, seed.name):
        conflicts.append(f"{label}：同一層已經有名為「{seed.name}」的科目（S9）")


def _compare_account(
    db: Session, seed: AccountSeed, existing: Account, conflicts: list[str], report: SeedReport
) -> None:
    label = f"科目 {seed.code}"
    parent_code = None
    if existing.parent_id is not None:
        parent = account_repo.get(db, existing.parent_id)
        parent_code = parent.code if parent else None
    structural = (
        ("account_class", existing.account_class, seed.account_class),
        ("subtype", existing.subtype, seed.subtype),
        ("system_key", existing.system_key, seed.system_key),
        ("上層科目", parent_code, seed.parent_code),
    )
    for what, db_value, yaml_value in structural:
        if db_value != yaml_value:
            conflicts.append(f"{label}：{what} 資料庫為 {db_value}，YAML 為 {yaml_value}")
    if seed.is_leaf and account_repo.has_children(db, existing.id):
        conflicts.append(f"{label}：YAML 中是最末層，但資料庫中已有子科目")
    maintained = (
        ("名稱", existing.name, seed.name),
        ("sort_order", existing.sort_order, seed.sort_order),
        ("show_on_home", existing.show_on_home, seed.show_on_home),
        ("is_active", existing.is_active, True),
        ("tax_category", existing.tax_category, seed.tax_category),
    )
    diffs = [what for what, db_value, yaml_value in maintained if db_value != yaml_value]
    if diffs:
        report.skipped.append(f"{label}：{'、'.join(diffs)}與 YAML 不同")


def _plan_autopay(db: Session, seed: AccountSeed, conflicts: list[str]) -> None:
    code = seed.credit_card.autopay if seed.credit_card else None
    if code is None:
        return
    existing = account_repo.get_by_code(db, code)
    if existing is not None and (
        not existing.is_active
        or existing.subtype != AccountSubtype.BANK
        or account_repo.has_children(db, existing.id)
    ):
        conflicts.append(
            f"信用卡 {seed.code}：扣繳帳戶 {code} 必須是啟用中、最末層的 BANK 帳戶（M07）"
        )


def _plan_new_payment_method(db: Session, pm: PaymentMethodSeed, conflicts: list[str]) -> None:
    existing = account_repo.get_by_code(db, pm.account_code)
    if existing is not None and (
        not existing.is_active or account_repo.has_children(db, existing.id)
    ):
        conflicts.append(f"付款方式「{pm.name}」：扣款帳戶 {pm.account_code} 已停用或不是最末層")


# --- 寫入 -----------------------------------------------------------------------


def apply_seed(db: Session, plan: SeedPlan) -> SeedReport:
    """在單一資料庫交易內寫入；任何錯誤都整批回滾。"""
    try:
        ids: dict[str, int] = {}

        def account_id(code: str) -> int:
            if code not in ids:
                ids[code] = account_repo.get_by_code(db, code).id
            return ids[code]

        for seed in plan.new_accounts:
            account = Account(
                code=seed.code,
                name=seed.name,
                account_class=seed.account_class,
                parent_id=account_id(seed.parent_code) if seed.parent_code else None,
                subtype=seed.subtype,
                system_key=seed.system_key,
                tax_category=seed.tax_category,
                show_on_home=seed.show_on_home,
                sort_order=seed.sort_order,
            )
            db.add(account)
            db.flush()
            ids[seed.code] = account.id

        for seed in plan.new_cards:
            card = seed.credit_card
            db.add(
                CreditCardProfile(
                    account_id=account_id(seed.code),
                    closing_day=card.closing_day,
                    due_day=card.due_day,
                    autopay_account_id=account_id(card.autopay) if card.autopay else None,
                    credit_limit=card.credit_limit,
                )
            )

        for cur in plan.new_currencies:
            db.add(Currency(code=cur.code, name=cur.name, minor_unit=cur.minor_unit))

        for pm in plan.new_payment_methods:
            db.add(
                PaymentMethod(
                    name=pm.name,
                    account_id=account_id(pm.account_code),
                    is_default=pm.is_default and not plan.keep_existing_default,
                    sort_order=pm.sort_order,
                )
            )

        for key, value in plan.new_settings.items():
            db.add(Setting(key=key, value=value))

        db.commit()
    except Exception:
        db.rollback()
        raise
    return plan.report


def seed_master_data(
    db: Session, path: Path | str = DEFAULT_SEED_FILE, dry_run: bool = False
) -> SeedReport:
    """讀檔 → 比對 → 寫入。dry_run=True 時只回報結果，不寫入。"""
    plan = plan_seed(db, load_seed_file(path))
    if dry_run:
        return plan.report
    return apply_seed(db, plan)
