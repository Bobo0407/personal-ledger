"""信用卡設定（規格 6.2、7.1 credit_cards.py、9.2 M07；P1 決策 S1、S1-a、S1-b）。

- 信用卡帳戶（subtype = CREDIT_CARD）可以先存在、暫時沒有設定，視為「尚未設定信用卡週期」；
  這段期間仍可正常記帳，因為分錄不依賴結帳日。
- M07 只約束「建立或修改設定」的那一刻：
  結帳日、繳款日必填（1–31）；扣繳帳戶若有設定，必須是啟用中、最末層的 BANK 資產帳戶。
- 帳單週期、本期應繳（P3-2A；規格 6.2、8.1、T18）：current_cycle()、amount_due()。
  沒有設定時回傳 None，不推測週期。語意與限制記在 docs/decisions/p3.md。
- 設定頁（P3-2B）：list_card_settings()、autopay_candidates()、update_card_settings()。
"""

import calendar
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy.orm import Session

from app import clock
from app.models import Account, CreditCardProfile
from app.models.enums import AccountClass, AccountSubtype, TxnType
from app.repositories import accounts as account_repo
from app.repositories import ledger as ledger_repo
from app.services.accounts import get_account
from app.services.errors import RuleViolation
from app.services.ledger_queries import balance, subtree_ids


def check_day(value: int | None, what: str) -> int:
    if value is None:
        raise RuleViolation("M07", f"信用卡必須設定{what}")
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 31:
        raise RuleViolation("M07", f"{what}必須是 1 到 31 的整數")
    return value


def get_profile(db: Session, card_account_id: int) -> CreditCardProfile | None:
    """None 代表「尚未設定信用卡週期」。"""
    return db.get(CreditCardProfile, card_account_id)


def set_profile(
    db: Session,
    card_account_id: int,
    *,
    closing_day: int | None,
    due_day: int | None,
    autopay_account_id: int | None = None,
    credit_limit: int | None = None,
) -> CreditCardProfile:
    """建立或修改信用卡設定。autopay_account_id、credit_limit 傳 None 代表不設定（或清除）。"""
    card = get_account(db, card_account_id)
    if card.subtype != AccountSubtype.CREDIT_CARD:
        raise RuleViolation("NOT_CREDIT_CARD", f"「{card.name}」不是信用卡帳戶")
    if not card.is_active:
        raise RuleViolation("INACTIVE", f"「{card.name}」已停用，不能修改信用卡設定")
    closing_day = check_day(closing_day, "結帳日")
    due_day = check_day(due_day, "繳款日")

    if autopay_account_id is not None:
        autopay = get_account(db, autopay_account_id)
        if (
            autopay.account_class != AccountClass.ASSET
            or autopay.subtype != AccountSubtype.BANK
            or not autopay.is_active
            or account_repo.has_children(db, autopay.id)
        ):
            raise RuleViolation("M07", "扣繳帳戶必須是啟用中、最末層的銀行（BANK）帳戶")

    if credit_limit is not None and (
        isinstance(credit_limit, bool) or not isinstance(credit_limit, int) or credit_limit <= 0
    ):
        raise RuleViolation("CREDIT_LIMIT", "信用額度必須是大於 0 的整數")

    profile = get_profile(db, card.id)
    if profile is None:
        profile = CreditCardProfile(account_id=card.id, closing_day=closing_day, due_day=due_day)
        db.add(profile)
    profile.closing_day = closing_day
    profile.due_day = due_day
    profile.autopay_account_id = autopay_account_id
    profile.credit_limit = credit_limit
    db.commit()
    return profile


# --- 帳單週期與本期應繳（P3-2A）-------------------------------------------------------------


@dataclass(frozen=True)
class CardCycle:
    """最近已結帳、目前等待繳款的 statement cycle（不是正在累積中的下一期）。"""

    statement_start: date  # 上一個結帳日 + 1 天
    statement_end: date  # 最近一個不晚於今天的結帳日（含當天）
    due_date: date  # 繳款日：statement_end 之後第一個符合 due_day 的日期
    next_closing: date  # 下一個結帳日（statement_end 之後新刷的消費屬於這一期）
    days_until_due: int  # due_date − 今天；已過期為負數（P3-2A 沒有 overdue 旗標）


@dataclass(frozen=True)
class CardDue:
    cycle: CardCycle
    statement_balance: int  # 結帳日當天（含）的卡片餘額，欠款為正；卡片是貸方餘額時為負
    paid_since: int  # 結帳日之後到今天的 CARD_PAYMENT 合計
    amount_due: int  # max(statement_balance − paid_since, 0)
    overpaid: int  # max(paid_since − statement_balance, 0)


def _clamped(year: int, month: int, day: int) -> date:
    """該月沒有這一天時取月底（規格 6.2：結帳日；繳款日採同一規則）。"""
    return date(year, month, min(day, calendar.monthrange(year, month)[1]))


def _shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    index = year * 12 + (month - 1) + delta
    return index // 12, index % 12 + 1


def closing_date_in_month(year: int, month: int, closing_day: int) -> date:
    """某年某月的結帳日；該月沒有這一天（例如 31）就取月底。"""
    return _clamped(year, month, closing_day)


def _closing_next_to(closing: date, closing_day: int, delta: int) -> date:
    """結帳日 closing 前一個（delta = −1）或後一個（delta = +1）結帳日。"""
    year, month = _shift_month(closing.year, closing.month, delta)
    return closing_date_in_month(year, month, closing_day)


