"""交易驗證（規格第 9 節；P1 Step 3 決策 Q4、Q8、Q10 與修正）。

結果分三種：errors（擋下）、confirmations（跳確認，帶 confirmed 重送才寫入）、
notices（儲存後提示）。
只有在沒有任何 error 時才計算 confirmations 與 notices。規則全部跑完才回傳，多個錯誤一次列出。

Step 3 實作的是「所有交易類型共用」的規則：
- POSTING_AMOUNT：每筆分錄金額必須是整數（不接受 bool）且不為 0。
  這是分錄的不變條件，不是規格的 V01；V01（使用者輸入的金額 > 0）由各 EntryBuilder 的輸入處理。
- V02：至少 2 筆分錄，且加總為 0。
- V03：科目必須是最末層（沒有任何子科目）。
- V04：科目、付款方式、商家必須是啟用中。Step 3 只有新增交易，所以每筆分錄都檢查；
  修改交易時只檢查新增或被改動的分錄（決策 D14，Step 6）。
- V05：日期必填；每筆分錄必須選科目，且科目必須存在。
- V09：原幣別與原幣金額必須同時填寫或同時空白，且幣別必須存在。
- SYSTEM_ACCOUNT：期初權益只能用在 OPENING、帳務調整只能用在 ADJUSTMENT（Q8）。
- V10（確認）：交易金額 = 所有正數分錄加總，「大於」settings.large_amount_threshold 才確認。
  設定不存在時用規格預設 10,000；設定存在但不是正整數時丟 InvalidSetting，不靜默改用預設值。
- V11（確認）：交易日期晚於今天（台北日期）。
- V13（提醒）：CASH／BANK 帳戶「截至今天」的餘額因為這筆交易而變少，且結果 < 0。
  未來日期的交易不影響今天的餘額，所以不會觸發。
- TXN_STRUCTURE（Step 4、5）：依 txn_type 檢查分錄結構，是 builder 之外的第二道防線，
  見 txn_structure.py。
- V15（Step 5）：ADJUSTMENT 備註必填（field=description），且只能調整一個資產或負債帳戶，
  對方固定是「帳務調整」系統科目。
- OPENING_EXISTS（Step 5，D12）：同一帳戶只能有一筆有效期初分錄；同一筆期初裡也不能重複。
- V10 不套用在 OPENING、PAYROLL（R4）。

使用者輸入層的規則（V01 金額 > 0、V06、表單必填欄位）在各 builder 的 check(form)，不在這裡；
同一條規則只在一個地方實作。V07 退款在 Step 6。
"""

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import clock
from app.models import Account, Currency, Merchant, PaymentMethod, Setting
from app.models.enums import AccountClass, AccountSubtype, LineRole, SystemAccountKey, TxnType
from app.repositories import accounts as account_repo
from app.repositories import ledger as ledger_repo
from app.services.drafts import EntryDraft
from app.services.issues import Issue
from app.services.ledger_queries import raw_balance
from app.services.txn_structure import check_structure

LARGE_AMOUNT_SETTING = "large_amount_threshold"
DEFAULT_LARGE_AMOUNT_THRESHOLD = 10_000  # 規格 6.10 的初始值；只在 settings 完全沒有這筆時使用
POSITIVE_INT = re.compile(r"[1-9][0-9]*")

# V10 不套用的交易類型：金額本來就經常超過門檻，「多打一個 0」的確認只會變成固定噪音（R4）
V10_EXEMPT = frozenset({TxnType.OPENING, TxnType.PAYROLL})

# 系統科目只能出現在對應的交易類型（Q8）
SYSTEM_ACCOUNT_TXN_TYPE = {
    SystemAccountKey.OPENING_EQUITY: TxnType.OPENING,
    SystemAccountKey.ADJUSTMENT: TxnType.ADJUSTMENT,
}

NEGATIVE_BALANCE_SUBTYPES = frozenset({AccountSubtype.CASH, AccountSubtype.BANK})


