"""LedgerService：唯一的分錄寫入入口（規格 7.1 ledger.py、7.4）。

post() 的流程（P1 Step 3）：
0. 呼叫端的 Session 不能有尚未提交的變更（new／dirty／deleted），否則拒絕，
   避免把別人的變更一起 commit 進去（Q3）。
1. validate()：只讀不寫。
2. 有 error → ValidationFailed；有尚未確認的項目 → ConfirmationRequired。兩者都不寫入任何資料。
3. 寫入 transactions、postings（line_no 依草稿順序從 1 開始）→ flush 取得 id
   → audit.record(CREATE) → commit。任何例外都整批回滾。
4. 回傳 PostResult(transaction_id, notices)。
5. 寫入時撞到發票部分唯一索引（V08 的最後防線，P2 Step 2）→ 轉成 V08 ValidationFailed；
   其他 IntegrityError 原樣拋出。post() 與 update() 共用，restore() 不變。

Step 6 的生命週期（全部在同一個資料庫交易內：異動 + 稽核一起提交，任何錯誤整批回滾）：
- update()：交易類型不能改（TXN_TYPE_IMMUTABLE）、已刪除的要先還原（TXN_DELETED）。
  驗證時帶入修改前的內容（D14、U3、U4、U10）；通過後刪掉舊分錄、寫入新分錄（line_no 重新從 1 編），
  表頭的 status、source、source_ref 不變。稽核 UPDATE 的 before／after 是完整快照。
- soft_delete()：只設 deleted_at，分錄保留（之後由 active_postings() 自動排除）。
  已有有效退款的支出不能刪除（REFUND_EXISTS），要先刪退款。
- restore()：清除 deleted_at，重新檢查（validate_restore）。
餘額不另外存，刪除、還原後的餘額一律由 active_postings() 即時計算。
confirm_draft 在 V1.5。
"""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import clock
from app.models import Account, Posting, Transaction, TransactionTag
from app.models.enums import AuditAction, TxnStatus, TxnType
from app.repositories import ledger as ledger_repo
from app.repositories import tags as tag_repo
from app.services import audit
from app.services.drafts import EntryDraft, ExistingTransaction, PostingDraft
from app.services.errors import NotFound
from app.services.validation import (
    ConfirmationRequired,
    Issue,
    ValidationFailed,
    ValidationResult,
    invoice_taken_issue,
    validate,
    validate_restore,
)

AUDIT_ENTITY = "transaction"


class SessionNotClean(RuntimeError):
    """呼叫 post() 時 Session 還有尚未提交的變更。這是程式錯誤，不是使用者輸入錯誤。"""


@dataclass(frozen=True)
class PostResult:
    transaction_id: int
    notices: tuple[Issue, ...]
    # True：同一個送出權杖已經建立過交易，這次沒有寫入，transaction_id 是第一次建立的那筆
    # （P2 Step 2，transactions.record_expense 的 submission_token）
    replayed: bool = False


def _ensure_clean(db: Session) -> None:
    if db.new or db.dirty or db.deleted:
        raise SessionNotClean("Session 有尚未提交的變更，請先 commit 或 rollback 再寫入分錄")


def transaction_snapshot(db: Session, txn: Transaction) -> dict[str, Any]:
    """稽核紀錄用的交易快照：表頭欄位 + 每筆分錄 + 標籤 id。

    tag_ids（P2 Step 8A）用查詢讀資料庫目前的 transaction_tags（Transaction 沒有標籤
    relationship，不會讀到過期的 ORM 集合）；呼叫端要在寫入標籤後先 flush 再取快照。"""
    ids = {p.account_id for p in txn.postings}
    codes = dict(db.execute(select(Account.id, Account.code).where(Account.id.in_(ids))).all())
    return {
        "id": txn.id,
        "txn_date": txn.txn_date,
        "txn_type": txn.txn_type,
        "status": txn.status,
        "description": txn.description,
        "merchant_id": txn.merchant_id,
        "payment_method_id": txn.payment_method_id,
        "voucher_type": txn.voucher_type,
        "invoice_no": txn.invoice_no,
        "exclude_from_budget": txn.exclude_from_budget,
        "related_txn_id": txn.related_txn_id,
        "source": txn.source,
        "source_ref": txn.source_ref,
        "deleted_at": txn.deleted_at,
        "tag_ids": list(tag_repo.ids_for_transaction(db, txn.id)),
        "postings": [
            {
                "line_no": p.line_no,
                "account_id": p.account_id,
                "account_code": codes.get(p.account_id),
                "amount": p.amount,
                "line_role": p.line_role,
                "original_currency": p.original_currency,
                "original_amount_minor": p.original_amount_minor,
                "memo": p.memo,
            }
            for p in txn.postings
        ],
    }


