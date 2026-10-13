"""交易明細與「快速記帳格式支出」的修改條件（P2 Step 4A）。

只讀，不寫入。明細要能讀到已刪除的交易（規格 5.4：交易的「已刪除」檢視與還原），
所以這裡用 id 直接讀一筆交易；這不是帳務加總，不經過 active_postings()。

分錄顯示不用「借／貸」（規格 1.5），也不給畫面帶正負號的金額：
每筆分錄依科目的正常餘額方向（ledger_queries.NORMAL_SIGN）換算成「增加／減少 + 金額」，
例如「郵局 減少 120」「聯邦信用卡 增加 120」（欠款增加）。template 不判斷正負或科目類別。

修改條件（edit guard）：Web 修改表單只有快速記帳的欄位，拿它修改其他形狀的支出
會默默刪掉手續費、利息、外幣或改掉「不計入預算」，所以只開放「快速記帳格式」的 EXPENSE。
這是 4A 唯一新增的判斷；帳務規則（V08、V10–V12、REFUND_LOCKED……）仍全部由 P1 負責。
P2 Step 5B：收入、轉帳、繳卡費的修改條件與預填值在 services/txn_edit.py
（還原 → builder 重建 → 比對）。
"""

from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy.orm import Session

from app.models import Merchant, PaymentMethod, Transaction
from app.models.enums import LineRole, TxnStatus, TxnType
from app.repositories import accounts as account_repo
from app.repositories import ledger as ledger_repo
from app.services import txn_edit
from app.services.entry_options import VOUCHER_LABELS
from app.services.errors import NotFound, RuleViolation
from app.services.ledger_queries import NORMAL_SIGN
from app.services.search import TXN_TYPE_LABELS

NOT_EDITABLE = "NOT_EDITABLE"

ROLE_LABELS = {LineRole.FEE: "手續費", LineRole.INTEREST: "分期利息"}


@dataclass(frozen=True)
class PostingLine:
    account_name: str
    effect: str  # 「增加」或「減少」（依科目正常餘額方向）
    amount: int  # 絕對值
    role_label: str | None  # 手續費、分期利息；MAIN 與一般分錄不另外標示


@dataclass(frozen=True)
class TxnDetail:
    id: int
    txn_date: date
    txn_type: str
    type_label: str
    is_deleted: bool
    deleted_at: datetime | None
    merchant_name: str | None
    payment_method_name: str | None
    description: str | None
    voucher_label: str | None
    invoice_no: str | None
    magnitude: int  # 交易規模：正數分錄加總（與列表相同定義）
    lines: tuple[PostingLine, ...]
    refunded_amount: int  # EXPENSE：有效退款合計；其他類型為 0
    related_txn_id: int | None  # REFUND：原交易
    edit_blockers: tuple[str, ...]  # 空 = 可以用快速記帳表單修改
    delete_blockers: tuple[str, ...]  # 空 = 可以刪除（已刪除的交易不適用）


def _get(db: Session, txn_id: int) -> Transaction:
    txn = db.get(Transaction, txn_id)
    if txn is None:
        raise NotFound("交易", txn_id)
    return txn


def _line(db: Session, posting) -> PostingLine:
    account = account_repo.get(db, posting.account_id)
    signed = posting.amount * NORMAL_SIGN[account.account_class]
    return PostingLine(
        account_name=account.name,
        effect="增加" if signed > 0 else "減少",
        amount=abs(posting.amount),
        role_label=ROLE_LABELS.get(posting.line_role),
    )


def edit_blockers(txn: Transaction) -> tuple[str, ...]:
    """不能用表單修改的原因；空 tuple 代表可以修改。
    支出：快速記帳格式（4A）；收入、轉帳、繳卡費：txn_edit（5B）；其他類型不提供修改。"""
    if txn.txn_type != TxnType.EXPENSE:
        return txn_edit.edit_blockers(txn)
    if txn.deleted_at is not None:
        return ("已刪除的交易要先還原才能修改",)
    if txn.status != TxnStatus.POSTED:
        return ("草稿交易不提供修改",)
    reasons = []
    roles = [p.line_role for p in txn.postings]
    if LineRole.FEE in roles:
        reasons.append("含手續費")
    if LineRole.INTEREST in roles:
        reasons.append("含分期利息")
    if any(
        p.original_currency is not None or p.original_amount_minor is not None for p in txn.postings
    ):
        reasons.append("含外幣資料")
    if txn.exclude_from_budget:
        reasons.append("設定為不計入預算")
    quick_entry_shape = (
        len(roles) == 2 and roles.count(LineRole.MAIN) == 1 and roles.count(None) == 1
    )
    if not reasons and not quick_entry_shape:
        reasons.append("分錄不是快速記帳格式")
    if reasons:
        return (f"這筆支出{('、'.join(reasons))}，快速記帳表單無法完整修改",)
    return ()


