"""交易記錄的流程串接（P1 Step 4 決策 P1、P3、D9；Step 6 修改、刪除、還原與退款）。

P2 的 router 只呼叫這裡的 record_*／update_transaction／delete_transaction／restore_transaction，
不碰借貸邏輯、不自己挑 builder、不直接寫分錄。

每種交易的草稿產生流程（_draft_*，新增與修改共用）：
1. builder.check(form)：表單語意（V01、V06、必填欄位）。有問題就丟 ValidationFailed，不往下做。
2. 查詢需要的主資料：付款方式 → 實際扣款帳戶（P1）；轉入帳戶是不是信用卡（D9）；
   退款的原交易（U7）。
3. builder.build(...)：純結構轉換成 EntryDraft。
之後交給 ledger.post()（新增）或 ledger.update()（修改）；這兩個是唯一的寫入入口。
"""

from collections.abc import Callable, Iterable
from dataclasses import replace

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import PaymentMethod, Transaction
from app.models.enums import AccountSubtype, SystemAccountKey, TxnSource, TxnType
from app.repositories import accounts as account_repo
from app.repositories import ledger as ledger_repo
from app.services import ledger
from app.services.accounts import system_account
from app.services.drafts import EntryDraft
from app.services.entries import (
    AdjustmentEntry,
    AdjustmentForm,
    CardPaymentEntry,
    CardPaymentForm,
    ExpenseEntry,
    ExpenseForm,
    IncomeEntry,
    IncomeForm,
    ManualEntry,
    ManualForm,
    OpeningEntry,
    OpeningForm,
    PayrollEntry,
    RefundEntry,
    RefundForm,
    TransferEntry,
    TransferForm,
)
from app.services.errors import NotFound
from app.services.issues import Issue
from app.services.ledger import PostResult
from app.services.ledger_queries import NORMAL_SIGN
from app.services.txn_structure import CODE as STRUCTURE_CODE
from app.services.validation import (
    ValidationFailed,
    ValidationResult,
    main_posting,
    refund_original,
)


def _raise_if(issues: Iterable[Issue]) -> None:
    issues = tuple(issues)
    if issues:
        raise ValidationFailed(ValidationResult(errors=issues))


def _payment_method(db: Session, payment_method_id: int) -> PaymentMethod:
    pm = db.get(PaymentMethod, payment_method_id)
    if pm is None:
        _raise_if([Issue("V05", "找不到付款方式", field="payment_method_id")])
    return pm


def _sign(db: Session, account_id: int | None) -> int:
    """科目的正常方向正負號；找不到的科目先用 +1，交給 validation 的 V05 擋下。"""
    account = account_repo.get(db, account_id) if account_id is not None else None
    return NORMAL_SIGN[account.account_class] if account is not None else 1


# --- 草稿產生（新增與修改共用）------------------------------------------------------


def _draft_expense(db: Session, form: ExpenseForm) -> EntryDraft:
    _raise_if(ExpenseEntry.check(form))
    pm = _payment_method(db, form.payment_method_id)
    # 付款方式只是管道：LINE Pay → 郵局、聯邦信用卡 → 聯邦信用卡（負債）
    return ExpenseEntry.build(form, payment_account_id=pm.account_id)


def _draft_income(db: Session, form: IncomeForm) -> EntryDraft:
    _raise_if(IncomeEntry.check(form))
    return IncomeEntry.build(form)


def _draft_transfer(db: Session, form: TransferForm) -> EntryDraft:
    """轉入帳戶是信用卡時自動改成信用卡繳款（D9）。"""
    _raise_if(TransferEntry.check(form))
    to_account = account_repo.get(db, form.to_account_id)
    if to_account is not None and to_account.subtype == AccountSubtype.CREDIT_CARD:
        if form.fee_amount:
            # 不支援的交易組合（不是缺欄位），所以用結構錯誤代碼而不是 V05
            _raise_if([Issue(STRUCTURE_CODE, "信用卡繳款不支援手續費", field="fee_amount")])
        return _draft_card_payment(
            db,
            CardPaymentForm(
                txn_date=form.txn_date,
                amount=form.amount,
                card_account_id=form.to_account_id,
                from_account_id=form.from_account_id,
                description=form.description,
            ),
        )
    return TransferEntry.build(form)


def _draft_card_payment(db: Session, form: CardPaymentForm) -> EntryDraft:
    _raise_if(CardPaymentEntry.check(form))
    return CardPaymentEntry.build(form)


def _draft_opening(db: Session, form: OpeningForm) -> EntryDraft:
    """期初餘額。對方科目固定是「期初權益」系統科目（找不到時丟 MissingSystemAccount）。"""
    _raise_if(OpeningEntry.check(form))
    equity = system_account(db, SystemAccountKey.OPENING_EQUITY)
    signs = {line.account_id: _sign(db, line.account_id) for line in form.lines}
    return OpeningEntry.build(form, opening_equity_id=equity.id, signs=signs)


