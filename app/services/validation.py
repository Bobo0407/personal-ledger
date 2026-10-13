"""交易驗證（規格第 9 節；P1 Step 3 決策 Q4、Q8、Q10 與修正）。

結果分三種：errors（擋下）、confirmations（跳確認，帶 confirmed 重送才寫入）、
notices（儲存後提示）。
只有在沒有任何 error 時才計算 confirmations 與 notices。規則全部跑完才回傳，多個錯誤一次列出。

所有交易類型共用的規則（Step 3 起）：
- POSTING_AMOUNT：每筆分錄金額必須是整數（不接受 bool）且不為 0。
  這是分錄的不變條件，不是規格的 V01；V01（使用者輸入的金額 > 0）由各 EntryBuilder 的輸入處理。
- V02：至少 2 筆分錄，且加總為 0。
- V03：科目必須是最末層（沒有任何子科目）。
- V04：科目、付款方式、商家必須是啟用中。新增交易時每筆分錄都檢查；
  修改交易時只檢查新增或被改動的分錄（決策 D14，見下方 Step 6）。
- V05：日期必填；每筆分錄必須選科目，且科目必須存在。
- V09：原幣別與原幣金額必須同時填寫或同時空白，且幣別必須存在。
- SYSTEM_ACCOUNT：期初權益只能用在 OPENING、帳務調整只能用在 ADJUSTMENT（Q8）。
- V10（確認）：交易金額 = 所有正數分錄加總，「大於」settings.large_amount_threshold 才確認。
  設定不存在時用規格預設 10,000；設定存在但不是正整數時丟 InvalidSetting，不靜默改用預設值。
- V11（確認）：交易日期晚於今天（台北日期）。
- V12（確認，P2 Step 2）：同一天、同商家、MAIN 金額相同的有效 EXPENSE 已經有一筆。
  只比對 EXPENSE；金額只看 MAIN（手續費、利息不影響）；從 active_postings() 查，
  已刪除與草稿不算；修改時排除自己，且只在日期、商家或 MAIN 金額改變時重問（同 U3）。
- V08（P2 Step 2）：發票號碼格式為 2 個大寫英文字母 + 8 位數字，且不能被其他未刪除的交易使用。
  新增與修改共用；修改時排除自己。還原時的 V08 見 validate_restore()。
- V13（提醒）：CASH／BANK 帳戶「截至今天」的餘額因為這筆交易而變少，且結果 < 0。
  未來日期的交易不影響今天的餘額，所以不會觸發。
- TXN_STRUCTURE（Step 4、5）：依 txn_type 檢查分錄結構，是 builder 之外的第二道防線，
  見 txn_structure.py。
- V15（Step 5）：ADJUSTMENT 備註必填（field=description），且只能調整一個資產或負債帳戶，
  對方固定是「帳務調整」系統科目。
- OPENING_EXISTS（Step 5，D12）：同一帳戶只能有一筆有效期初分錄；同一筆期初裡也不能重複。
- V10 不套用在 OPENING、PAYROLL（R4）。

Step 6：修改、刪除、還原與退款
- validate(db, draft, existing)：existing 是修改前的交易（ExistingTransaction），新增時為 None。
  - V04 只檢查新增或被改動的分錄（D14）。分錄身分 = (account_id, amount, line_role,
    original_currency, original_amount_minor)，不含 memo；用 Counter 比對（重複分錄也正確）。
    付款方式、商家沒換就不檢查 V04。
  - V10 只在交易金額改變、且新金額超過門檻時確認；V11 只在日期改成未來時確認（U3）。
  - V13 用「現在餘額 − 舊影響 + 新影響」判斷；舊／新影響只在各自日期 ≤ 今天時才計入（U4）。
  - OPENING_EXISTS 排除自己。
- REFUND_ORIGINAL：原交易必須存在、未刪除、POSTED、而且是 EXPENSE；退款必須沖回原交易的 MAIN 科目。
- REFUND_DATE：退款日期不能早於原交易日期。
- V07：其他有效退款 + 本筆 ≤ 原交易 MAIN 金額。
- REFUND_LOCKED（U10、U11）：原支出已有有效退款時，MAIN 科目與 exclude_from_budget 不能改、
  MAIN 金額不能低於已退金額、日期不能晚於最早一筆有效退款；退款不能改原交易。
- validate_restore(db, txn)：還原時的檢查（U6、U14），見該函式說明。

使用者輸入層的規則（V01 金額 > 0、V06、表單必填欄位）在各 builder 的 check(form)，不在這裡；
同一條規則只在一個地方實作。
"""

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import clock
from app.models import Account, Currency, Merchant, PaymentMethod, Setting, Transaction
from app.models.enums import (
    AccountClass,
    AccountSubtype,
    LineRole,
    SystemAccountKey,
    TxnStatus,
    TxnType,
)
from app.repositories import accounts as account_repo
from app.repositories import ledger as ledger_repo
from app.repositories import tags as tag_repo
from app.services.drafts import EntryDraft, ExistingTransaction, PostingDraft, draft_from_model
from app.services.issues import Issue
from app.services.ledger_queries import raw_balance
from app.services.txn_structure import check_structure

