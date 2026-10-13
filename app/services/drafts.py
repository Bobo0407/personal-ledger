"""分錄草稿：EntryBuilder 的輸出、LedgerService 的輸入（規格第 7 節）。

- Builder 只做結構轉換，不查資料庫、不做驗證；所有業務規則在 validation.py。
  所以尚未選科目的分錄也可以轉成草稿（account_id=None），由 V05 在驗證階段擋下。
- amount 帶正負號：借方為正、貸方為負，單位為新台幣元。
- postings 的順序就是寫入後的 line_no（由 LedgerService 從 1 開始編號）。
- 沒有 status：P1 一律寫入 POSTED（DRAFT 的確認入帳在 V1.5）。
- tag_ids（P2 Step 8A、ADR-007）：交易表頭層級的標籤，不是分錄、不影響借貸。
  Builder 不知道標籤，由 transactions 用 replace() 補上（和 source_ref 相同）。
  None = 這次不處理標籤（新增時就是沒有標籤；修改時保留原本的標籤）；
  () = 沒有標籤／移除全部；非空 tuple = 整組替換。由 transactions 去重並排序。
"""

from dataclasses import dataclass
from datetime import date

from app.models.enums import TxnSource


@dataclass(frozen=True)
class PostingDraft:
    account_id: int | None
    amount: int
    line_role: str | None = None
    original_currency: str | None = None
    original_amount_minor: int | None = None
    memo: str | None = None


@dataclass(frozen=True)
class EntryDraft:
    txn_date: date | None
    txn_type: str
    postings: tuple[PostingDraft, ...]
    description: str | None = None
    merchant_id: int | None = None
    payment_method_id: int | None = None
    voucher_type: str | None = None
    invoice_no: str | None = None
    exclude_from_budget: bool = False
    related_txn_id: int | None = None
    source: str = TxnSource.MANUAL.value
    source_ref: str | None = None
    tag_ids: tuple[int, ...] | None = None


@dataclass(frozen=True)
class ExistingTransaction:
    """修改既有交易時，原版本的關鍵內容（Step 6）。validate() 用它做新舊版本比對：
    D14 只檢查被改動的分錄、V10／V11 只在相關欄位改變時重問、V13 用淨變動判斷、
    排除自己的 D12／V07，以及原交易有退款時的鎖定（REFUND_LOCKED）。"""

    id: int
    txn_type: str
    txn_date: date
    payment_method_id: int | None
    merchant_id: int | None
    exclude_from_budget: bool
    related_txn_id: int | None
    invoice_no: str | None
    postings: tuple[PostingDraft, ...]
    tag_ids: tuple[int, ...] = ()  # 修改前的標籤（由呼叫端在修改前從資料庫查好傳入）

    @classmethod
    def from_model(cls, txn, tag_ids: tuple[int, ...] = ()) -> "ExistingTransaction":
        return cls(
            id=txn.id,
            txn_type=txn.txn_type,
            txn_date=date.fromisoformat(txn.txn_date),
            payment_method_id=txn.payment_method_id,
            merchant_id=txn.merchant_id,
            exclude_from_budget=txn.exclude_from_budget,
            related_txn_id=txn.related_txn_id,
            invoice_no=txn.invoice_no,
            postings=tuple(posting_draft_of(p) for p in txn.postings),
            tag_ids=tuple(tag_ids),
        )


def posting_draft_of(posting) -> PostingDraft:
    return PostingDraft(
        account_id=posting.account_id,
        amount=posting.amount,
        line_role=posting.line_role,
        original_currency=posting.original_currency,
        original_amount_minor=posting.original_amount_minor,
        memo=posting.memo,
    )


def draft_from_model(txn) -> EntryDraft:
    """把資料庫中的交易轉回草稿（還原時重新檢查用）。"""
    return EntryDraft(
        txn_date=date.fromisoformat(txn.txn_date),
        txn_type=txn.txn_type,
        postings=tuple(posting_draft_of(p) for p in txn.postings),
        description=txn.description,
        merchant_id=txn.merchant_id,
        payment_method_id=txn.payment_method_id,
        voucher_type=txn.voucher_type,
        invoice_no=txn.invoice_no,
        exclude_from_budget=txn.exclude_from_budget,
        related_txn_id=txn.related_txn_id,
        source=txn.source,
        source_ref=txn.source_ref,
    )
