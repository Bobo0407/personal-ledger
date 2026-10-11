"""分錄草稿：EntryBuilder 的輸出、LedgerService 的輸入（規格第 7 節）。

- Builder 只做結構轉換，不查資料庫、不做驗證；所有業務規則在 validation.py。
  所以尚未選科目的分錄也可以轉成草稿（account_id=None），由 V05 在驗證階段擋下。
- amount 帶正負號：借方為正、貸方為負，單位為新台幣元。
- postings 的順序就是寫入後的 line_no（由 LedgerService 從 1 開始編號）。
- 沒有 status：P1 一律寫入 POSTED（DRAFT 的確認入帳在 V1.5）。
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
