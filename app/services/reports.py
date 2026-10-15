"""月報（規格 8.2、T16；P3-4）。只讀：所有數字即時計算，不儲存、不快取、沒有彙總表。

收入與支出（依科目大類，不依交易類型）
- 收入 = abs(class_total(INCOME))：規格 8.2「收入科目分錄加總的絕對值」，依 v1.0 規格字面實作。
  一般月份與 class_total 的正方向結果相同；收入被沖銷成淨負值的月份仍顯示絕對值。
- 支出 = class_total(EXPENSE)：含 exclude_from_budget = 1 的交易（「不計入預算」只影響預算進度）；
  退款貸記費用科目，依分錄正負號自然沖減，可以是負數，不 clamp。
- 轉帳、繳卡費只碰資產／負債，期初與帳務調整的對方是權益科目，所以天然不會混入收入或支出。
- 結餘 = 收入 − 支出；儲蓄率 = 結餘 ÷ 收入（收入 ≤ 0 → None）。全部在這裡算，template 只印。

各科目支出與預算對照
- 第一層費用科目：actual = period_total（含所有子孫科目，含不計入預算的交易）。
- 預算進度重用 budgets.budget_cards()（排除 exclude_from_budget）。
  excluded_amount = actual − budget.used，保留正負號：不計入預算的支出 → 正，退款 → 負。
- 第二層只列直接 child（金額已含更深的子孫）；第一層自己直接記帳的部分（正常情況為 0）
  以 own_amount 補上，讓「各項加總 = 第一層合計」永遠成立。

帳戶與淨資產
- 列出所有資產、負債的末層科目（不只 show_on_home）；月初 = 月初前一天的餘額，月底 = 月底餘額。
- 淨資產直接呼叫 ledger_queries.net_worth()，不是把列加起來（沒有 parent + child 重複的問題）。

日期：指定月份的整個 calendar month（含頭尾），沒有 today 截止，與預算（P3-3A）同一個月界。
所以看目前月份時，月底餘額會包含本月稍後日期的 POSTED 交易（end_includes_future）。
deleted／DRAFT 的排除全部由 ledger_queries／budgets 底層的 active_postings() 負責。
"""

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy.orm import Session

from app import clock
from app.models.enums import AccountClass
from app.repositories import accounts as account_repo
from app.services import budgets
from app.services.errors import InvalidSetting, RuleViolation
from app.services.ledger_queries import (
    adjustment_total,
    balance,
    class_total,
    net_worth,
    period_total,
)

BALANCE_CLASSES = (AccountClass.ASSET, AccountClass.LIABILITY)
CLASS_LABELS = {AccountClass.ASSET: "資產", AccountClass.LIABILITY: "負債"}
RATE_STEP = Decimal("0.1")


@dataclass(frozen=True)
class ExpenseChild:
    account_id: int
    name: str
    is_active: bool
    amount: int  # 含更深的子孫科目


@dataclass(frozen=True)
class ExpenseRow:
    account_id: int
    name: str
    is_active: bool
    actual: int  # 含 exclude_from_budget 的交易
    children: tuple[ExpenseChild, ...]  # 直接 child，金額不為 0 的才列
    own_amount: int  # 直接記在第一層科目本身的金額（正常為 0）
    budget: budgets.BudgetCard | None  # 該月沒有生效預算（或預算設定異常）→ None
    excluded_amount: int  # actual − budget.used，有正負號；沒有預算 → 0


@dataclass(frozen=True)
class AccountBalanceRow:
    account_id: int
    name: str
    account_class: str  # ASSET／LIABILITY
    is_active: bool
    start: int  # 月初（上月底）餘額，正常餘額方向
    end: int  # 月底餘額，正常餘額方向

    @property
    def class_label(self) -> str:
        return CLASS_LABELS[self.account_class]

    @property
    def start_is_debt(self) -> bool:
        return self.account_class == AccountClass.LIABILITY and self.start > 0

    @property
    def end_is_debt(self) -> bool:
        return self.account_class == AccountClass.LIABILITY and self.end > 0


@dataclass(frozen=True)
class MonthlyReport:
    month: str  # YYYY-MM
    first_day: date
    last_day: date
    prev_month: str | None
    next_month: str | None
    income: int
    expense: int
    balance: int
    saving_rate: Decimal | None  # 百分比，一位小數；收入 ≤ 0 → None
    adjustment_total: int
    expense_rows: tuple[ExpenseRow, ...]
    budget_error: str | None  # budget_alert_pct 不合法：預算欄顯示錯誤，其他部分照常
    accounts: tuple[AccountBalanceRow, ...]
    net_worth_start: int
    net_worth_end: int
    end_includes_future: bool  # 月底晚於今天：月底餘額含稍後日期的交易


