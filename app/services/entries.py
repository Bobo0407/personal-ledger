"""EntryBuilder 家族：把表單輸入轉成分錄草稿（規格第 7 節）。

Builder 只做結構轉換：不查資料庫、不做業務驗證，所有規則在 validation.py。
Step 3：ManualEntry；Step 4：Expense、Income、Transfer、CardPayment；
Opening、Adjustment、Payroll 在 Step 5，Refund 在 Step 6。
"""

from dataclasses import dataclass
from datetime import date

from app.models.enums import LineRole, TxnType
from app.services.drafts import EntryDraft, PostingDraft
from app.services.issues import Issue


def clean_optional(text: str | None) -> str | None:
    text = (text or "").strip()
    return text or None


def _is_zero(amount: object) -> bool:
    # 只略過「整數 0」；其他不合法的值（None、小數、bool）留給驗證擋下
    return isinstance(amount, int) and not isinstance(amount, bool) and amount == 0


@dataclass(frozen=True)
class ManualLine:
    """多分錄交易的一行。amount 帶正負號：借方為正、貸方為負（Q6）。"""

    account_id: int | None
    amount: int
    line_role: str | None = None
    original_currency: str | None = None
    original_amount_minor: int | None = None
    memo: str | None = None


@dataclass(frozen=True)
class ManualForm:
    txn_date: date | None
    lines: tuple[ManualLine, ...]
    description: str | None = None
    merchant_id: int | None = None


class ManualEntry:
    """自由多分錄交易（F13）。金額為 0 的行自動略過（Q6，與 D10 薪資單共用）。"""

    txn_type = TxnType.MANUAL

    @classmethod
    def build(cls, form: ManualForm) -> EntryDraft:
        postings = tuple(
            PostingDraft(
                account_id=line.account_id,
                amount=line.amount,
                line_role=line.line_role,
                original_currency=line.original_currency,
                original_amount_minor=line.original_amount_minor,
                memo=clean_optional(line.memo),
            )
            for line in form.lines
            if not _is_zero(line.amount)
        )
        return EntryDraft(
            txn_date=form.txn_date,
            txn_type=cls.txn_type.value,
            postings=postings,
            description=clean_optional(form.description),
            merchant_id=form.merchant_id,
        )


# --- 一般交易的 builder（Step 4）-----------------------------------------------------
#
# 責任邊界（Step 4 決策 P2）：
# - check(form)：只做「產生草稿之前」必須先確認的表單語意，純函式：
#   V01（使用者輸入金額必須是大於 0 的整數）、V06（轉出轉入不同）、表單必填欄位（V05，帶 field）、
#   有手續費／利息金額但沒選科目。日期是否填寫由 validation.py 的草稿 V05 負責。
# - build(form, ...)：純結構轉換，不查資料庫；呼叫前必須先通過 check()。
#   付款方式要先由 transactions.py 解析成帳戶再傳進來（P1）。
# - 草稿產生後的帳務不變條件（V02–V04、V09、系統科目、TXN_STRUCTURE、V10、V11、V13）
#   全部在 validation.py，同一條規則不在兩處實作。


