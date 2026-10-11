"""交易記錄的流程串接（P1 Step 4 決策 P1、P3、D9）。

P2 的 router 只呼叫這裡的 record_*，不碰借貸邏輯、不自己挑 builder、不直接寫分錄。

每個 record_* 的流程：
1. builder.check(form)：表單語意（V01、V06、必填欄位）。有問題就丟 ValidationFailed，不往下做。
2. 查詢需要的主資料：付款方式 → 實際扣款帳戶（P1）；轉入帳戶是不是信用卡（D9）。
3. builder.build(...)：純結構轉換成 EntryDraft。
4. ledger.post(...)：驗證帳務不變條件並寫入；這是唯一的寫入入口。
"""

from collections.abc import Iterable

from sqlalchemy.orm import Session

from app.models import PaymentMethod
from app.models.enums import AccountSubtype, SystemAccountKey
from app.repositories import accounts as account_repo
from app.services import ledger
from app.services.accounts import system_account
from app.services.entries import (
    AdjustmentEntry,
    AdjustmentForm,
    CardPaymentEntry,
    CardPaymentForm,
    ExpenseEntry,
    ExpenseForm,
    IncomeEntry,
    IncomeForm,
    ManualForm,
    OpeningEntry,
    OpeningForm,
    PayrollEntry,
    TransferEntry,
    TransferForm,
)
from app.services.issues import Issue
from app.services.ledger import PostResult
from app.services.ledger_queries import NORMAL_SIGN
from app.services.txn_structure import CODE as STRUCTURE_CODE
from app.services.validation import ValidationFailed, ValidationResult


def _raise_if(issues: Iterable[Issue]) -> None:
    issues = tuple(issues)
    if issues:
        raise ValidationFailed(ValidationResult(errors=issues))


def record_expense(
    db: Session, form: ExpenseForm, *, confirmed: Iterable[str] = (), user_id: int | None = None
) -> PostResult:
    _raise_if(ExpenseEntry.check(form))
    pm = db.get(PaymentMethod, form.payment_method_id)
    if pm is None:
        _raise_if([Issue("V05", "找不到付款方式", field="payment_method_id")])
    # 付款方式只是管道：LINE Pay → 郵局、聯邦信用卡 → 聯邦信用卡（負債）
    draft = ExpenseEntry.build(form, payment_account_id=pm.account_id)
    return ledger.post(db, draft, confirmed=confirmed, user_id=user_id)


def record_income(
    db: Session, form: IncomeForm, *, confirmed: Iterable[str] = (), user_id: int | None = None
) -> PostResult:
    _raise_if(IncomeEntry.check(form))
    return ledger.post(db, IncomeEntry.build(form), confirmed=confirmed, user_id=user_id)


def record_transfer(
    db: Session, form: TransferForm, *, confirmed: Iterable[str] = (), user_id: int | None = None
) -> PostResult:
    """帳戶間轉帳。轉入帳戶是信用卡時自動改成信用卡繳款（D9）。"""
    _raise_if(TransferEntry.check(form))
    to_account = account_repo.get(db, form.to_account_id)
    if to_account is not None and to_account.subtype == AccountSubtype.CREDIT_CARD:
        if form.fee_amount:
            # 不支援的交易組合（不是缺欄位），所以用結構錯誤代碼而不是 V05
            _raise_if([Issue(STRUCTURE_CODE, "信用卡繳款不支援手續費", field="fee_amount")])
        return record_card_payment(
            db,
            CardPaymentForm(
                txn_date=form.txn_date,
                amount=form.amount,
                card_account_id=form.to_account_id,
                from_account_id=form.from_account_id,
                description=form.description,
            ),
            confirmed=confirmed,
            user_id=user_id,
        )
    return ledger.post(db, TransferEntry.build(form), confirmed=confirmed, user_id=user_id)


def record_card_payment(
    db: Session,
    form: CardPaymentForm,
    *,
    confirmed: Iterable[str] = (),
    user_id: int | None = None,
) -> PostResult:
    _raise_if(CardPaymentEntry.check(form))
    return ledger.post(db, CardPaymentEntry.build(form), confirmed=confirmed, user_id=user_id)


# --- 特殊交易（Step 5）---------------------------------------------------------------


def _sign(db: Session, account_id: int | None) -> int:
    """科目的正常方向正負號；找不到的科目先用 +1，交給 validation 的 V05 擋下。"""
    account = account_repo.get(db, account_id) if account_id is not None else None
    return NORMAL_SIGN[account.account_class] if account is not None else 1


def record_opening(
    db: Session, form: OpeningForm, *, confirmed: Iterable[str] = (), user_id: int | None = None
) -> PostResult:
    """期初餘額。對方科目固定是「期初權益」系統科目（找不到時丟 MissingSystemAccount）。"""
    _raise_if(OpeningEntry.check(form))
    equity = system_account(db, SystemAccountKey.OPENING_EQUITY)
    signs = {line.account_id: _sign(db, line.account_id) for line in form.lines}
    draft = OpeningEntry.build(form, opening_equity_id=equity.id, signs=signs)
    return ledger.post(db, draft, confirmed=confirmed, user_id=user_id)


def record_adjustment(
    db: Session,
    form: AdjustmentForm,
    *,
    confirmed: Iterable[str] = (),
    user_id: int | None = None,
) -> PostResult:
    """帳務調整。對方科目固定是「帳務調整」系統科目（找不到時丟 MissingSystemAccount）。"""
    _raise_if(AdjustmentEntry.check(form))
    adjustment = system_account(db, SystemAccountKey.ADJUSTMENT)
    draft = AdjustmentEntry.build(
        form, adjustment_account_id=adjustment.id, sign=_sign(db, form.account_id)
    )
    return ledger.post(db, draft, confirmed=confirmed, user_id=user_id)


def record_payroll(
    db: Session, form: ManualForm, *, confirmed: Iterable[str] = (), user_id: int | None = None
) -> PostResult:
    """薪資：txn_type = PAYROLL 的多分錄交易（R7）。"""
    return ledger.post(db, PayrollEntry.build(form), confirmed=confirmed, user_id=user_id)
