"""收入、轉帳、繳卡費的修改條件與預填值（P2 Step 5B）。只讀，不寫入、不做帳務判斷。

修改表單只能表達固定的幾個欄位，所以只開放「表單能完整還原」的交易：
1. 從既有分錄還原成表單值（收入／轉帳／繳卡費各自的 Form）。
2. 用 P1 的 builder 重建分錄（IncomeEntry、TransferEntry、CardPaymentEntry；純函式，不查資料庫）。
3. 和既有分錄逐筆比對完整內容（科目、金額、角色、原幣、memo；不看順序）。
不一致就不開放修改：用修改表單儲存會默默改掉表單看不到的內容。
表頭有表單沒有的欄位（商家、付款方式、憑證、發票、關聯交易、不計入預算）也不開放。

這裡只判斷「表單能不能完整修改」；帳務規則（V06、V10、V11、V13、TXN_STRUCTURE、
TXN_TYPE_IMMUTABLE、D9……）仍全部在 P1 的 update_transaction。
繳卡費在畫面上是「轉入信用卡的轉帳」，預填值用轉帳表單的欄位名稱（轉入 = 信用卡）。
"""

from collections import Counter
from collections.abc import Callable, Iterable

from app.models import Transaction
from app.models.enums import LineRole, TxnStatus, TxnType
from app.services.drafts import EntryDraft
from app.services.entries import (
    CardPaymentEntry,
    CardPaymentForm,
    IncomeEntry,
    IncomeForm,
    TransferEntry,
    TransferForm,
)

EDITABLE_TYPES = frozenset({TxnType.INCOME, TxnType.TRANSFER, TxnType.CARD_PAYMENT})

NOT_REBUILDABLE = "這筆交易的分錄不是表單可以完整修改的格式"
EXTRA_HEADER = "這筆交易有表單沒有的欄位，無法用表單完整修改"
INCOME_WITH_MERCHANT = "這筆收入有商家，收入表單沒有商家欄位，無法完整修改"


def _content(postings: Iterable) -> Counter:
    """分錄的完整內容（不看順序、不看 line_no）。"""
    return Counter(
        (
            p.account_id,
            p.amount,
            p.line_role,
            p.original_currency,
            p.original_amount_minor,
            p.memo,
        )
        for p in postings
    )


def _single(postings: list, keep: Callable[[object], bool]):
    """剛好一筆符合條件的分錄；零筆或多筆都回傳 None（之後重建比對一定不會通過）。"""
    found = [p for p in postings if keep(p)]
    return found[0] if len(found) == 1 else None


def _optional(postings: list, keep: Callable[[object], bool]):
    found = [p for p in postings if keep(p)]
    return found[0] if found else None


def _income(txn: Transaction) -> tuple[IncomeForm, EntryDraft] | None:
    main = _single(txn.postings, lambda p: p.line_role == LineRole.MAIN)
    receive = _single(txn.postings, lambda p: p.line_role is None)
    if main is None or receive is None:
        return None
    form = IncomeForm(
        txn_date=None,
        amount=-main.amount,
        account_id=main.account_id,
        receive_account_id=receive.account_id,
    )
    return form, IncomeEntry.build(form)


def _transfer(txn: Transaction) -> tuple[TransferForm, EntryDraft] | None:
    to = _single(txn.postings, lambda p: p.line_role is None and p.amount > 0)
    frm = _single(txn.postings, lambda p: p.line_role is None and p.amount < 0)
    if to is None or frm is None:
        return None
    fee = _optional(txn.postings, lambda p: p.line_role == LineRole.FEE)
    form = TransferForm(
        txn_date=None,
        amount=to.amount,
        from_account_id=frm.account_id,
        to_account_id=to.account_id,
        fee_amount=fee.amount if fee is not None else None,
        fee_account_id=fee.account_id if fee is not None else None,
    )
    return form, TransferEntry.build(form)


def _card_payment(txn: Transaction) -> tuple[TransferForm, EntryDraft] | None:
    card = _single(txn.postings, lambda p: p.line_role is None and p.amount > 0)
    frm = _single(txn.postings, lambda p: p.line_role is None and p.amount < 0)
    if card is None or frm is None:
        return None
    built = CardPaymentEntry.build(
        CardPaymentForm(
            txn_date=None,
            amount=card.amount,
            card_account_id=card.account_id,
            from_account_id=frm.account_id,
        )
    )
    form = TransferForm(
        txn_date=None,
        amount=card.amount,
        from_account_id=frm.account_id,
        to_account_id=card.account_id,
    )
    return form, built


_EXTRACTORS = {
    TxnType.INCOME: _income,
    TxnType.TRANSFER: _transfer,
    TxnType.CARD_PAYMENT: _card_payment,
}


def _rebuild(txn: Transaction):
    """還原成表單 → builder 重建 → 完整比對；無法完整還原時回傳 None。"""
    rebuilt = _EXTRACTORS[txn.txn_type](txn)
    if rebuilt is None:
        return None
    form, draft = rebuilt
    if _content(draft.postings) != _content(txn.postings):
        return None
    return form


def edit_blockers(txn: Transaction) -> tuple[str, ...]:
    """收入、轉帳、繳卡費不能用表單修改的原因；空 tuple 代表可以修改。"""
    if txn.txn_type not in EDITABLE_TYPES:
        return ("這類交易目前不提供修改",)
    if txn.deleted_at is not None:
        return ("已刪除的交易要先還原才能修改",)
    if txn.status != TxnStatus.POSTED:
        return ("草稿交易不提供修改",)
    if txn.txn_type == TxnType.INCOME and txn.merchant_id is not None:
        return (INCOME_WITH_MERCHANT,)
    if (
        txn.merchant_id is not None
        or txn.payment_method_id is not None
        or txn.voucher_type is not None
        or txn.invoice_no is not None
        or txn.related_txn_id is not None
        or txn.exclude_from_budget
    ):
        return (EXTRA_HEADER,)
    if _rebuild(txn) is None:
        return (NOT_REBUILDABLE,)
    return ()


def _text(value: int | None) -> str:
    return "" if value is None else str(value)


def edit_values(txn: Transaction) -> dict[str, str]:
    """修改表單的預填值。呼叫端要先確認 edit_blockers(txn) 為空。"""
    form = _rebuild(txn)
    values = {"txn_date": txn.txn_date, "amount": _text(form.amount)}
    if isinstance(form, IncomeForm):
        values |= {
            "account_id": _text(form.account_id),
            "receive_account_id": _text(form.receive_account_id),
        }
    else:
        values |= {
            "from_account_id": _text(form.from_account_id),
            "to_account_id": _text(form.to_account_id),
            "fee_amount": _text(form.fee_amount),
            "fee_account_id": _text(form.fee_account_id),
        }
    values["description"] = txn.description or ""
    return values
