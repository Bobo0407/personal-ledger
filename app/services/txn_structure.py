"""交易結構檢查 TXN_STRUCTURE（P1 Step 4 決策 P4、P5、P6、P7、P9、P12、D9）。

Builder 之外的第二道防線：就算有人手寫一個 EntryDraft 繞過 builder，
不符合交易類型語意的分錄也寫不進資料庫。只在分錄本身沒有其他錯誤時才執行。

EXPENSE
- 剛好一筆 MAIN，記在費用科目、金額為正。
- FEE、INTEREST 記在費用科目、金額為正；不允許其他 line_role。
- 剛好一筆付款分錄（line_role 為空），金額為負，等於付款方式對應的帳戶（必須有付款方式）。
- 有 INTEREST（分期利息）時，付款帳戶必須是信用卡（P12）。
INCOME
- 剛好一筆 MAIN，記在收入科目、金額為負；剛好一筆收款分錄，資產或負債、金額為正。
- 不使用付款方式（P5：收款帳戶不是付款管道）。
TRANSFER
- 剛好一筆轉入（正）、一筆轉出（負），都是資產或負債；可另有 FEE（費用、正數）。
- 不能轉入信用卡（D9：繳卡費由 transactions 自動改成 CARD_PAYMENT），也不能從信用卡轉出（P6）。
- 不使用付款方式。
CARD_PAYMENT
- 剛好兩筆、不帶 line_role：信用卡（正）與 CASH／BANK 資產（負）。
- 因此不可能出現費用、收入、權益分錄：繳卡費永遠不會計入支出。
- 不使用付款方式。
OPENING（Step 5）
- 只能有資產或負債帳戶，加上最多一筆「期初權益」；加總剛好為 0 時不建立期初權益分錄（R2）。
PAYROLL（Step 5，R8 最低結構）
- 至少一筆收入分錄；收入分錄必須是貸方（負數）；不能有權益分錄。
REFUND（Step 6）
- 剛好兩筆：一筆 MAIN（費用科目、負數，沖回支出）、
  一筆退回分錄（line_role 為空，資產或負債、正數）；
  退回分錄的帳戶等於付款方式對應的帳戶（必須有付款方式）。
- 「MAIN 科目必須是原交易的支出科目」與退款金額上限（V07）要查原交易，在 validation.py。
ADJUSTMENT 的規則是 V15，在 validation.py；MANUAL 不在這裡檢查。
"""

from sqlalchemy.orm import Session

from app.models import Account, PaymentMethod
from app.models.enums import AccountClass, AccountSubtype, LineRole, SystemAccountKey, TxnType
from app.services.drafts import EntryDraft
from app.services.issues import Issue

CODE = "TXN_STRUCTURE"
BALANCE_SHEET = frozenset({AccountClass.ASSET, AccountClass.LIABILITY})
CARD_PAYMENT_SOURCES = frozenset({AccountSubtype.CASH, AccountSubtype.BANK})


def _issue(message: str, line: int | None = None) -> Issue:
    return Issue(CODE, message, line)


def check_structure(db: Session, draft: EntryDraft, accounts: dict[int, Account]) -> list[Issue]:
    checker = _CHECKERS.get(draft.txn_type)
    return checker(db, draft, accounts) if checker else []


def _lines(draft: EntryDraft, accounts: dict[int, Account]):
    return [(i, p, accounts[p.account_id]) for i, p in enumerate(draft.postings, start=1)]


