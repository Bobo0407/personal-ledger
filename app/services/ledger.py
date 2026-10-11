"""LedgerService：唯一的分錄寫入入口（規格 7.1 ledger.py、7.4）。

post() 的流程（P1 Step 3）：
0. 呼叫端的 Session 不能有尚未提交的變更（new／dirty／deleted），否則拒絕，
   避免把別人的變更一起 commit 進去（Q3）。
1. validate()：只讀不寫。
2. 有 error → ValidationFailed；有尚未確認的項目 → ConfirmationRequired。兩者都不寫入任何資料。
3. 寫入 transactions、postings（line_no 依草稿順序從 1 開始）→ flush 取得 id
   → audit.record(CREATE) → commit。任何例外都整批回滾。
4. 回傳 PostResult(transaction_id, notices)。

只有這個模組可以建立 Posting（架構守門測試把關）。
update、soft_delete、restore 在 Step 6；confirm_draft 在 V1.5。
"""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Account, Posting, Transaction
from app.models.enums import AuditAction, TxnStatus
from app.services import audit
from app.services.drafts import EntryDraft
from app.services.validation import (
    ConfirmationRequired,
    Issue,
    ValidationFailed,
    validate,
)

AUDIT_ENTITY = "transaction"


class SessionNotClean(RuntimeError):
    """呼叫 post() 時 Session 還有尚未提交的變更。這是程式錯誤，不是使用者輸入錯誤。"""


@dataclass(frozen=True)
class PostResult:
    transaction_id: int
    notices: tuple[Issue, ...]


def _ensure_clean(db: Session) -> None:
    if db.new or db.dirty or db.deleted:
        raise SessionNotClean("Session 有尚未提交的變更，請先 commit 或 rollback 再寫入分錄")


def transaction_snapshot(db: Session, txn: Transaction) -> dict[str, Any]:
    """稽核紀錄用的交易快照：表頭欄位 + 每筆分錄。"""
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
    if result.errors:
        raise ValidationFailed(result)
    confirmed = set(confirmed)
    pending = tuple(i for i in result.confirmations if i.code not in confirmed)
    if pending:
        raise ConfirmationRequired(result, pending)

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
        txn.postings = [
            Posting(
                line_no=line_no,
                account_id=p.account_id,
                amount=p.amount,
                line_role=p.line_role,
                original_currency=p.original_currency,
                original_amount_minor=p.original_amount_minor,
                memo=p.memo,
            )
            for line_no, p in enumerate(draft.postings, start=1)
        ]
        db.add(txn)
        db.flush()
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
    except Exception:
        db.rollback()
        raise
    return PostResult(txn.id, result.notices)