@dataclass(frozen=True)
class ValidationResult:
    errors: tuple[Issue, ...] = ()
    confirmations: tuple[Issue, ...] = ()
    notices: tuple[Issue, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors

    def codes(self) -> set[str]:
        return {i.code for i in (*self.errors, *self.confirmations, *self.notices)}


class InvalidSetting(RuntimeError):
    """系統設定存在但內容不合法。這是設定資料異常，不靜默改用預設值。"""

    def __init__(self, key: str, value: object) -> None:
        super().__init__(f"系統設定 {key} 的值不合法：{value!r}（必須是正整數）")
        self.key = key
        self.value = value


class ValidationFailed(Exception):
    """有 error，不能儲存（P2 對應 HTTP 422）。"""

    def __init__(self, result: ValidationResult) -> None:
        super().__init__("；".join(i.message for i in result.errors))
        self.result = result


class ConfirmationRequired(Exception):
    """有尚未確認的項目（P2 對應 HTTP 409）。pending 是還沒確認的那些。"""

    def __init__(self, result: ValidationResult, pending: tuple[Issue, ...]) -> None:
        super().__init__("；".join(i.message for i in pending))
        self.result = result
        self.pending = pending


@dataclass
class _Collector:
    errors: list[Issue] = field(default_factory=list)
    confirmations: list[Issue] = field(default_factory=list)
    notices: list[Issue] = field(default_factory=list)

    def result(self) -> ValidationResult:
        return ValidationResult(tuple(self.errors), tuple(self.confirmations), tuple(self.notices))


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def validate(db: Session, draft: EntryDraft) -> ValidationResult:
    out = _Collector()
    _check_header(db, draft, out)
    accounts = _check_postings(db, draft, out)
    if not out.errors:
        # 交易結構（Step 4，P4）：只在分錄本身沒有問題時檢查，避免同一個錯誤重複出現
        out.errors.extend(check_structure(db, draft, accounts))
        _check_adjustment(draft, accounts, out)
    if not out.errors:
        _check_opening_exists(db, draft, accounts, out)
    if not out.errors:
        _check_confirmations(db, draft, out)
        _check_notices(db, draft, accounts, out)
    return out.result()


# --- errors ---------------------------------------------------------------------


def _check_header(db: Session, draft: EntryDraft, out: _Collector) -> None:
    if draft.txn_date is None:
        out.errors.append(Issue("V05", "請填寫日期", field="txn_date"))
    elif isinstance(draft.txn_date, datetime) or not isinstance(draft.txn_date, date):
        out.errors.append(Issue("V05", "日期格式不正確", field="txn_date"))
    if draft.txn_type not in set(TxnType):
        out.errors.append(Issue("TXN_TYPE", f"不認識的交易類型：{draft.txn_type}"))
    if draft.txn_type == TxnType.ADJUSTMENT and not (draft.description or "").strip():
        out.errors.append(
            Issue("V15", "請說明調整原因，例如「月底現金盤點短少」", field="description")
        )

    if draft.payment_method_id is not None:
        pm = db.get(PaymentMethod, draft.payment_method_id)
        if pm is None:
            out.errors.append(Issue("V05", "找不到付款方式", field="payment_method_id"))
        elif not pm.is_active:
            out.errors.append(Issue("V04", f"「{pm.name}」已停用", field="payment_method_id"))
    if draft.merchant_id is not None:
        merchant = db.get(Merchant, draft.merchant_id)
        if merchant is None:
            out.errors.append(Issue("V05", "找不到商家", field="merchant_id"))
        elif not merchant.is_active:
            out.errors.append(Issue("V04", f"「{merchant.name}」已停用", field="merchant_id"))


def _check_postings(db: Session, draft: EntryDraft, out: _Collector) -> dict[int, Account]:
    postings = draft.postings
    ids = {p.account_id for p in postings if _is_int(p.account_id)}
    accounts = (
        {a.id: a for a in db.scalars(select(Account).where(Account.id.in_(ids)))} if ids else {}
    )
    currencies = set(db.scalars(select(Currency.code)))

    if len(postings) < 2:
        out.errors.append(Issue("V02", "每筆交易至少需要 2 筆分錄"))

    amounts_ok = True
    for line, p in enumerate(postings, start=1):
        if not _is_int(p.amount) or p.amount == 0:
            out.errors.append(Issue("POSTING_AMOUNT", "分錄金額必須是不為 0 的整數", line))
            amounts_ok = False

        account = None
        if p.account_id is None:
            out.errors.append(Issue("V05", "請選擇科目", line))
        else:
            account = accounts.get(p.account_id)
            if account is None:
                out.errors.append(Issue("V05", "找不到科目", line))
        if account is not None:
            if account_repo.has_children(db, account.id):
                out.errors.append(Issue("V03", f"請選擇「{account.name}」底下的子科目", line))
            if not account.is_active:
                out.errors.append(Issue("V04", f"「{account.name}」已停用", line))
            required_type = SYSTEM_ACCOUNT_TXN_TYPE.get(account.system_key)
            if account.system_key is not None and draft.txn_type != required_type:
                out.errors.append(
                    Issue(
                        "SYSTEM_ACCOUNT",
                        f"「{account.name}」是系統科目，只能用在 {required_type} 交易",
                        line,
                    )
                )

        if p.line_role is not None and p.line_role not in set(LineRole):
            out.errors.append(Issue("LINE_ROLE", f"不認識的分錄角色：{p.line_role}", line))

        if (p.original_currency is None) != (p.original_amount_minor is None):
            missing = "原幣金額" if p.original_amount_minor is None else "原幣別"
            out.errors.append(Issue("V09", f"請填寫{missing}", line))
        elif p.original_currency is not None:
            if p.original_currency not in currencies:
                out.errors.append(Issue("V09", f"不認識的幣別：{p.original_currency}", line))
            if not _is_int(p.original_amount_minor) or p.original_amount_minor <= 0:
                out.errors.append(Issue("V09", "原幣金額必須是大於 0 的整數", line))

    if amounts_ok and postings:
        diff = sum(p.amount for p in postings)
        if diff != 0:
            out.errors.append(Issue("V02", f"借貸不平衡，差額 {abs(diff):,} 元"))
    return accounts


# --- confirmations ---------------------------------------------------------------


def transaction_magnitude(draft: EntryDraft) -> int:
    """交易金額 = 所有正數分錄的加總（Q4）。例：轉帳 5,000 + 手續費 15 → 5,015。"""
    return sum(p.amount for p in draft.postings if p.amount > 0)


def large_amount_threshold(db: Session) -> int:
    """V10 門檻。設定不存在 → 規格預設 10,000；設定存在但不是正整數 → InvalidSetting。"""
    setting = db.get(Setting, LARGE_AMOUNT_SETTING)
    if setting is None:
        return DEFAULT_LARGE_AMOUNT_THRESHOLD
    if not POSITIVE_INT.fullmatch(setting.value or ""):
        raise InvalidSetting(LARGE_AMOUNT_SETTING, setting.value)
    return int(setting.value)


def _check_adjustment(draft: EntryDraft, accounts: dict[int, Account], out: _Collector) -> None:
    """V15：帳務調整只能調整一個資產或負債帳戶，對方固定是「帳務調整」系統科目。"""
    if draft.txn_type != TxnType.ADJUSTMENT:
        return
    lines = [accounts[p.account_id] for p in draft.postings]
    system = [a for a in lines if a.system_key == SystemAccountKey.ADJUSTMENT]
    others = [a for a in lines if a.system_key != SystemAccountKey.ADJUSTMENT]
    if (
        len(lines) != 2
        or len(system) != 1
        or len(others) != 1
        or others[0].account_class not in (AccountClass.ASSET, AccountClass.LIABILITY)
    ):
        out.errors.append(
            Issue("V15", "帳務調整只能調整一個資產或負債帳戶，對方科目固定為「帳務調整」")
        )


def _check_opening_exists(
    db: Session, draft: EntryDraft, accounts: dict[int, Account], out: _Collector
) -> None:
    """D12：同一帳戶只能有一筆有效的期初分錄（已刪除的期初不算）。"""
    if draft.txn_type != TxnType.OPENING:
        return
    seen: set[int] = set()
    for line, p in enumerate(draft.postings, start=1):
        if accounts[p.account_id].system_key is not None:
            continue  # 期初權益每筆期初交易都會出現
        if p.account_id in seen:
            name = accounts[p.account_id].name
            out.errors.append(Issue("OPENING_EXISTS", f"「{name}」在這筆期初中重複", line))
        seen.add(p.account_id)
    existing = ledger_repo.accounts_with_active_opening(db, seen)
    for line, p in enumerate(draft.postings, start=1):
        if p.account_id in existing:
            name = accounts[p.account_id].name
            out.errors.append(Issue("OPENING_EXISTS", f"「{name}」已經有期初餘額", line))


def _check_confirmations(db: Session, draft: EntryDraft, out: _Collector) -> None:
    magnitude = transaction_magnitude(draft)
    if draft.txn_type not in V10_EXEMPT and magnitude > large_amount_threshold(db):
        out.confirmations.append(Issue("V10", f"這筆 {magnitude:,} 元，確定金額沒有多打一個 0？"))
    days = (draft.txn_date - clock.today()).days
    if days > 0:
        out.confirmations.append(Issue("V11", f"日期是 {days} 天後，確定嗎？"))


# --- notices ---------------------------------------------------------------------


def _check_notices(
    db: Session, draft: EntryDraft, accounts: dict[int, Account], out: _Collector
) -> None:
    today = clock.today()
    if draft.txn_date > today:
        return  # 未來日期的交易不影響「截至今天」的餘額
    # 先依科目加總本筆交易的淨變動（同一科目可以出現多筆分錄，Q10），再判斷，不逐筆判斷
    deltas: dict[int, int] = defaultdict(int)
    for p in draft.postings:
        deltas[p.account_id] += p.amount
    for account_id, delta in deltas.items():
        account = accounts[account_id]
        if account.subtype not in NEGATIVE_BALANCE_SUBTYPES or delta >= 0:
            continue
        after = raw_balance(db, account_id, today) + delta  # 資產：正常餘額方向 = 原始正負號
        if after < 0:
            out.notices.append(
                Issue("V13", f"{account.name}餘額變成 −{abs(after):,}，可能漏記了提款或收入")
            )