def last_closing_on_or_before(today: date, closing_day: int) -> date:
    """最近一個不晚於 today 的結帳日。今天剛好是結帳日時就是今天（結帳日當天算本期）。"""
    this_month = closing_date_in_month(today.year, today.month, closing_day)
    if this_month <= today:
        return this_month
    year, month = _shift_month(today.year, today.month, -1)
    return closing_date_in_month(year, month, closing_day)


def due_date_after(closing: date, due_day: int) -> date:
    """結帳日 closing 之後第一個符合 due_day 的日期；該月沒有該日則取月底。

    必須嚴格晚於 closing：due_day 等於（或截斷後等於）結帳日時，落在下個月。
    """
    candidate = _clamped(closing.year, closing.month, due_day)
    if candidate > closing:
        return candidate
    year, month = _shift_month(closing.year, closing.month, 1)
    return _clamped(year, month, due_day)


def compute_cycle(closing_day: int, due_day: int, today: date) -> CardCycle:
    """純日期運算（不碰資料庫）。closing_day、due_day 的範圍由 M07 與資料庫 CHECK 保證。"""
    end = last_closing_on_or_before(today, closing_day)
    due = due_date_after(end, due_day)
    return CardCycle(
        statement_start=_closing_next_to(end, closing_day, -1) + timedelta(days=1),
        statement_end=end,
        due_date=due,
        next_closing=_closing_next_to(end, closing_day, +1),
        days_until_due=(due - today).days,
    )


def _card_profile(db: Session, card_account_id: int) -> tuple[int, CreditCardProfile | None]:
    """(信用卡科目 id, 設定)。不是信用卡 → RuleViolation；停用的卡仍可計算。"""
    card = get_account(db, card_account_id)
    if card.subtype != AccountSubtype.CREDIT_CARD:
        raise RuleViolation("NOT_CREDIT_CARD", f"「{card.name}」不是信用卡帳戶")
    return card.id, get_profile(db, card.id)


def current_cycle(db: Session, card_account_id: int, today: date | None = None) -> CardCycle | None:
    """最近已結帳、目前等待繳款的 statement cycle（規格 8.1 的「本期」）。

    不是正在累積中的下一期：結帳日之後新刷的消費屬於下一期，不在這個週期內。
    None = 尚未設定信用卡週期（不推測）。today 預設為台北的今天。
    """
    card_id, profile = _card_profile(db, card_account_id)
    if profile is None:
        return None
    return compute_cycle(profile.closing_day, profile.due_day, today or clock.today())


def amount_due(db: Session, card_account_id: int, today: date | None = None) -> CardDue | None:
    """本期應繳（規格 8.1、T18）。None = 尚未設定信用卡週期。

    statement_balance = 結帳日當天（含）的卡片餘額，來自 ledger_queries.balance()
        （active_postings()，欠款為正；已刪除、草稿與未來日期不計入）。
    paid_since = 結帳日之後（不含結帳日）到今天，該卡的 CARD_PAYMENT 分錄合計。
        只看 CARD_PAYMENT：結帳日後的 REFUND、ADJUSTMENT、MANUAL 都不當成繳款。
        結帳日當天或之前的繳款已經反映在 statement_balance，不會重複扣。
    amount_due = max(statement_balance − paid_since, 0)；
    overpaid = max(paid_since − statement_balance, 0)。

    V1 分期全額入帳，有分期消費時 amount_due 會偏高（規格 8.1），這裡不補償。
    """
    card_id, profile = _card_profile(db, card_account_id)
    if profile is None:
        return None
    today = today or clock.today()
    cycle = compute_cycle(profile.closing_day, profile.due_day, today)
    statement_balance = balance(db, card_id, cycle.statement_end)
    paid_since = ledger_repo.active_sum_for_accounts(
        db,
        subtree_ids(db, card_id),
        start=cycle.statement_end + timedelta(days=1),
        end=today,
        txn_type=TxnType.CARD_PAYMENT,
    )
    raw = statement_balance - paid_since
    return CardDue(
        cycle=cycle,
        statement_balance=statement_balance,
        paid_since=paid_since,
        amount_due=max(raw, 0),
        overpaid=max(-raw, 0),
    )


# --- 設定頁（P3-2B；規格 F07、7.3）-----------------------------------------------------------


@dataclass(frozen=True)
class CardSettingsRow:
    card: Account
    profile: CreditCardProfile | None  # None = 尚未設定


def list_card_settings(db: Session) -> tuple[CardSettingsRow, ...]:
    """設定頁：所有啟用中的信用卡（不論是否顯示在首頁）與其目前設定。"""
    return tuple(
        CardSettingsRow(card, get_profile(db, card.id))
        for card in account_repo.list_credit_cards(db)
    )


def autopay_candidates(db: Session) -> list[Account]:
    return account_repo.list_autopay_candidates(db)


def update_card_settings(
    db: Session,
    card_account_id: int,
    *,
    closing_day: int | None,
    due_day: int | None,
    autopay_account_id: int | None = None,
) -> CreditCardProfile:
    """設定頁的儲存：只改結帳日、繳款日、扣繳帳戶，保留既有 credit_limit。

    set_profile() 沒收到 credit_limit 會把它清成 None；設定頁沒有額度欄位，
    所以這裡先讀出現值再原封不動帶回去。驗證與寫入都交給 set_profile()。
    """
    if autopay_account_id is not None and account_repo.get(db, autopay_account_id) is None:
        raise RuleViolation("M07", "扣繳帳戶必須是啟用中、最末層的銀行（BANK）帳戶")
    existing = get_profile(db, card_account_id)
    return set_profile(
        db,
        card_account_id,
        closing_day=closing_day,
        due_day=due_day,
        autopay_account_id=autopay_account_id,
        credit_limit=existing.credit_limit if existing is not None else None,
    )