LARGE_AMOUNT_SETTING = "large_amount_threshold"
DEFAULT_LARGE_AMOUNT_THRESHOLD = 10_000  # 規格 6.10 的初始值；只在 settings 完全沒有這筆時使用
POSITIVE_INT = re.compile(r"[1-9][0-9]*")
INVOICE_NO = re.compile(r"[A-Z]{2}[0-9]{8}")  # 與資料庫 CHECK（GLOB）相同

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


def validate(
    db: Session, draft: EntryDraft, existing: ExistingTransaction | None = None
) -> ValidationResult:
    """existing：修改既有交易時傳入修改前的內容；新增時為 None。"""
    out = _Collector()
    _check_header(db, draft, out, existing)
    accounts = _check_postings(db, draft, out, existing)
    if not out.errors:
        # 交易結構（Step 4，P4）：只在分錄本身沒有問題時檢查，避免同一個錯誤重複出現
        out.errors.extend(check_structure(db, draft, accounts))
        _check_adjustment(draft, accounts, out)
    if not out.errors:
        exclude_id = existing.id if existing is not None else None
        _check_opening_exists(db, draft, accounts, out, exclude_id)
        _check_refund_locks(db, draft, out, existing)
        _check_refund(db, draft, out, exclude_id)
    if not out.errors:
        _check_confirmations(db, draft, out, existing)
        out.notices.extend(negative_balance_notices(db, draft, existing))
    return out.result()


def validate_restore(db: Session, txn: Transaction) -> ValidationResult:
    """還原已刪除的交易（U6、U14）。

    還原是把原本的內容放回帳上，不是重新輸入，所以不重跑 V04（科目停用也可以還原）、
    V10、V11；只檢查「放回去之後帳會不會壞」：
    - V02：分錄筆數與借貸平衡（資料健全性；正常情況不會發生）。
    - OPENING_EXISTS（D12）：刪除期間是否已經為同一帳戶建立了新的期初。
    - 退款：原交易仍然有效（REFUND_ORIGINAL）、日期（REFUND_DATE）與金額上限（V07）。
      原交易可能在退款刪除期間被修改過。
    - V08：發票號碼是否已被其他未刪除的交易使用（完整的 V08 在 P2）。
    - V13（提醒）。
    """
    draft = draft_from_model(txn)
    out = _Collector()
    if len(draft.postings) < 2:
        out.errors.append(Issue("V02", "每筆交易至少需要 2 筆分錄"))
    diff = sum(p.amount for p in draft.postings)
    if diff != 0:
        out.errors.append(Issue("V02", f"借貸不平衡，差額 {abs(diff):,} 元"))
    if draft.invoice_no is not None and ledger_repo.invoice_in_use(db, draft.invoice_no, txn.id):
        out.errors.append(
            Issue("V08", f"發票號碼 {draft.invoice_no} 已被其他交易使用", field="invoice_no")
        )
    accounts = _load_accounts(db, {p.account_id for p in draft.postings})
    _check_opening_exists(db, draft, accounts, out, txn.id)
    _check_refund(db, draft, out, txn.id)
    if not out.errors:
        out.notices.extend(negative_balance_notices(db, draft, None))
    return out.result()


def _load_accounts(db: Session, ids: set[int]) -> dict[int, Account]:
    if not ids:
        return {}
    return {a.id: a for a in db.scalars(select(Account).where(Account.id.in_(ids)))}


