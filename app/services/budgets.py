"""月預算（規格 6.9、8.1、F20、F21、M05；P3-3A）。只有 schema 與 service，沒有畫面。

預算只存「金額」與「從哪個月起生效」；已用金額與使用率從來不儲存，一律即時計算。

已用（used）
- 指定月份的整個 calendar month（月初到月底，含頭尾），該第一層費用科目與所有子孫科目的
  有效分錄加總（active_postings()：POSTED 且未刪除；DRAFT、已刪除排除）。
- 不依交易類型篩選：手續費、利息、薪資扣款、手動分錄，只要實際落在該科目底下就計入。
- 排除 exclude_from_budget = 1 的交易（ledger repository 的 budget_only）。
- 沒有「截至今天」的截止：日期落在該月的 POSTED 未來交易也計入該月。
- 退款貸記原費用科目，依分錄正負號自然沖減；used 可以是負數，不 clamp。

生效版本
- 指定月份採用同科目 effective_from ≤ 該月的最新一筆；比第一筆還早的月份沒有預算。
- 調整預算 = 新增較新的 effective_from；同一科目同一月份已有版本時，原地更新金額。
- effective_from 不得早於目前月份：V1 為了避免歷史月報被事後改寫而加的產品限制，
  不是資料庫限制，也不是 schema 原本的要求（docs/decisions/p3.md）。
- V1 不支援取消預算（沒有結束月、金額不能為 0、沒有 delete）。

使用率與狀態
- usage_pct = floor(max(used, 0) × 100 / budget)，used 為負時顯示 0。
- 狀態用整數交叉乘法判斷，不用四捨五入後的百分比：
  OVER：used ≥ budget（100% 固定）；WARN：used × 100 ≥ alert_pct × budget；否則 OK。
- budget_alert_pct 來自 settings（預設 80，有效範圍 1–99）；存在但不合法就 InvalidSetting。

V14（跨過 80%／100%；P3-3C）
- crossing() 是純函式（P3-3A）；crossings_for_change() 負責算「這次寫入」讓哪些預算跨過門檻。
- 預算 key = （第一層費用科目, 交易日期所在月份）；用該月自己的生效版本，沒有 today 截止。
- contribution = 該交易落在該第一層科目子樹的分錄淨額（不依 txn_type；exclude_from_budget → 沒有）。
- before = 寫入前 DB 的 used；after = before − 舊 contribution + 新 contribution：
  新增（舊=無）、修改（舊→新）、還原（舊=無；已刪除的不在 used 裡）、刪除（新=無）。
- after 沒有比 before 高（delta ≤ 0）就不可能跨過，直接略過，也不讀 budget_alert_pct。

畫面用的資料（P3-3B）
- BudgetCard：把 BudgetProgress 加上「顯示狀態」（ok／warn／used_up／over）與超支金額，
  /budgets 與首頁共用，template 不必自己判斷預算語意。
- settings_rows()：/budgets 設定頁的資料（目前生效版本、已排定的未來版本、本月進度）。
"""

import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from sqlalchemy.orm import Session

from app import clock
from app.models import Account, Budget, Setting, Transaction
from app.models.enums import AccountClass
from app.repositories import accounts as account_repo
from app.repositories import budgets as budget_repo
from app.repositories import ledger as ledger_repo
from app.services.accounts import get_account
from app.services.drafts import EntryDraft, ExistingTransaction
from app.services.errors import InvalidSetting, RuleViolation
from app.services.ledger_queries import subtree_ids

ALERT_PCT_SETTING = "budget_alert_pct"
DEFAULT_ALERT_PCT = 80  # 規格 6.10 的初始值；只在 settings 完全沒有這筆時使用
FULL_PCT = 100  # 超支門檻固定，不可設定
ALERT_PCT_RANGE = (1, 99)

_MONTH = re.compile(r"([0-9]{4})-(0[1-9]|1[0-2])")


class BudgetStatus(StrEnum):
    OK = "OK"
    WARN = "WARN"  # used ≥ alert_pct%
    OVER = "OVER"  # used ≥ 100%


@dataclass(frozen=True)
class BudgetProgress:
    account_id: int
    account_name: str
    is_active: bool
    month: str  # YYYY-MM
    budget: int  # 該月採用的版本金額
    effective_from: str  # 採用的是哪一個版本
    used: int  # 可以是負數（退款大於當月消費）
    remaining: int  # budget − used；超支為負
    usage_pct: int  # floor(max(used, 0) × 100 / budget)
    status: BudgetStatus


# --- 月份 ------------------------------------------------------------------------------


def month_of(day: date) -> str:
    return f"{day.year:04d}-{day.month:02d}"


def current_month() -> str:
    return month_of(clock.today())


