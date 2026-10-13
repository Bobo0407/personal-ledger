"""交易明細（P2 Step 4A）與修改條件的入口。

只讀，不寫入。明細要能讀到已刪除的交易（規格 5.4：交易的「已刪除」檢視與還原），
所以這裡用 id 直接讀一筆交易；這不是帳務加總，不經過 active_postings()。

分錄顯示不用「借／貸」（規格 1.5），也不給畫面帶正負號的金額：
每筆分錄依科目的正常餘額方向（ledger_queries.NORMAL_SIGN）換算成「增加／減少 + 金額」，
例如「郵局 減少 120」「聯邦信用卡 增加 120」（欠款增加）。template 不判斷正負或科目類別。

修改條件（edit guard）：表單不能完整表示的交易不開放修改，免得儲存時默默丟掉看不到的內容。
判斷與預填值在 services/txn_edit.py（還原 → builder 重建 → 完整比對）：
4A 只開放快速記帳格式的支出；5B 加入收入、轉帳、繳卡費；Step 6 支出改用同一個做法，
開放手續費、分期利息、不計入預算（外幣仍不開放）。
帳務規則（V08、V10–V12、REFUND_LOCKED……）仍全部由 P1 負責。

退款（P2 Step 7）：明細顯示已退、剩餘可退與有效退款連結；refund_state() 給退款頁預填。
「剩餘可退」＝原交易 MAIN 金額 − 有效退款 MAIN 合計（和 V07 用同一個 repository 查詢），
只用來顯示與預填，不是驗證來源；退款上限、日期、原交易限制都由 P1（V07、REFUND_DATE、
REFUND_ORIGINAL）判斷。
"""

from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy.orm import Session

from app.models import Merchant, PaymentMethod, Transaction
from app.models.enums import LineRole, TxnType
from app.repositories import accounts as account_repo
from app.repositories import ledger as ledger_repo
from app.services import txn_edit
from app.services.entry_options import VOUCHER_LABELS
from app.services.errors import NotFound, RuleViolation
from app.services.ledger_queries import NORMAL_SIGN
from app.services.search import TXN_TYPE_LABELS
from app.services.validation import main_posting, refund_original

NOT_EDITABLE = "NOT_EDITABLE"

ROLE_LABELS = {LineRole.FEE: "手續費", LineRole.INTEREST: "分期利息"}


@dataclass(frozen=True)
class PostingLine:
    account_name: str
    effect: str  # 「增加」或「減少」（依科目正常餘額方向）
    amount: int  # 絕對值
    role_label: str | None  # 手續費、分期利息；MAIN 與一般分錄不另外標示


@dataclass(frozen=True)
class RefundLink:
    txn_id: int
    txn_date: date
    amount: int  # 正數


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
    edit_blockers: tuple[str, ...]  # 空 = 可以用表單修改
    delete_blockers: tuple[str, ...]  # 空 = 可以刪除（已刪除的交易不適用）
    exclude_from_budget: bool = False  # 不計入預算（表頭旗標；P2 Step 6 在明細顯示）
    # 退款（P2 Step 7，只有支出有值）
    refunds: tuple[RefundLink, ...] = ()  # 有效退款
    refundable_amount: int = 0  # 剩餘可退（顯示用）
    refund_blocker: str | None = None  # 不能退款的原因；None 且是支出 → 顯示「退款」


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


def edit_blockers(db: Session, txn: Transaction) -> tuple[str, ...]:
    """不能用表單修改的原因；空 tuple 代表可以修改。判斷在 services/txn_edit.py
    （還原 → builder 重建 → 完整比對；Step 6 起支出也用同一個做法）。"""
    return txn_edit.edit_blockers(db, txn)


def require_editable(db: Session, txn_id: int) -> Transaction:
    """修改前的伺服器端檢查：找不到 → NotFound；不能用表單修改 → RuleViolation（NOT_EDITABLE）。"""
    txn = _get(db, txn_id)
    reasons = edit_blockers(db, txn)
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
    reasons = edit_blockers(db, txn)
    if reasons:
        return EditState(txn.txn_type, reasons[0], None)
    return EditState(txn.txn_type, None, txn_edit.edit_values(db, txn))


def _refund_info(db: Session, txn: Transaction) -> tuple[int, int, str | None]:
    """(已退, 剩餘可退, 不能退款的原因)。原交易條件沿用 P1 的 refund_original()（只拿來顯示）。"""
    if txn.txn_type != TxnType.EXPENSE:
        return 0, 0, "只能對支出交易退款"
    refunded = ledger_repo.active_refunded_amount(db, txn.id)
    main = main_posting(txn.postings)
    remaining = (main.amount - refunded) if main is not None else 0
    original, issues = refund_original(db, txn.id)
    if original is None:
        return refunded, max(remaining, 0), issues[0].message
    if main is None:
        return refunded, 0, "這筆支出沒有主要費用分錄，無法退款"
    if remaining <= 0:
        return refunded, 0, "已全額退款"
    return refunded, remaining, None


@dataclass(frozen=True)
class RefundState:
    """退款頁（GET/POST /txn/{id}/refund）需要的原交易摘要與預填值。"""

    original_id: int
    txn_date: date
    account_name: str
    merchant_name: str | None
    main_amount: int
    refunded: int
    remaining: int
    default_payment_method_id: int | None  # 原交易的付款方式，只有仍啟用時才預選
    blocker: str | None  # 不能退款的原因（只用來導回明細；POST 仍交給 P1 判斷）


def refund_state(db: Session, txn_id: int) -> RefundState:
    """找不到 → NotFound。"""
    txn = _get(db, txn_id)
    refunded, remaining, blocker = _refund_info(db, txn)
    main = main_posting(txn.postings)
    account = account_repo.get(db, main.account_id) if main is not None else None
    merchant = db.get(Merchant, txn.merchant_id) if txn.merchant_id is not None else None
    pm = db.get(PaymentMethod, txn.payment_method_id) if txn.payment_method_id else None
    return RefundState(
        original_id=txn.id,
        txn_date=date.fromisoformat(txn.txn_date),
        account_name=account.name if account is not None else "",
        merchant_name=merchant.name if merchant is not None else None,
        main_amount=main.amount if main is not None else 0,
        refunded=refunded,
        remaining=remaining,
        default_payment_method_id=pm.id if pm is not None and pm.is_active else None,
        blocker=blocker,
    )


def get_detail(db: Session, txn_id: int) -> TxnDetail:
    txn = _get(db, txn_id)
    merchant = db.get(Merchant, txn.merchant_id) if txn.merchant_id is not None else None
    pm = db.get(PaymentMethod, txn.payment_method_id) if txn.payment_method_id else None
    refunded, refundable, refund_blocker = _refund_info(db, txn)
    refunds = (
        tuple(
            RefundLink(rid, date.fromisoformat(day), amount)
            for rid, day, amount in ledger_repo.active_refunds(db, txn.id)
        )
        if txn.txn_type == TxnType.EXPENSE
        else ()
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
        edit_blockers=edit_blockers(db, txn),
        exclude_from_budget=txn.exclude_from_budget,
        delete_blockers=delete_blockers,
        refunds=refunds,
        refundable_amount=refundable,
        refund_blocker=refund_blocker,
    )


def edit_values(db: Session, txn_id: int) -> dict[str, str]:
    """修改表單的預填值；不能修改時丟 RuleViolation（NOT_EDITABLE）。"""
    return txn_edit.edit_values(db, require_editable(db, txn_id))