def posting_identity(p: PostingDraft) -> tuple:
    """D14 的分錄身分：memo 不算（只改備註不必重新檢查科目是否停用）。"""
    return (p.account_id, p.amount, p.line_role, p.original_currency, p.original_amount_minor)


# --- errors ---------------------------------------------------------------------


def _check_header(
    db: Session, draft: EntryDraft, out: _Collector, existing: ExistingTransaction | None
) -> None:
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

    # 修改時沒換的付款方式、商家不檢查 V04（D14 的精神：只檢查被改動的部分）
    pm_changed = existing is None or draft.payment_method_id != existing.payment_method_id
    merchant_changed = existing is None or draft.merchant_id != existing.merchant_id
    if draft.payment_method_id is not None:
        pm = db.get(PaymentMethod, draft.payment_method_id)
        if pm is None:
            out.errors.append(Issue("V05", "找不到付款方式", field="payment_method_id"))
        elif pm_changed and not pm.is_active:
            out.errors.append(Issue("V04", f"「{pm.name}」已停用", field="payment_method_id"))
    if draft.merchant_id is not None:
        merchant = db.get(Merchant, draft.merchant_id)
        if merchant is None:
            out.errors.append(Issue("V05", "找不到商家", field="merchant_id"))
        elif merchant_changed and not merchant.is_active:
            out.errors.append(Issue("V04", f"「{merchant.name}」已停用", field="merchant_id"))
    _check_tags(db, draft, out, existing)
    _check_invoice(db, draft, out, existing.id if existing is not None else None)


def _check_tags(
    db: Session, draft: EntryDraft, out: _Collector, existing: ExistingTransaction | None
) -> None:
    """標籤（P2 Step 8A）：找不到 → V05；新加上的停用標籤 → V04。
    原本就有、這次沒動的停用標籤可以保留（D14 的精神，和商家相同）。None = 不處理標籤。"""
    if draft.tag_ids is None:
        return
    found = tag_repo.get_many(db, draft.tag_ids)
    kept = set(existing.tag_ids) if existing is not None else set()
    for tag_id in draft.tag_ids:
        tag = found.get(tag_id)
        if tag is None:
            out.errors.append(Issue("V05", f"找不到標籤 #{tag_id}", field="tag_ids"))
        elif not tag.is_active and tag_id not in kept:
            out.errors.append(Issue("V04", f"標籤「{tag.name}」已停用", field="tag_ids"))


def invoice_taken_issue(owner: Transaction) -> Issue:
    """V08：發票號碼已被其他未刪除的交易使用。新增、修改與 LedgerService 的最後防線共用。"""
    day = date.fromisoformat(owner.txn_date)
    return Issue("V08", f"這張發票已記在 {day.month}/{day.day} 的交易", field="invoice_no")


def _check_invoice(
    db: Session, draft: EntryDraft, out: _Collector, exclude_txn_id: int | None
) -> None:
    if draft.invoice_no is None:
        return
    if not isinstance(draft.invoice_no, str) or not INVOICE_NO.fullmatch(draft.invoice_no):
        message = "發票號碼格式為 2 個英文字母加 8 位數字，例如 AB12345678"
        out.errors.append(Issue("V08", message, field="invoice_no"))
        return
    owner = ledger_repo.active_invoice_owner(db, draft.invoice_no, exclude_txn_id)
    if owner is not None:
        out.errors.append(invoice_taken_issue(owner))


def _check_postings(
    db: Session, draft: EntryDraft, out: _Collector, existing: ExistingTransaction | None
) -> dict[int, Account]:
    postings = draft.postings
    accounts = _load_accounts(db, {p.account_id for p in postings if _is_int(p.account_id)})
    currencies = set(db.scalars(select(Currency.code)))
    # D14：與修改前相同的分錄不檢查 V04。用 Counter（多重集合）比對，
    # 同一個身分出現兩次、舊版本只有一次時，多出來的那一筆仍視為新分錄。
    unchanged = Counter(posting_identity(p) for p in existing.postings) if existing else Counter()

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
            key = posting_identity(p)
            if unchanged[key] > 0:
                unchanged[key] -= 1
            elif not account.is_active:
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
    return _magnitude(draft.postings)


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
    db: Session,
    draft: EntryDraft,
    accounts: dict[int, Account],
    out: _Collector,
    exclude_txn_id: int | None,
) -> None:
    """D12：同一帳戶只能有一筆有效的期初分錄（已刪除的期初不算；修改、還原時排除自己）。"""
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
    existing = ledger_repo.accounts_with_active_opening(db, seen, exclude_txn_id)
    for line, p in enumerate(draft.postings, start=1):
        if p.account_id in existing:
            name = accounts[p.account_id].name
            out.errors.append(Issue("OPENING_EXISTS", f"「{name}」已經有期初餘額", line))