def month_range(month: str) -> tuple[date, date]:
    """'YYYY-MM' → (月初, 月底)，含頭尾。格式不對丟 RuleViolation（BUDGET_MONTH）。"""
    match = _MONTH.fullmatch(month) if isinstance(month, str) else None
    if match is None:
        raise RuleViolation("BUDGET_MONTH", "月份格式必須是 YYYY-MM")
    year, mon = int(match.group(1)), int(match.group(2))
    first = date(year, mon, 1)
    nxt = date(year + (mon == 12), mon % 12 + 1, 1)
    return first, date.fromordinal(nxt.toordinal() - 1)


# --- 設定 ------------------------------------------------------------------------------


def budget_alert_pct(db: Session) -> int:
    """V14／F21 的提醒百分比。設定不存在 → 80；存在但不是 1–99 的整數 → InvalidSetting。"""
    setting = db.get(Setting, ALERT_PCT_SETTING)
    if setting is None:
        return DEFAULT_ALERT_PCT
    low, high = ALERT_PCT_RANGE
    if (
        not re.fullmatch(r"[0-9]{1,3}", setting.value or "")
        or not low <= int(setting.value) <= high
    ):
        raise InvalidSetting(ALERT_PCT_SETTING, setting.value, f"{low} 到 {high} 的整數")
    return int(setting.value)


# --- 純函式：使用率、狀態、跨過門檻 ----------------------------------------------------------


def usage_pct(used: int, budget: int) -> int:
    return max(used, 0) * 100 // budget


def classify(used: int, budget: int, alert_pct: int) -> BudgetStatus:
    if used * 100 >= FULL_PCT * budget:
        return BudgetStatus.OVER
    if used * 100 >= alert_pct * budget:
        return BudgetStatus.WARN
    return BudgetStatus.OK


def crossing(before: int, after: int, budget: int, alert_pct: int) -> int | None:
    """這次變動讓使用率「跨過」哪個門檻（V14）；沒有跨過回傳 None。

    跨過門檻 T：變動前 < T% 且變動後 ≥ T%（剛好到達也算）。
    同時跨過兩個門檻時只回報較高的（100）。已經高於門檻後再增加、或用量減少，都不算跨過。
    """
    for threshold in (FULL_PCT, alert_pct):
        if before * 100 < threshold * budget <= after * 100:
            return threshold
    return None


# --- 設定預算 --------------------------------------------------------------------------


def set_budget(db: Session, account_id: int, effective_from: str, monthly_amount: int) -> Budget:
    """新增一個預算版本；同科目同月份已有版本時原地更新金額。

    M05：只能設在第一層（parent_id 為空）的費用科目。
    effective_from 不得早於目前月份（V1 產品限制，見模組說明）。
    """
    month_range(effective_from)
    if (
        isinstance(monthly_amount, bool)
        or not isinstance(monthly_amount, int)
        or monthly_amount <= 0
    ):
        raise RuleViolation("BUDGET_AMOUNT", "預算金額必須是大於 0 的整數")
    account = get_account(db, account_id)
    if account.account_class != AccountClass.EXPENSE or account.parent_id is not None:
        raise RuleViolation(
            "M05", f"「{account.name}」不是第一層費用科目，預算只能設在第一層費用科目"
        )
    if not account.is_active:
        raise RuleViolation("INACTIVE", f"「{account.name}」已停用，不能設定預算")
    if effective_from < current_month():
        raise RuleViolation("BUDGET_PAST_MONTH", f"生效月份不能早於目前月份（{current_month()}）")

    budget = budget_repo.get(db, account.id, effective_from)
    if budget is None:
        budget = Budget(
            account_id=account.id, effective_from=effective_from, monthly_amount=monthly_amount
        )
        db.add(budget)
    else:
        budget.monthly_amount = monthly_amount
    db.commit()
    return budget


# --- 進度 ------------------------------------------------------------------------------


def _latest_per_account(rows: Iterable[Budget]) -> dict[int, Budget]:
    """rows 已依 (account_id, effective_from) 由舊到新；後面的蓋掉前面的。"""
    chosen: dict[int, Budget] = {}
    for row in rows:
        chosen[row.account_id] = row
    return chosen


def _progress(
    db: Session, account: Account, budget: Budget, month: str, first: date, last: date, alert: int
) -> BudgetProgress:
    used = ledger_repo.active_sum_for_accounts(
        db, subtree_ids(db, account.id), first, last, budget_only=True
    )  # 費用科目的正常方向是借方（+），不需要翻號
    return BudgetProgress(
        account_id=account.id,
        account_name=account.name,
        is_active=account.is_active,
        month=month,
        budget=budget.monthly_amount,
        effective_from=budget.effective_from,
        used=used,
        remaining=budget.monthly_amount - used,
        usage_pct=usage_pct(used, budget.monthly_amount),
        status=classify(used, budget.monthly_amount, alert),
    )