def _fail(code: str, message: str, field: str | None = None) -> ValidationFailed:
    return ValidationFailed(ValidationResult(errors=(Issue(code, message, field=field),)))


def _raise_for(result: ValidationResult, confirmed: Iterable[str]) -> None:
    if result.errors:
        raise ValidationFailed(result)
    confirmed = set(confirmed)
    pending = tuple(i for i in result.confirmations if i.code not in confirmed)
    if pending:
        raise ConfirmationRequired(result, pending)


def _raise_if_invoice_taken(db: Session, draft: EntryDraft, exclude_txn_id: int | None) -> None:
    """V08 最後防線：寫入時撞到發票部分唯一索引（例如兩個請求同時送出同一張發票）。

    不解析例外訊息：rollback 之後直接查「這個發票號碼現在是否被其他未刪除交易占用」。
    - 被占用 → 轉成 V08 ValidationFailed。
    - 占用者是同一個送出權杖建立的（同一筆重送）→ 不轉換，交給 transactions 依權杖處理。
    - 沒有被占用 → 不是發票衝突，呼叫端原樣拋出原本的 IntegrityError。
    """
    if draft.invoice_no is None:
        return
    owner = ledger_repo.active_invoice_owner(db, draft.invoice_no, exclude_txn_id)
    if owner is None:
        return
    if draft.source_ref is not None and owner.source_ref == draft.source_ref:
        return
    raise ValidationFailed(ValidationResult(errors=(invoice_taken_issue(owner),)))


def _new_postings(postings: tuple[PostingDraft, ...]) -> list[Posting]:
    return [
        Posting(
            line_no=line_no,
            account_id=p.account_id,
            amount=p.amount,
            line_role=p.line_role,
            original_currency=p.original_currency,
            original_amount_minor=p.original_amount_minor,
            memo=p.memo,
        )
        for line_no, p in enumerate(postings, start=1)
    ]


def _write_tags(db: Session, txn_id: int, tag_ids: tuple[int, ...] | None) -> None:
    """寫入或整組替換交易的標籤（P2 Step 8A、ADR-007）。只有 LedgerService 呼叫。

    None：不碰現有關聯。() 或非空：整組替換。只刪掉不再需要的、只新增原本沒有的，
    同一個 (transaction_id, tag_id) 不會在同一次 flush 裡先刪後加。最後明確 flush，
    讓接下來的稽核快照讀到資料庫的真實狀態。"""
    if tag_ids is None:
        return
    wanted = set(tag_ids)
    current = tag_repo.links_for_transaction(db, txn_id)
    for link in current:
        if link.tag_id not in wanted:
            db.delete(link)
    existing = {link.tag_id for link in current}
    db.add_all(
        TransactionTag(transaction_id=txn_id, tag_id=tag_id) for tag_id in sorted(wanted - existing)
    )
    db.flush()


def _get(db: Session, txn_id: int) -> Transaction:
    txn = db.get(Transaction, txn_id)
    if txn is None:
        raise NotFound("交易", txn_id)
    return txn


def post(
    db: Session,
    draft: EntryDraft,
    *,
    confirmed: Iterable[str] = (),
    user_id: int | None = None,
) -> PostResult:
    """驗證並寫入一筆 POSTED 交易。confirmed 是使用者已確認的規則代碼，例如 {"V10"}。"""
    _ensure_clean(db)

    result = validate(db, draft)
    _raise_for(result, confirmed)

    try:
        txn = Transaction(
            txn_date=draft.txn_date.isoformat(),
            txn_type=draft.txn_type,
            status=TxnStatus.POSTED.value,
            description=draft.description,
            merchant_id=draft.merchant_id,
            payment_method_id=draft.payment_method_id,
            voucher_type=draft.voucher_type,
            invoice_no=draft.invoice_no,
            exclude_from_budget=draft.exclude_from_budget,
            related_txn_id=draft.related_txn_id,
            source=draft.source,
            source_ref=draft.source_ref,
        )
        txn.postings = _new_postings(draft.postings)
        db.add(txn)
        db.flush()
        # 規格 7.4：transactions、postings → transaction_tags → audit_logs → 提交
        _write_tags(db, txn.id, draft.tag_ids)
        audit.record(
            db,
            entity=AUDIT_ENTITY,
            entity_id=txn.id,
            action=AuditAction.CREATE.value,
            before=None,
            after=transaction_snapshot(db, txn),
            user_id=user_id,
        )
        db.commit()
    except IntegrityError:
        db.rollback()
        _raise_if_invoice_taken(db, draft, None)
        raise
    except Exception:
        db.rollback()
        raise
    return PostResult(txn.id, result.notices)