def _check_confirmations(
    db: Session, draft: EntryDraft, out: _Collector, existing: ExistingTransaction | None
) -> None:
    """修改時只在相關欄位改變才重新確認（U3）：只改備註不必再確認一次大金額或未來日期。"""
    magnitude = transaction_magnitude(draft)
    magnitude_changed = existing is None or magnitude != _magnitude(existing.postings)
    if (
        draft.txn_type not in V10_EXEMPT
        and magnitude_changed
        and magnitude > large_amount_threshold(db)
    ):
        out.confirmations.append(Issue("V10", f"這筆 {magnitude:,} 元，確定金額沒有多打一個 0？"))
    days = (draft.txn_date - clock.today()).days
    date_changed = existing is None or draft.txn_date != existing.txn_date
    if days > 0 and date_changed:
        out.confirmations.append(Issue("V11", f"日期是 {days} 天後，確定嗎？"))
    _check_duplicate_expense(db, draft, out, existing)


def _check_duplicate_expense(
    db: Session, draft: EntryDraft, out: _Collector, existing: ExistingTransaction | None
) -> None:
    """V12：同一天、同商家、MAIN 金額相同的有效 EXPENSE。"""
    if draft.txn_type != TxnType.EXPENSE or draft.merchant_id is None:
        return
    main = main_posting(draft.postings)
    if main is None:
        return
    if existing is not None:
        old_main = main_posting(existing.postings)
        unchanged = (
            draft.txn_date == existing.txn_date
            and draft.merchant_id == existing.merchant_id
            and old_main is not None
            and main.amount == old_main.amount
        )
        if unchanged:
            return
    exclude_id = existing.id if existing is not None else None
    if not ledger_repo.active_expense_main_exists(
        db, draft.txn_date, draft.merchant_id, main.amount, exclude_id
    ):
        return
    merchant = db.get(Merchant, draft.merchant_id)
    name = merchant.name if merchant is not None else "這個商家"
    day = (
        "今天"
        if draft.txn_date == clock.today()
        else f"{draft.txn_date.month}/{draft.txn_date.day}"
    )
    out.confirmations.append(Issue("V12", f"{day}已經有一筆 {name} {main.amount:,} 元，是重複嗎？"))


def _magnitude(postings: tuple[PostingDraft, ...]) -> int:
    return sum(p.amount for p in postings if p.amount > 0)


# --- notices ---------------------------------------------------------------------


def negative_balance_notices(
    db: Session, draft: EntryDraft, existing: ExistingTransaction | None
) -> list[Issue]:
    """V13：CASH／BANK 帳戶「截至今天」的餘額因這次寫入而變少，且結果 < 0。

    新餘額 = 現在餘額 − 舊影響 + 新影響（U4）。
    - 舊影響：修改前的分錄，只在舊日期 ≤ 今天時計入（未來日期本來就不在今天的餘額裡）。
    - 新影響：這次的分錄，只在新日期 ≤ 今天時計入。
    依科目加總淨變動（同一科目可以有多筆分錄，Q10），不逐筆判斷。
    """
    today = clock.today()
    deltas: dict[int, int] = defaultdict(int)
    if draft.txn_date <= today:
        for p in draft.postings:
            deltas[p.account_id] += p.amount
    if existing is not None and existing.txn_date <= today:
        for p in existing.postings:
            deltas[p.account_id] -= p.amount
    accounts = _load_accounts(db, {a for a, d in deltas.items() if d < 0})
    notices: list[Issue] = []
    for account_id, delta in deltas.items():
        account = accounts.get(account_id)
        if delta >= 0 or account is None or account.subtype not in NEGATIVE_BALANCE_SUBTYPES:
            continue
        after = raw_balance(db, account_id, today) + delta  # 資產：正常餘額方向 = 原始正負號
        if after < 0:
            notices.append(
                Issue("V13", f"{account.name}餘額變成 −{abs(after):,}，可能漏記了提款或收入")
            )
    return notices