def account_progress(
    db: Session, account_id: int, month: str | None = None
) -> BudgetProgress | None:
    """單一科目在指定月份的預算進度；該月沒有生效的預算 → None。"""
    month = month or current_month()
    first, last = month_range(month)
    account = get_account(db, account_id)
    chosen = _latest_per_account(budget_repo.in_force_candidates(db, month, account.id))
    budget = chosen.get(account.id)
    if budget is None:
        return None
    return _progress(db, account, budget, month, first, last, budget_alert_pct(db))


def monthly_progress(db: Session, month: str | None = None) -> tuple[BudgetProgress, ...]:
    """指定月份（預設本月）所有有生效預算的第一層費用科目，依 (sort_order, code) 排序。

    停用的科目只要該月有生效的預算就照樣列出（is_active = False），不讓預算消失。
    """
    month = month or current_month()
    first, last = month_range(month)
    chosen = _latest_per_account(budget_repo.in_force_candidates(db, month))
    if not chosen:
        return ()
    alert = budget_alert_pct(db)
    accounts = [a for a in account_repo.list_all(db) if a.id in chosen]  # 已依 (sort_order, code)
    return tuple(_progress(db, a, chosen[a.id], month, first, last, alert) for a in accounts)


# --- 畫面用的資料（P3-3B）-----------------------------------------------------------------------


@dataclass(frozen=True)
class BudgetCard:
    """一個科目的預算進度卡（/budgets 與首頁共用）。只是顯示用的包裝，不重算任何金額。"""

    progress: BudgetProgress

    @property
    def state(self) -> str:
        """ok／warn／used_up（剛好 100%）／over（超支）。template 只比對這個字串。"""
        if self.progress.status == BudgetStatus.OVER:
            return "used_up" if self.progress.remaining == 0 else "over"
        return "warn" if self.progress.status == BudgetStatus.WARN else "ok"

    @property
    def month_label(self) -> str:
        """不是目前月份時才有值（例如未來日期的交易），畫面照印，不做判斷。"""
        return "" if self.progress.month == current_month() else self.progress.month

    @property
    def over_amount(self) -> int:
        return max(-self.progress.remaining, 0)


@dataclass(frozen=True)
class BudgetVersion:
    effective_from: str  # YYYY-MM
    monthly_amount: int


@dataclass(frozen=True)
class BudgetSettingsRow:
    account_id: int
    account_name: str
    is_active: bool
    current: BudgetVersion | None  # 本月生效的版本；None = 本月沒有預算
    scheduled: tuple[BudgetVersion, ...]  # 已排定、尚未生效的未來版本（由近到遠）
    card: BudgetCard | None  # 本月進度；沒有預算、或 budget_alert_pct 異常時為 None


@dataclass(frozen=True)
class BudgetSettings:
    month: str  # 目前月份（YYYY-MM）：生效月份欄位的預設值與 min
    rows: tuple[BudgetSettingsRow, ...]
    progress_error: str | None  # budget_alert_pct 不合法時的說明；進度不顯示，表單仍可使用


def budget_cards(db: Session, month: str | None = None) -> tuple[BudgetCard, ...]:
    """指定月份（預設本月）所有有生效預算的科目，沒有重新排序。"""
    return tuple(BudgetCard(p) for p in monthly_progress(db, month))


def settings_rows(db: Session, month: str | None = None) -> BudgetSettings:
    """/budgets 設定頁的資料。

    列出啟用中的第一層費用科目；停用的科目只有「本月有生效預算」才列出（唯讀，不因停用而消失）。
    budget_alert_pct 不合法時不 fallback：進度留空並回報錯誤，其餘（目前版本、未來版本）照常。
    """
    month = month or current_month()
    in_force = _latest_per_account(budget_repo.in_force_candidates(db, month))
    later: dict[int, list[BudgetVersion]] = defaultdict(list)
    for row in budget_repo.later_versions(db, month):
        later[row.account_id].append(BudgetVersion(row.effective_from, row.monthly_amount))
    cards: dict[int, BudgetCard] = {}
    error: str | None = None
    try:
        cards = {c.progress.account_id: c for c in budget_cards(db, month)}
    except InvalidSetting as exc:
        error = str(exc)
    rows = []
    for account in account_repo.list_all(db):  # 已依 (sort_order, code)
        if account.account_class != AccountClass.EXPENSE or account.parent_id is not None:
            continue
        current = in_force.get(account.id)
        if not account.is_active and current is None:
            continue
        rows.append(
            BudgetSettingsRow(
                account_id=account.id,
                account_name=account.name,
                is_active=account.is_active,
                current=(
                    BudgetVersion(current.effective_from, current.monthly_amount)
                    if current is not None
                    else None
                ),
                scheduled=tuple(later.get(account.id, ())),
                card=cards.get(account.id),
            )
        )
    return BudgetSettings(month=month, rows=tuple(rows), progress_error=error)