def update(
    db: Session,
    txn_id: int,
    draft: EntryDraft,
    *,
    confirmed: Iterable[str] = (),
    user_id: int | None = None,
) -> PostResult:
    """修改一筆有效交易。txn_id 不存在 → NotFound。"""
    _ensure_clean(db)
    txn = _get(db, txn_id)
    if txn.deleted_at is not None:
        raise _fail("TXN_DELETED", "這筆交易已刪除，請先還原再修改")
    if draft.txn_type != txn.txn_type:
        raise _fail("TXN_TYPE_IMMUTABLE", "交易類型不能修改；請刪除後重新記一筆")

    # 修改前就從資料庫取得原本的標籤（驗證「沒動的停用標籤可以保留」要用）
    existing = ExistingTransaction.from_model(txn, tag_repo.ids_for_transaction(db, txn.id))
    result = validate(db, draft, existing)
    _raise_for(result, confirmed)

    try:
        before = transaction_snapshot(db, txn)
        txn.txn_date = draft.txn_date.isoformat()
        txn.description = draft.description
        txn.merchant_id = draft.merchant_id
        txn.payment_method_id = draft.payment_method_id
        txn.voucher_type = draft.voucher_type
        txn.invoice_no = draft.invoice_no
        txn.exclude_from_budget = draft.exclude_from_budget
        txn.related_txn_id = draft.related_txn_id
        # 先刪掉舊分錄再寫入新分錄，避免 (transaction_id, line_no) 唯一鍵衝突
        txn.postings.clear()
        db.flush()
        txn.postings.extend(_new_postings(draft.postings))
        db.flush()
        _write_tags(db, txn.id, draft.tag_ids)
        audit.record(
            db,
            entity=AUDIT_ENTITY,
            entity_id=txn.id,
            action=AuditAction.UPDATE.value,
            before=before,
            after=transaction_snapshot(db, txn),
            user_id=user_id,
        )
        db.commit()
    except IntegrityError:
        db.rollback()
        _raise_if_invoice_taken(db, draft, txn_id)
        raise
    except Exception:
        db.rollback()
        raise
    return PostResult(txn.id, result.notices)


def soft_delete(db: Session, txn_id: int, *, user_id: int | None = None) -> None:
    """軟刪除：只設 deleted_at，分錄保留。"""
    _ensure_clean(db)
    txn = _get(db, txn_id)
    if txn.deleted_at is not None:
        raise _fail("TXN_DELETED", "這筆交易已經刪除")
    if txn.txn_type == TxnType.EXPENSE and ledger_repo.active_refunded_amount(db, txn.id) > 0:
        raise _fail("REFUND_EXISTS", "這筆支出已有退款，請先刪除退款")
    try:
        before = transaction_snapshot(db, txn)
        txn.deleted_at = clock.now_iso()
        db.flush()
        audit.record(
            db,
            entity=AUDIT_ENTITY,
            entity_id=txn.id,
            action=AuditAction.DELETE.value,
            before=before,
            after=transaction_snapshot(db, txn),
            user_id=user_id,
        )
        db.commit()
    except Exception:
        db.rollback()
        raise


def restore(db: Session, txn_id: int, *, user_id: int | None = None) -> PostResult:
    """還原已刪除的交易。重新檢查放回帳上會不會壞（validate_restore），不需要確認。"""
    _ensure_clean(db)
    txn = _get(db, txn_id)
    if txn.deleted_at is None:
        raise _fail("TXN_NOT_DELETED", "這筆交易沒有被刪除")
    result = validate_restore(db, txn)
    if result.errors:
        raise ValidationFailed(result)
    try:
        before = transaction_snapshot(db, txn)
        txn.deleted_at = None
        db.flush()
        audit.record(
            db,
            entity=AUDIT_ENTITY,
            entity_id=txn.id,
            action=AuditAction.RESTORE.value,
            before=before,
            after=transaction_snapshot(db, txn),
            user_id=user_id,
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return PostResult(txn.id, result.notices)
