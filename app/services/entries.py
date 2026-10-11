"""EntryBuilder 家族：把表單輸入轉成分錄草稿（規格第 7 節）。

Builder 只做結構轉換：不查資料庫、不做業務驗證，所有規則在 validation.py。
Step 3 只有 ManualEntry；Expense、Income、Transfer、CardPayment 在 Step 4，
Opening、Adjustment、Payroll 在 Step 5，Refund 在 Step 6。
"""

from dataclasses import dataclass
from datetime import date

from app.models.enums import TxnType
from app.services.drafts import EntryDraft, PostingDraft


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