# --- 退款（Step 6）---------------------------------------------------------------


def refund_original(db: Session, original_id: int | None) -> tuple[Transaction | None, list[Issue]]:
    """退款的原交易：必須存在、未刪除、POSTED、而且是支出（REFUND_ORIGINAL）。"""
    field_name = "original_txn_id"
    if original_id is None:
        return None, [Issue("REFUND_ORIGINAL", "請選擇要退款的原交易", field=field_name)]
    original = db.get(Transaction, original_id)
    if original is None:
        return None, [Issue("REFUND_ORIGINAL", "找不到原交易", field=field_name)]
    if original.deleted_at is not None or original.status != TxnStatus.POSTED:
        return None, [Issue("REFUND_ORIGINAL", "原交易已刪除或尚未入帳", field=field_name)]
    if original.txn_type != TxnType.EXPENSE:
        return None, [Issue("REFUND_ORIGINAL", "只能對支出交易退款", field=field_name)]
    return original, []


def main_posting(postings) -> object | None:
    """MAIN 分錄（支出與退款都剛好一筆，已由 TXN_STRUCTURE 保證）。"""
    return next((p for p in postings if p.line_role == LineRole.MAIN), None)


def _check_refund(
    db: Session, draft: EntryDraft, out: _Collector, exclude_txn_id: int | None
) -> None:
    if draft.txn_type != TxnType.REFUND:
        return
    original, issues = refund_original(db, draft.related_txn_id)
    if original is None:
        out.errors.extend(issues)
        return
    original_main = main_posting(original.postings)
    refund_main = main_posting(draft.postings)
    if original_main is None or refund_main is None:
        return  # 結構錯誤已由 TXN_STRUCTURE 回報
    if refund_main.account_id != original_main.account_id:
        out.errors.append(
            Issue("REFUND_ORIGINAL", "退款必須沖回原交易的支出科目", field="original_txn_id")
        )
    if draft.txn_date < date.fromisoformat(original.txn_date):
        out.errors.append(
            Issue(
                "REFUND_DATE", f"退款日期不能早於原交易日期 {original.txn_date}", field="txn_date"
            )
        )
    refunded = ledger_repo.active_refunded_amount(db, original.id, exclude_txn_id)
    remaining = original_main.amount - refunded
    if -refund_main.amount > remaining:
        out.errors.append(
            Issue(
                "V07",
                f"原交易 {original_main.amount:,} 元，已退 {refunded:,} 元，"
                f"最多還能退 {max(remaining, 0):,} 元",
                field="amount",
            )
        )


def _check_refund_locks(
    db: Session, draft: EntryDraft, out: _Collector, existing: ExistingTransaction | None
) -> None:
    """REFUND_LOCKED（U10、U11）。商家、備註、付款方式、手續費、利息仍可修改；
    不做「同步更新退款」的機制。"""
    if existing is None:
        return
    if existing.txn_type == TxnType.REFUND and draft.related_txn_id != existing.related_txn_id:
        out.errors.append(Issue("REFUND_LOCKED", "退款不能改成其他原交易", field="original_txn_id"))
    if existing.txn_type != TxnType.EXPENSE:
        return
    refunded = ledger_repo.active_refunded_amount(db, existing.id)
    if refunded == 0:
        return
    old_main = main_posting(existing.postings)
    new_main = main_posting(draft.postings)
    if new_main.account_id != old_main.account_id:
        out.errors.append(
            Issue("REFUND_LOCKED", "這筆支出已有退款，不能更換支出科目", field="account_id")
        )
    if new_main.amount < refunded:
        out.errors.append(
            Issue(
                "REFUND_LOCKED",
                f"這筆支出已退 {refunded:,} 元，金額不能低於已退金額",
                field="amount",
            )
        )
    earliest = ledger_repo.earliest_active_refund_date(db, existing.id)
    if earliest is not None and draft.txn_date > date.fromisoformat(earliest):
        out.errors.append(
            Issue(
                "REFUND_LOCKED",
                f"這筆支出在 {earliest} 已有退款，日期不能晚於退款日",
                field="txn_date",
            )
        )
    if draft.exclude_from_budget != existing.exclude_from_budget:
        out.errors.append(
            Issue(
                "REFUND_LOCKED",
                "這筆支出已有退款，不能更改「不計入預算」",
                field="exclude_from_budget",
            )
        )