def _bounds(month: str) -> tuple[date, date]:
    """YYYY-MM → (月初, 月底)。只驗格式（4 位數年、月 01–12），不另加年份限制。

    date 本身撐不住的邊界（0000 年、9999-12，以及月初前一天不存在的 0001-01）也當成無效月份。
    """
    try:
        first, last = budgets.month_range(month)
        _ = (first - timedelta(days=1), last + timedelta(days=1))  # 前後一天都要存在
    except (RuleViolation, ValueError, OverflowError):
        raise RuleViolation(
            "REPORT_MONTH", "月份格式必須是 YYYY-MM（年份 4 位數、月份 01–12）"
        ) from None
    return first, last


def _neighbor(day: date) -> str | None:
    month = budgets.month_of(day)
    try:
        _bounds(month)
    except RuleViolation:
        return None
    return month


def saving_rate(income: int, balance_amount: int) -> Decimal | None:
    """結餘 ÷ 收入，百分比，一位小數、四捨五入（half up）。收入 ≤ 0 → None；可以是負數。"""
    if income <= 0:
        return None
    rate = (Decimal(balance_amount) * 100 / Decimal(income)).quantize(RATE_STEP, ROUND_HALF_UP)
    return rate if rate != 0 else Decimal("0.0")  # 不要出現「-0.0%」


def monthly_report(db: Session, month: str | None = None) -> MonthlyReport:
    """指定月份（預設本月）的月報。月份格式不對 → RuleViolation（REPORT_MONTH）。"""
    month = month or budgets.current_month()
    first, last = _bounds(month)
    before = first - timedelta(days=1)

    income = abs(class_total(db, AccountClass.INCOME, first, last))
    expense = class_total(db, AccountClass.EXPENSE, first, last)
    balance_amount = income - expense

    budget_cards: dict[int, budgets.BudgetCard] = {}
    budget_error: str | None = None
    try:
        budget_cards = {c.progress.account_id: c for c in budgets.budget_cards(db, month)}
    except InvalidSetting as exc:
        budget_error = str(exc)

    accounts = account_repo.list_all(db)  # 已依 (sort_order, code)
    children_of: dict[int, list] = {}
    for account in accounts:
        if account.parent_id is not None:
            children_of.setdefault(account.parent_id, []).append(account)

    return MonthlyReport(
        month=month,
        first_day=first,
        last_day=last,
        prev_month=_neighbor(before),
        next_month=_neighbor(last + timedelta(days=1)),
        income=income,
        expense=expense,
        balance=balance_amount,
        saving_rate=saving_rate(income, balance_amount),
        adjustment_total=adjustment_total(db, first, last),
        expense_rows=_expense_rows(db, accounts, children_of, budget_cards, first, last),
        budget_error=budget_error,
        accounts=_balance_rows(db, accounts, children_of, before, last),
        net_worth_start=net_worth(db, before),
        net_worth_end=net_worth(db, last),
        end_includes_future=last > clock.today(),
    )


def _expense_rows(db, accounts, children_of, budget_cards, first, last) -> tuple[ExpenseRow, ...]:
    rows = []
    for account in accounts:
        if account.account_class != AccountClass.EXPENSE or account.parent_id is not None:
            continue
        actual = period_total(db, account.id, first, last)
        child_totals = [
            (child, period_total(db, child.id, first, last))
            for child in children_of.get(account.id, ())
        ]
        children = tuple(
            ExpenseChild(child.id, child.name, child.is_active, amount)
            for child, amount in child_totals
            if amount != 0
        )
        own_amount = actual - sum(amount for _, amount in child_totals)
        card = budget_cards.get(account.id)
        # 有預算、或有任何金額才列；實際為 0 又沒有預算的科目省略
        if not (actual != 0 or card is not None or children or own_amount != 0):
            continue
        rows.append(
            ExpenseRow(
                account_id=account.id,
                name=account.name,
                is_active=account.is_active,
                actual=actual,
                children=children,
                own_amount=own_amount,
                budget=card,
                excluded_amount=actual - card.progress.used if card is not None else 0,
            )
        )
    return tuple(rows)


def _balance_rows(db, accounts, children_of, before, last) -> tuple[AccountBalanceRow, ...]:
    rows = []
    for account_class in BALANCE_CLASSES:  # 資產在前、負債在後；同類依 (sort_order, code)
        for account in accounts:
            if account.account_class != account_class or account.id in children_of:
                continue  # 只列末層：父科目的餘額含子孫，列出來會重複
            start = balance(db, account.id, before)
            end = balance(db, account.id, last)
            if account.is_active or start != 0 or end != 0:
                rows.append(
                    AccountBalanceRow(
                        account_id=account.id,
                        name=account.name,
                        account_class=account.account_class,
                        is_active=account.is_active,
                        start=start,
                        end=end,
                    )
                )
    return tuple(rows)