def require_editable(db: Session, txn_id: int) -> Transaction:
    """修改前的伺服器端檢查：找不到 → NotFound；不能用表單修改 → RuleViolation（NOT_EDITABLE）。"""
    txn = _get(db, txn_id)
    reasons = edit_blockers(txn)
    if reasons:
        raise RuleViolation(NOT_EDITABLE, reasons[0])
    return txn


@dataclass(frozen=True)
class EditState:
    """修改頁需要的狀態：原交易類型（決定用哪張表單）、不能修改的原因、預填值。"""

    txn_type: str
    blocker: str | None  # None = 可以修改
    values: dict[str, str] | None  # 可以修改時的預填值（也是「目前值」的來源）


def edit_state(db: Session, txn_id: int) -> EditState:
    """找不到 → NotFound。不能修改時 values 為 None。"""
    txn = _get(db, txn_id)
    reasons = edit_blockers(txn)
    if reasons:
        return EditState(txn.txn_type, reasons[0], None)
    if txn.txn_type == TxnType.EXPENSE:
        return EditState(txn.txn_type, None, _expense_values(db, txn))
    return EditState(txn.txn_type, None, txn_edit.edit_values(txn))


def get_detail(db: Session, txn_id: int) -> TxnDetail:
    txn = _get(db, txn_id)
    merchant = db.get(Merchant, txn.merchant_id) if txn.merchant_id is not None else None
    pm = db.get(PaymentMethod, txn.payment_method_id) if txn.payment_method_id else None
    refunded = (
        ledger_repo.active_refunded_amount(db, txn.id) if txn.txn_type == TxnType.EXPENSE else 0
    )
    is_deleted = txn.deleted_at is not None
    delete_blockers: tuple[str, ...] = ()
    if not is_deleted and refunded > 0:
        delete_blockers = (f"這筆支出已退 {refunded:,} 元，請先刪除退款",)
    return TxnDetail(
        id=txn.id,
        txn_date=date.fromisoformat(txn.txn_date),
        txn_type=txn.txn_type,
        type_label=TXN_TYPE_LABELS.get(txn.txn_type, txn.txn_type),
        is_deleted=is_deleted,
        deleted_at=datetime.fromisoformat(txn.deleted_at) if is_deleted else None,
        merchant_name=merchant.name if merchant is not None else None,
        payment_method_name=pm.name if pm is not None else None,
        description=txn.description,
        voucher_label=VOUCHER_LABELS.get(txn.voucher_type) if txn.voucher_type else None,
        invoice_no=txn.invoice_no,
        magnitude=sum(p.amount for p in txn.postings if p.amount > 0),
        lines=tuple(_line(db, p) for p in txn.postings),
        refunded_amount=refunded,
        related_txn_id=txn.related_txn_id,
        edit_blockers=edit_blockers(txn),
        delete_blockers=delete_blockers,
    )


def edit_values(db: Session, txn_id: int) -> dict[str, str]:
    """修改表單的預填值；不能修改時丟 RuleViolation（NOT_EDITABLE）。"""
    txn = require_editable(db, txn_id)
    if txn.txn_type == TxnType.EXPENSE:
        return _expense_values(db, txn)
    return txn_edit.edit_values(txn)


def _expense_values(db: Session, txn: Transaction) -> dict[str, str]:
    """快速記帳格式支出的預填值（4A）。"""
    main = next(p for p in txn.postings if p.line_role == LineRole.MAIN)
    merchant = db.get(Merchant, txn.merchant_id) if txn.merchant_id is not None else None
    return {
        "txn_date": txn.txn_date,
        "amount": str(main.amount),
        "account_id": str(main.account_id),
        "payment_method_id": "" if txn.payment_method_id is None else str(txn.payment_method_id),
        "description": txn.description or "",
        "merchant_name": merchant.name if merchant is not None else "",
        "voucher_type": txn.voucher_type or "",
        "invoice_no": txn.invoice_no or "",
    }