# --- V14：這次寫入讓哪些預算跨過門檻（P3-3C）---------------------------------------------


@dataclass(frozen=True)
class BudgetCrossing:
    account_id: int
    account_name: str
    month: str  # YYYY-MM，交易日期所在月份
    threshold: int  # 100，或 budget_alert_pct
    used: int  # 這次寫入之後的 used
    budget: int


@dataclass(frozen=True)
class BudgetChange:
    crossings: tuple[BudgetCrossing, ...] = ()
    config_error: bool = False  # budget_alert_pct 不合法，無法判斷（不 fallback）


def _expense_root(accounts: dict[int, Account], account_id: int | None) -> int | None:
    """科目所屬的第一層費用科目 id；不在費用科目底下回傳 None。"""
    account = accounts.get(account_id) if account_id is not None else None
    while account is not None and account.parent_id is not None:
        account = accounts.get(account.parent_id)
    if account is None or account.account_class != AccountClass.EXPENSE:
        return None
    return account.id


def _contributions(
    tx: EntryDraft | ExistingTransaction | None, accounts: dict[int, Account]
) -> dict[tuple[int, str], int]:
    """{(第一層費用科目 id, 月份): 分錄淨額}。exclude_from_budget 的交易沒有任何 contribution。"""
    if tx is None or tx.exclude_from_budget or tx.txn_date is None:
        return {}
    month = month_of(tx.txn_date)
    out: dict[tuple[int, str], int] = {}
    for posting in tx.postings:
        root = _expense_root(accounts, posting.account_id)
        if root is not None:
            out[(root, month)] = out.get((root, month), 0) + posting.amount
    return out


def crossings_for_change(
    db: Session,
    new: EntryDraft | ExistingTransaction | None,
    old: EntryDraft | ExistingTransaction | None,
) -> BudgetChange:
    """新增（old=None）、修改、還原（old=None）、刪除（new=None）讓哪些預算跨過 80%／100%。

    只讀。排序：跨過 100% 的在前，其次依科目順序（sort_order、code），最後依月份。
    """
    accounts_in_order = account_repo.list_all(db)
    accounts = {a.id: a for a in accounts_in_order}
    order = {a.id: i for i, a in enumerate(accounts_in_order)}
    after, before = _contributions(new, accounts), _contributions(old, accounts)

    crossings: list[BudgetCrossing] = []
    config_error = False
    alert: int | None = None
    for key in sorted(set(after) | set(before)):
        delta = after.get(key, 0) - before.get(key, 0)
        if delta <= 0:
            continue  # 沒有增加用量就不可能跨過門檻
        root_id, month = key
        version = _latest_per_account(budget_repo.in_force_candidates(db, month, root_id)).get(
            root_id
        )
        if version is None:
            continue
        if alert is None:
            try:
                alert = budget_alert_pct(db)
            except InvalidSetting:
                config_error = True  # 不 fallback 成 80；整筆只記一次
                break
        first, last = month_range(month)
        used = ledger_repo.active_sum_for_accounts(
            db, subtree_ids(db, root_id), first, last, budget_only=True
        )
        threshold = crossing(used, used + delta, version.monthly_amount, alert)
        if threshold is not None:
            crossings.append(
                BudgetCrossing(
                    root_id,
                    accounts[root_id].name,
                    month,
                    threshold,
                    used + delta,
                    version.monthly_amount,
                )
            )
    crossings.sort(key=lambda c: (c.threshold != FULL_PCT, order[c.account_id], c.month))
    return BudgetChange(tuple(crossings), config_error)


def cards_for_transaction(db: Session, txn_id: int) -> tuple[BudgetCard, ...]:
    """儲存後顯示：這筆交易「目前」計入的預算卡片（edit 之前的舊 key 不顯示）。

    排序同 V14 notices。budget_alert_pct 不合法時回傳空（設定異常由 notice 與 /budgets 說明）。
    """
    txn = db.get(Transaction, txn_id)
    if txn is None:
        return ()
    accounts_in_order = account_repo.list_all(db)
    accounts = {a.id: a for a in accounts_in_order}
    order = {a.id: i for i, a in enumerate(accounts_in_order)}
    keys = _contributions(ExistingTransaction.from_model(txn), accounts)
    cards: list[BudgetCard] = []
    try:
        for root_id, month in sorted(keys, key=lambda k: (order[k[0]], k[1])):
            progress = account_progress(db, root_id, month)
            if progress is not None:
                cards.append(BudgetCard(progress))
    except InvalidSetting:
        return ()
    return tuple(cards)