def _expense(db: Session, draft: EntryDraft, accounts: dict[int, Account]) -> list[Issue]:
    issues: list[Issue] = []
    lines = _lines(draft, accounts)
    mains = [x for x in lines if x[1].line_role == LineRole.MAIN]
    payments = [x for x in lines if x[1].line_role is None]
    if len(mains) != 1:
        issues.append(_issue(f"支出交易必須剛好有一筆主要費用分錄（MAIN），目前 {len(mains)} 筆"))
    for line, p, account in lines:
        role = p.line_role
        if role in (LineRole.MAIN, LineRole.FEE, LineRole.INTEREST):
            if account.account_class != AccountClass.EXPENSE or p.amount <= 0:
                issues.append(_issue(f"{role} 分錄必須是記在費用科目的正數金額", line))
        elif role is not None:
            issues.append(_issue(f"支出交易不能有 {role} 分錄", line))
    if len(payments) != 1:
        issues.append(_issue(f"支出交易必須剛好有一筆付款分錄，目前 {len(payments)} 筆"))
        return issues
    line, payment, pay_account = payments[0]
    if pay_account.account_class not in BALANCE_SHEET or payment.amount >= 0:
        issues.append(_issue("付款分錄必須是資產或負債帳戶的負數金額", line))
    if draft.payment_method_id is None:
        issues.append(_issue("支出交易必須有付款方式"))
    else:
        pm = db.get(PaymentMethod, draft.payment_method_id)
        if pm is not None and pm.account_id != payment.account_id:
            issues.append(_issue(f"付款分錄的帳戶與付款方式「{pm.name}」的扣款帳戶不一致", line))
    has_interest = any(p.line_role == LineRole.INTEREST for _, p, _ in lines)
    if has_interest and pay_account.subtype != AccountSubtype.CREDIT_CARD:
        issues.append(_issue("分期利息只能用在信用卡付款的消費", line))
    return issues


def _income(db: Session, draft: EntryDraft, accounts: dict[int, Account]) -> list[Issue]:
    issues: list[Issue] = []
    lines = _lines(draft, accounts)
    mains = [x for x in lines if x[1].line_role == LineRole.MAIN]
    others = [x for x in lines if x[1].line_role is None]
    if len(mains) != 1 or len(others) != 1 or len(lines) != 2:
        return [_issue("收入交易必須剛好是一筆收入分錄（MAIN）加一筆收款分錄")]
    line, main, main_account = mains[0]
    if main_account.account_class != AccountClass.INCOME or main.amount >= 0:
        issues.append(_issue("收入分錄必須是記在收入科目的負數金額", line))
    line, receive, receive_account = others[0]
    if receive_account.account_class not in BALANCE_SHEET or receive.amount <= 0:
        issues.append(_issue("收款分錄必須是資產或負債帳戶的正數金額", line))
    if draft.payment_method_id is not None:
        issues.append(_issue("收入交易不使用付款方式，請直接指定收款帳戶"))
    return issues


def _transfer(db: Session, draft: EntryDraft, accounts: dict[int, Account]) -> list[Issue]:
    issues: list[Issue] = []
    lines = _lines(draft, accounts)
    moves = [x for x in lines if x[1].line_role is None]
    for line, p, account in lines:
        if p.line_role == LineRole.FEE:
            if account.account_class != AccountClass.EXPENSE or p.amount <= 0:
                issues.append(_issue("手續費分錄必須是記在費用科目的正數金額", line))
        elif p.line_role is not None:
            issues.append(_issue(f"轉帳交易不能有 {p.line_role} 分錄", line))
    ins = [x for x in moves if x[1].amount > 0]
    outs = [x for x in moves if x[1].amount < 0]
    if len(ins) != 1 or len(outs) != 1:
        issues.append(_issue("轉帳交易必須剛好有一筆轉入、一筆轉出"))
        return issues
    for line, _, account in (*ins, *outs):
        if account.account_class not in BALANCE_SHEET:
            issues.append(_issue(f"「{account.name}」不是資產或負債帳戶，不能轉帳", line))
    if ins[0][2].subtype == AccountSubtype.CREDIT_CARD:
        issues.append(_issue("轉入信用卡請使用信用卡繳款", ins[0][0]))
    if outs[0][2].subtype == AccountSubtype.CREDIT_CARD:
        issues.append(_issue("V1 不支援從信用卡轉出（預借現金）", outs[0][0]))
    if draft.payment_method_id is not None:
        issues.append(_issue("轉帳交易不使用付款方式"))
    return issues