def _valid_amount(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _has_optional_amount(value: object) -> bool:
    """手續費、利息：None 或 0 視為沒填。"""
    return not (value is None or _is_zero(value))


def _required_amount(issues: list[Issue], value: object, field: str = "amount") -> None:
    if value is None:
        issues.append(Issue("V05", "請填寫金額", field=field))
    elif not _valid_amount(value):
        issues.append(Issue("V01", "金額必須是大於 0 的整數", field=field))


def _optional_amount(
    issues: list[Issue], value: object, account_id: int | None, prefix: str, label: str
) -> None:
    """prefix 為 "fee" 或 "interest"，對應表單欄位 <prefix>_amount 與 <prefix>_account_id。"""
    if not _has_optional_amount(value):
        return
    if not _valid_amount(value):
        issues.append(Issue("V01", f"{label}必須是大於 0 的整數", field=f"{prefix}_amount"))
    if account_id is None:
        issues.append(Issue("V05", f"請選擇{label}科目", field=f"{prefix}_account_id"))


def _required_id(issues: list[Issue], value: int | None, field: str, label: str) -> None:
    if value is None:
        issues.append(Issue("V05", f"請選擇{label}", field=field))


@dataclass(frozen=True)
class ExpenseForm:
    txn_date: date | None
    amount: int | None  # 帳單上的台幣金額（分期時為本金全額）
    account_id: int | None  # 費用末層科目
    payment_method_id: int | None
    fee_amount: int | None = None  # 例如海外交易手續費
    fee_account_id: int | None = None
    interest_amount: int | None = None  # 信用卡分期總利息（D7：不存期數；P12：只限信用卡）
    interest_account_id: int | None = None
    original_currency: str | None = None  # 只放在 MAIN 分錄（P8）
    original_amount_minor: int | None = None
    description: str | None = None
    merchant_id: int | None = None
    voucher_type: str | None = None
    invoice_no: str | None = None
    exclude_from_budget: bool = False


class ExpenseEntry:
    """一般支出（F08、F12、UF01、UF06、UF07）。

    分錄：費用 +amount（MAIN，帶原幣資訊）、手續費 +fee（FEE）、利息 +interest（INTEREST）、
    付款方式對應的帳戶 −合計。刷信用卡時付款帳戶是負債，欠款增加，費用在刷卡當天認列。
    """

    txn_type = TxnType.EXPENSE

    @staticmethod
    def check(form: ExpenseForm) -> tuple[Issue, ...]:
        issues: list[Issue] = []
        _required_amount(issues, form.amount)
        _required_id(issues, form.account_id, "account_id", "科目")
        _required_id(issues, form.payment_method_id, "payment_method_id", "付款方式")
        _optional_amount(issues, form.fee_amount, form.fee_account_id, "fee", "手續費")
        _optional_amount(
            issues, form.interest_amount, form.interest_account_id, "interest", "分期利息"
        )
        return tuple(issues)

    @classmethod
    def build(cls, form: ExpenseForm, *, payment_account_id: int) -> EntryDraft:
        lines = [
            PostingDraft(
                form.account_id,
                form.amount,
                line_role=LineRole.MAIN.value,
                original_currency=form.original_currency,
                original_amount_minor=form.original_amount_minor,
            )
        ]
        if _has_optional_amount(form.fee_amount):
            lines.append(PostingDraft(form.fee_account_id, form.fee_amount, LineRole.FEE.value))
        if _has_optional_amount(form.interest_amount):
            lines.append(
                PostingDraft(
                    form.interest_account_id, form.interest_amount, LineRole.INTEREST.value
                )
            )
        lines.append(PostingDraft(payment_account_id, -sum(p.amount for p in lines)))
        return EntryDraft(
            txn_date=form.txn_date,
            txn_type=cls.txn_type.value,
            postings=tuple(lines),
            description=clean_optional(form.description),
            merchant_id=form.merchant_id,
            payment_method_id=form.payment_method_id,
            voucher_type=form.voucher_type,
            invoice_no=clean_optional(form.invoice_no),
            exclude_from_budget=form.exclude_from_budget,
        )


@dataclass(frozen=True)
class IncomeForm:
    txn_date: date | None
    amount: int | None
    account_id: int | None  # 收入末層科目
    receive_account_id: int | None  # 收款帳戶（P5：不使用付款方式）
    description: str | None = None
    merchant_id: int | None = None


class IncomeEntry:
    """一般收入（F08）。分錄：收款帳戶 +amount、收入科目 −amount（MAIN，P9）。"""

    txn_type = TxnType.INCOME

    @staticmethod
    def check(form: IncomeForm) -> tuple[Issue, ...]:
        issues: list[Issue] = []
        _required_amount(issues, form.amount)
        _required_id(issues, form.account_id, "account_id", "科目")
        _required_id(issues, form.receive_account_id, "receive_account_id", "收款帳戶")
        return tuple(issues)

    @classmethod
    def build(cls, form: IncomeForm) -> EntryDraft:
        return EntryDraft(
            txn_date=form.txn_date,
            txn_type=cls.txn_type.value,
            postings=(
                PostingDraft(form.receive_account_id, form.amount),
                PostingDraft(form.account_id, -form.amount, LineRole.MAIN.value),
            ),
            description=clean_optional(form.description),
            merchant_id=form.merchant_id,
        )


@dataclass(frozen=True)
class TransferForm:
    txn_date: date | None
    amount: int | None
    from_account_id: int | None
    to_account_id: int | None
    fee_amount: int | None = None  # 跨行轉帳手續費，由轉出帳戶一起負擔
    fee_account_id: int | None = None
    description: str | None = None


class TransferEntry:
    """帳戶間轉帳（F09、F14、UF03）。

    分錄：轉入 +amount、手續費 +fee（FEE，第三筆）、轉出 −(amount + fee)。不標 MAIN（P9）。
    應收／應付（F14）也用轉帳記錄，例如代墊：郵局 → 應收-小明。
    """

    txn_type = TxnType.TRANSFER

    @staticmethod
    def check(form: TransferForm) -> tuple[Issue, ...]:
        issues: list[Issue] = []
        _required_amount(issues, form.amount)
        _required_id(issues, form.from_account_id, "from_account_id", "轉出帳戶")
        _required_id(issues, form.to_account_id, "to_account_id", "轉入帳戶")
        if form.from_account_id is not None and form.from_account_id == form.to_account_id:
            issues.append(Issue("V06", "轉出和轉入不能是同一個帳戶", field="to_account_id"))
        _optional_amount(issues, form.fee_amount, form.fee_account_id, "fee", "手續費")
        return tuple(issues)

    @classmethod
    def build(cls, form: TransferForm) -> EntryDraft:
        lines = [PostingDraft(form.to_account_id, form.amount)]
        total = form.amount
        if _has_optional_amount(form.fee_amount):
            lines.append(PostingDraft(form.fee_account_id, form.fee_amount, LineRole.FEE.value))
            total += form.fee_amount
        lines.append(PostingDraft(form.from_account_id, -total))
        return EntryDraft(
            txn_date=form.txn_date,
            txn_type=cls.txn_type.value,
            postings=tuple(lines),
            description=clean_optional(form.description),
        )


@dataclass(frozen=True)
class CardPaymentForm:
    txn_date: date | None
    amount: int | None
    card_account_id: int | None  # CREDIT_CARD 負債帳戶
    from_account_id: int | None  # CASH／BANK 扣款帳戶（P7）
    description: str | None = None


class CardPaymentEntry:
    """信用卡繳款（F10、UF04）。分錄：信用卡 +amount（欠款減少）、扣款帳戶 −amount。

    只有資產 → 負債兩筆分錄，沒有任何費用分錄，所以不會重複計入支出。
    """

    txn_type = TxnType.CARD_PAYMENT

    @staticmethod
    def check(form: CardPaymentForm) -> tuple[Issue, ...]:
        issues: list[Issue] = []
        _required_amount(issues, form.amount)
        _required_id(issues, form.card_account_id, "card_account_id", "信用卡")
        _required_id(issues, form.from_account_id, "from_account_id", "扣款帳戶")
        return tuple(issues)

    @classmethod
    def build(cls, form: CardPaymentForm) -> EntryDraft:
        return EntryDraft(
            txn_date=form.txn_date,
            txn_type=cls.txn_type.value,
            postings=(
                PostingDraft(form.card_account_id, form.amount),
                PostingDraft(form.from_account_id, -form.amount),
            ),
            description=clean_optional(form.description),
        )