def _draft_adjustment(db: Session, form: AdjustmentForm) -> EntryDraft:
    """帳務調整。對方科目固定是「帳務調整」系統科目（找不到時丟 MissingSystemAccount）。"""
    _raise_if(AdjustmentEntry.check(form))
    adjustment = system_account(db, SystemAccountKey.ADJUSTMENT)
    return AdjustmentEntry.build(
        form, adjustment_account_id=adjustment.id, sign=_sign(db, form.account_id)
    )


def _draft_refund(db: Session, form: RefundForm) -> EntryDraft:
    """退款：支出科目、商家、不計入預算都從原交易帶入（U7）。"""
    _raise_if(RefundEntry.check(form))
    original, issues = refund_original(db, form.original_txn_id)
    _raise_if(issues)
    pm = _payment_method(db, form.payment_method_id)
    return RefundEntry.build(
        form,
        payment_account_id=pm.account_id,
        main_account_id=main_posting(original.postings).account_id,
        merchant_id=original.merchant_id,
        exclude_from_budget=original.exclude_from_budget,
    )


# --- 新增 -------------------------------------------------------------------------


SUBMISSION_PREFIX = "submit:"


def _tag_ids(tag_ids: Iterable[int] | None) -> tuple[int, ...] | None:
    """標籤 id 去重、排序（P2 Step 8A）。None 保持 None（不處理標籤）。"""
    return None if tag_ids is None else tuple(sorted(set(tag_ids)))


def _with_tags(draft: EntryDraft, tag_ids: Iterable[int] | None) -> EntryDraft:
    """把標籤補進草稿（builder 不知道標籤，和 source_ref 相同的做法）。"""
    return replace(draft, tag_ids=_tag_ids(tag_ids))


def submission_ref(token: str) -> str:
    """Web 一次性送出權杖 → transactions.source_ref（source = MANUAL；P2 Step 2 決策 A）。"""
    return f"{SUBMISSION_PREFIX}{token}"


def _replayed(db: Session, source_ref: str) -> PostResult | None:
    txn = ledger_repo.find_by_source_ref(db, TxnSource.MANUAL.value, source_ref)
    return PostResult(txn.id, (), replayed=True) if txn is not None else None


def find_replay(db: Session, submission_token: str) -> PostResult | None:
    """這個送出權杖是否已經建立過交易；是的話回傳 replayed 結果，否則 None。

    只讀不寫。Web 在處理任何會寫入的前置步驟（例如建立商家）之前先呼叫，
    避免已用過的權杖重送時產生副作用。record_expense() 內仍會再檢查一次，
    那才是權威檢查（搭配 UNIQUE(source, source_ref) 處理同時送出）。
    """
    return _replayed(db, submission_ref(submission_token))


def _post_once(
    db: Session,
    make_draft: Callable[[], EntryDraft],
    *,
    confirmed: Iterable[str],
    user_id: int | None,
    submission_token: str | None,
) -> PostResult:
    """新增交易的共用流程（P2 Step 5A 由 record_expense 抽出，行為不變）。

    submission_token 是 None（一般 service 呼叫，例如 CLI、測試）：直接 ledger.post()，不做防重。
    有 submission_token（Web 表單的一次性權杖，規格 9.3「重複送出」）：
    - 這個權杖已經建立過交易（含之後被軟刪除的）→ 不驗證、不寫入、不寫稽核，
      回傳第一次建立的 transaction_id（replayed=True）。草稿也不會產生（make_draft 不被呼叫）。
    - 否則以 source = MANUAL、source_ref = "submit:<token>" 寫入；兩個請求同時送出時，
      後到的會撞到 UNIQUE(source, source_ref)，rollback 後改回傳先寫入的那筆。
    """
    if submission_token is None:
        return ledger.post(db, make_draft(), confirmed=confirmed, user_id=user_id)

    ref = submission_ref(submission_token)
    replay = _replayed(db, ref)
    if replay is not None:
        return replay
    draft = replace(make_draft(), source_ref=ref)
    try:
        return ledger.post(db, draft, confirmed=confirmed, user_id=user_id)
    except IntegrityError:
        # ledger.post 已經 rollback；確認是不是同一個權杖已經被寫入，是的話視為重送
        replay = _replayed(db, ref)
        if replay is None:
            raise
        return replay


def record_expense(
    db: Session,
    form: ExpenseForm,
    *,
    confirmed: Iterable[str] = (),
    user_id: int | None = None,
    submission_token: str | None = None,
    tag_ids: Iterable[int] | None = None,
) -> PostResult:
    """submission_token 的行為見 _post_once()；tag_ids 見 drafts.EntryDraft。"""
    return _post_once(
        db,
        lambda: _with_tags(_draft_expense(db, form), tag_ids),
        confirmed=confirmed,
        user_id=user_id,
        submission_token=submission_token,
    )