def _card_payment(db: Session, draft: EntryDraft, accounts: dict[int, Account]) -> list[Issue]:
    lines = _lines(draft, accounts)
    if len(lines) != 2 or any(p.line_role is not None for _, p, _ in lines):
        return [_issue("信用卡繳款必須剛好兩筆分錄：信用卡與扣款帳戶")]
    issues: list[Issue] = []
    (line_a, a, acc_a), (line_b, b, acc_b) = lines
    card, source = (
        ((line_a, a, acc_a), (line_b, b, acc_b))
        if a.amount > 0
        else (
            (line_b, b, acc_b),
            (line_a, a, acc_a),
        )
    )
    if card[2].subtype != AccountSubtype.CREDIT_CARD or card[1].amount <= 0:
        issues.append(_issue("信用卡繳款的正數分錄必須是信用卡帳戶", card[0]))
    if (
        source[2].account_class != AccountClass.ASSET
        or source[2].subtype not in CARD_PAYMENT_SOURCES
        or source[1].amount >= 0
    ):
        issues.append(_issue("信用卡繳款的扣款帳戶必須是現金或銀行帳戶", source[0]))
    if draft.payment_method_id is not None:
        issues.append(_issue("信用卡繳款不使用付款方式"))
    return issues


def _opening(db: Session, draft: EntryDraft, accounts: dict[int, Account]) -> list[Issue]:
    """期初：只能有資產或負債帳戶，加上最多一筆「期初權益」（R1、R2）。"""
    issues: list[Issue] = []
    equity_lines = 0
    for line, _, account in _lines(draft, accounts):
        if account.system_key == SystemAccountKey.OPENING_EQUITY:
            equity_lines += 1
        elif account.account_class not in BALANCE_SHEET:
            issues.append(_issue(f"期初餘額只能設定資產或負債帳戶，「{account.name}」不是", line))
    if equity_lines > 1:
        issues.append(_issue("一筆期初交易只能有一筆期初權益分錄"))
    if draft.payment_method_id is not None:
        issues.append(_issue("期初交易不使用付款方式"))
    return issues


def _payroll(db: Session, draft: EntryDraft, accounts: dict[int, Account]) -> list[Issue]:
    """薪資（R8 最低結構）：至少一筆收入分錄、收入分錄都是貸方、不能有權益分錄。"""
    issues: list[Issue] = []
    lines = _lines(draft, accounts)
    if not any(a.account_class == AccountClass.INCOME for _, _, a in lines):
        issues.append(_issue("薪資交易至少要有一筆收入分錄"))
    for line, p, account in lines:
        if account.account_class == AccountClass.INCOME and p.amount > 0:
            issues.append(_issue("薪資的收入分錄必須是貸方（負數）", line))
        if account.account_class == AccountClass.EQUITY:
            issues.append(_issue("薪資交易不能有權益分錄", line))
    return issues


def _refund(db: Session, draft: EntryDraft, accounts: dict[int, Account]) -> list[Issue]:
    lines = _lines(draft, accounts)
    mains = [x for x in lines if x[1].line_role == LineRole.MAIN]
    backs = [x for x in lines if x[1].line_role is None]
    if len(lines) != 2 or len(mains) != 1 or len(backs) != 1:
        return [_issue("退款必須剛好兩筆分錄：沖回支出（MAIN）與退回帳戶")]
    issues: list[Issue] = []
    line, main, main_account = mains[0]
    if main_account.account_class != AccountClass.EXPENSE or main.amount >= 0:
        issues.append(_issue("退款的 MAIN 分錄必須是記在費用科目的負數金額", line))
    line, back, back_account = backs[0]
    if back_account.account_class not in BALANCE_SHEET or back.amount <= 0:
        issues.append(_issue("退回分錄必須是資產或負債帳戶的正數金額", line))
    if draft.payment_method_id is None:
        issues.append(_issue("退款必須有退款方式"))
    else:
        pm = db.get(PaymentMethod, draft.payment_method_id)
        if pm is not None and pm.account_id != back.account_id:
            issues.append(_issue(f"退回分錄的帳戶與退款方式「{pm.name}」的帳戶不一致", line))
    return issues


_CHECKERS = {
    TxnType.EXPENSE: _expense,
    TxnType.INCOME: _income,
    TxnType.TRANSFER: _transfer,
    TxnType.CARD_PAYMENT: _card_payment,
    TxnType.OPENING: _opening,
    TxnType.PAYROLL: _payroll,
    TxnType.REFUND: _refund,
}