def record_income(
    db: Session,
    form: IncomeForm,
    *,
    confirmed: Iterable[str] = (),
    user_id: int | None = None,
    submission_token: str | None = None,
    tag_ids: Iterable[int] | None = None,
) -> PostResult:
    """submission_token 的行為見 _post_once()；tag_ids 見 drafts.EntryDraft。"""
    return _post_once(
        db,
        lambda: _with_tags(_draft_income(db, form), tag_ids),
        confirmed=confirmed,
        user_id=user_id,
        submission_token=submission_token,
    )


def record_transfer(
    db: Session,
    form: TransferForm,
    *,
    confirmed: Iterable[str] = (),
    user_id: int | None = None,
    submission_token: str | None = None,
    tag_ids: Iterable[int] | None = None,
) -> PostResult:
    """帳戶間轉帳。轉入帳戶是信用卡時自動改成信用卡繳款（D9）；
    繳卡費同樣帶著這個送出權杖寫入。submission_token 的行為見 _post_once()。"""
    return _post_once(
        db,
        lambda: _with_tags(_draft_transfer(db, form), tag_ids),
        confirmed=confirmed,
        user_id=user_id,
        submission_token=submission_token,
    )


def record_card_payment(
    db: Session,
    form: CardPaymentForm,
    *,
    confirmed: Iterable[str] = (),
    user_id: int | None = None,
) -> PostResult:
    return ledger.post(db, _draft_card_payment(db, form), confirmed=confirmed, user_id=user_id)


def record_opening(
    db: Session, form: OpeningForm, *, confirmed: Iterable[str] = (), user_id: int | None = None
) -> PostResult:
    return ledger.post(db, _draft_opening(db, form), confirmed=confirmed, user_id=user_id)


def record_adjustment(
    db: Session,
    form: AdjustmentForm,
    *,
    confirmed: Iterable[str] = (),
    user_id: int | None = None,
) -> PostResult:
    return ledger.post(db, _draft_adjustment(db, form), confirmed=confirmed, user_id=user_id)


def record_payroll(
    db: Session, form: ManualForm, *, confirmed: Iterable[str] = (), user_id: int | None = None
) -> PostResult:
    """薪資：txn_type = PAYROLL 的多分錄交易（R7）。"""
    return ledger.post(db, PayrollEntry.build(form), confirmed=confirmed, user_id=user_id)


def record_refund(
    db: Session,
    form: RefundForm,
    *,
    confirmed: Iterable[str] = (),
    user_id: int | None = None,
    submission_token: str | None = None,
) -> PostResult:
    """退款。submission_token 同 record_expense（P2 Step 7 接上同一套 _post_once）。"""
    return _post_once(
        db,
        lambda: _draft_refund(db, form),
        confirmed=confirmed,
        user_id=user_id,
        submission_token=submission_token,
    )


# --- 修改、刪除、還原（Step 6）---------------------------------------------------------

_DRAFTERS: dict[type, Callable[[Session, object], EntryDraft]] = {
    ExpenseForm: _draft_expense,
    IncomeForm: _draft_income,
    TransferForm: _draft_transfer,
    CardPaymentForm: _draft_card_payment,
    OpeningForm: _draft_opening,
    AdjustmentForm: _draft_adjustment,
    RefundForm: _draft_refund,
}


def update_transaction(
    db: Session,
    txn_id: int,
    form: object,
    *,
    confirmed: Iterable[str] = (),
    user_id: int | None = None,
    tag_ids: Iterable[int] | None = None,
) -> PostResult:
    """用表單修改既有交易。表單類型決定交易類型；與原交易不同時由 ledger.update
    回報 TXN_TYPE_IMMUTABLE（U1）。
    tag_ids（P2 Step 8A）：None = 保留原本的標籤；() = 移除全部；其他 = 整組替換。
    ManualForm 依原交易決定是 MANUAL 還是 PAYROLL。"""
    if isinstance(form, ManualForm):
        txn = db.get(Transaction, txn_id)
        if txn is None:
            raise NotFound("交易", txn_id)
        builder = PayrollEntry if txn.txn_type == TxnType.PAYROLL else ManualEntry
        draft = builder.build(form)
    else:
        draft = _DRAFTERS[type(form)](db, form)
    draft = _with_tags(draft, tag_ids)
    return ledger.update(db, txn_id, draft, confirmed=confirmed, user_id=user_id)


def delete_transaction(db: Session, txn_id: int, *, user_id: int | None = None) -> PostResult:
    return ledger.soft_delete(db, txn_id, user_id=user_id)


def restore_transaction(db: Session, txn_id: int, *, user_id: int | None = None) -> PostResult:
    return ledger.restore(db, txn_id, user_id=user_id)
