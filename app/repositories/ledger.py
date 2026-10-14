"""分錄查詢（規格 5.4）。只查詢，不 commit，不放商業規則。

有效分錄 = postings JOIN transactions，且 transactions.deleted_at IS NULL AND status = 'POSTED'。
這個排除條件全專案只寫在 active_postings() 這一處；所有帳務加總都從這裡開始，
呼叫端只能再加條件或改成加總欄位，例如：

    active_postings().with_only_columns(func.sum(Posting.amount)).where(...)

例外（規格 5.4）：帳平檢查要看全部交易（含已刪除與草稿），使用名稱明確寫著「全部」的
all_transaction_sums() 與 all_postings_on_non_leaf_accounts()，不得用於餘額或報表。
active_invoice_owner()／invoice_in_use() 只用於發票號碼唯一性（與資料庫部分唯一索引相同條件），
find_by_source_ref() 只用於送出權杖去重；兩者都不是帳務加總。

交易列表與搜尋（P2 Step 3）：先從 active_postings() 依交易分組成「一筆交易一列」的子查詢
（_active_transactions()），之後的篩選、筆數、分頁、合計都建立在它之上，
不會因為 postings 的 join 而重複計算交易，也不另外撰寫排除條件。
"""

from collections.abc import Collection, Iterable
from datetime import date

from sqlalchemy import ColumnElement, Select, Subquery, case, exists, func, select
from sqlalchemy.orm import Session, aliased

from app.models import Account, Merchant, Posting, Transaction, TransactionTag
from app.models.enums import AccountClass, LineRole, TxnStatus, TxnType


def active_postings() -> Select:
    """有效分錄的唯一查詢入口。"""
    return (
        select(Posting)
        .join(Transaction, Posting.transaction_id == Transaction.id)
        .where(Transaction.deleted_at.is_(None), Transaction.status == TxnStatus.POSTED.value)
    )


def _date_range(stmt: Select, start: date | None, end: date | None) -> Select:
    # txn_date 是 YYYY-MM-DD 字串，字串比較等同日期比較
    if start is not None:
        stmt = stmt.where(Transaction.txn_date >= start.isoformat())
    if end is not None:
        stmt = stmt.where(Transaction.txn_date <= end.isoformat())
    return stmt


def active_sum_for_accounts(
    db: Session,
    account_ids: Iterable[int],
    start: date | None = None,
    end: date | None = None,
    txn_type: str | None = None,
    budget_only: bool = False,
) -> int:
    """指定科目在期間內（起訖日都包含）的有效分錄加總，原始正負號；可限定交易類型。

    budget_only = True：另外排除 exclude_from_budget = 1 的交易（預算已用；P3-3A）。
    """
    ids = list(account_ids)
    if not ids:
        return 0
    stmt = active_postings().with_only_columns(func.coalesce(func.sum(Posting.amount), 0))
    stmt = stmt.where(Posting.account_id.in_(ids))
    if txn_type is not None:
        stmt = stmt.where(Transaction.txn_type == txn_type)
    if budget_only:
        stmt = stmt.where(Transaction.exclude_from_budget.is_(False))
    return int(db.scalar(_date_range(stmt, start, end)) or 0)


def accounts_with_active_opening(
    db: Session, account_ids: Iterable[int], exclude_txn_id: int | None = None
) -> set[int]:
    """這些科目中，已經有有效 OPENING 分錄的科目 id（決策 D12）；修改期初時排除自己。"""
    ids = list(account_ids)
    if not ids:
        return set()
    stmt = (
        active_postings()
        .with_only_columns(Posting.account_id)
        .where(Transaction.txn_type == TxnType.OPENING.value, Posting.account_id.in_(ids))
        .distinct()
    )
    if exclude_txn_id is not None:
        stmt = stmt.where(Transaction.id != exclude_txn_id)
    return set(db.scalars(stmt))


def _active_refund_mains(original_id: int, exclude_txn_id: int | None) -> Select:
    """原交易的有效退款（POSTED、未刪除）中，沖回 MAIN 的分錄（決策 U8）。"""
    stmt = active_postings().where(
        Transaction.txn_type == TxnType.REFUND.value,
        Transaction.related_txn_id == original_id,
        Posting.line_role == LineRole.MAIN.value,
    )
    if exclude_txn_id is not None:
        stmt = stmt.where(Transaction.id != exclude_txn_id)
    return stmt


def active_refunded_amount(db: Session, original_id: int, exclude_txn_id: int | None = None) -> int:
    """已退金額（正數）＝有效退款 MAIN 分錄（貸方）加總的相反數；修改退款時排除自己。"""
    stmt = _active_refund_mains(original_id, exclude_txn_id).with_only_columns(
        func.coalesce(func.sum(Posting.amount), 0)
    )
    return -int(db.scalar(stmt) or 0)


def active_refunds(db: Session, original_id: int) -> list[tuple[int, str, int]]:
    """原交易的有效退款 [(退款交易 id, 日期, 退款金額（正數）), ...]，依日期、id 排序。
    只給明細顯示用（P2 Step 7）；和 active_refunded_amount 同一個查詢條件。"""
    stmt = (
        _active_refund_mains(original_id, None)
        .with_only_columns(Transaction.id, Transaction.txn_date, Posting.amount)
        .order_by(Transaction.txn_date, Transaction.id)
    )
    return [(txn_id, day, -amount) for txn_id, day, amount in db.execute(stmt)]


def earliest_active_refund_date(db: Session, original_id: int) -> str | None:
    stmt = _active_refund_mains(original_id, None).with_only_columns(func.min(Transaction.txn_date))
    return db.scalar(stmt)


def active_invoice_owner(
    db: Session, invoice_no: str, exclude_txn_id: int | None = None
) -> Transaction | None:
    """占用這個發票號碼的未刪除交易（V08）。

    條件與資料庫部分唯一索引 uq_transactions_invoice_no_active 完全相同
    （deleted_at IS NULL；不看 status），全專案只在這裡寫一次。
    """
    stmt = select(Transaction).where(
        Transaction.invoice_no == invoice_no, Transaction.deleted_at.is_(None)
    )
    if exclude_txn_id is not None:
        stmt = stmt.where(Transaction.id != exclude_txn_id)
    return db.scalars(stmt.order_by(Transaction.id).limit(1)).first()


def invoice_in_use(db: Session, invoice_no: str, exclude_txn_id: int | None = None) -> bool:
    """發票號碼是否已被其他未刪除的交易使用（與部分唯一索引的條件相同）。"""
    return active_invoice_owner(db, invoice_no, exclude_txn_id) is not None


def active_expense_main_exists(
    db: Session,
    txn_date: date,
    merchant_id: int,
    main_amount: int,
    exclude_txn_id: int | None = None,
) -> bool:
    """V12：同一天、同商家、MAIN 金額相同的有效 EXPENSE 是否已存在。

    從 active_postings() 開始，已刪除與草稿交易自然排除；修改時用 exclude_txn_id 排除自己。
    """
    stmt = active_postings().where(
        Transaction.txn_type == TxnType.EXPENSE.value,
        Transaction.txn_date == txn_date.isoformat(),
        Transaction.merchant_id == merchant_id,
        Posting.line_role == LineRole.MAIN.value,
        Posting.amount == main_amount,
    )
    if exclude_txn_id is not None:
        stmt = stmt.where(Transaction.id != exclude_txn_id)
    return bool(db.scalar(select(stmt.exists())))


def find_by_source_ref(db: Session, source: str, source_ref: str) -> Transaction | None:
    """依 (source, source_ref) 找交易，包含已刪除與草稿（與 UNIQUE 約束的範圍相同）。

    Web 一次性送出權杖用它判斷「這個權杖是否已經建立過交易」（P2 Step 2 決策 A）。
    """
    stmt = select(Transaction).where(
        Transaction.source == source, Transaction.source_ref == source_ref
    )
    return db.scalar(stmt)


def active_sum_for_class(
    db: Session, account_class: str, start: date | None = None, end: date | None = None
) -> int:
    """某一大類在期間內的有效分錄加總，原始正負號。"""
    stmt = (
        active_postings()
        .join(Account, Posting.account_id == Account.id)
        .with_only_columns(func.coalesce(func.sum(Posting.amount), 0))
        .where(Account.account_class == account_class)
    )
    return int(db.scalar(_date_range(stmt, start, end)) or 0)


# --- 帳平檢查專用：刻意包含已刪除與草稿交易 -------------------------------------


def all_transaction_sums(db: Session) -> list[tuple[int, str, str | None, int, int]]:
    """全部交易（含已刪除、草稿）的 (id, status, deleted_at, 分錄筆數, 分錄加總)。"""
    stmt = (
        select(
            Transaction.id,
            Transaction.status,
            Transaction.deleted_at,
            func.count(Posting.id),
            func.coalesce(func.sum(Posting.amount), 0),
        )
        .outerjoin(Posting, Posting.transaction_id == Transaction.id)
        .group_by(Transaction.id)
        .order_by(Transaction.id)
    )
    return [tuple(row) for row in db.execute(stmt)]


def all_postings_on_non_leaf_accounts(db: Session) -> list[tuple[int, int, int]]:
    """全部交易中記在非末層科目上的分錄：(transaction_id, posting_id, account_id)。"""
    child = Account.__table__.alias("child")
    stmt = (
        select(Posting.transaction_id, Posting.id, Posting.account_id)
        .where(exists().where(child.c.parent_id == Posting.account_id))
        .order_by(Posting.transaction_id, Posting.id)
    )
    return [tuple(row) for row in db.execute(stmt)]


# --- 交易列表與搜尋（P2 Step 3）-----------------------------------------------------------


def _active_transactions() -> Subquery:
    """有效交易，一筆交易一列：(transaction_id, magnitude)。

    magnitude = 該交易所有正數分錄的加總（交易規模，P2 Step 3）；
    與 validation.transaction_magnitude()（V10 用的 Python 版本）是同一個定義。
    從 active_postings() 分組而來，所以已刪除與草稿交易不會出現。
    """
    positive = case((Posting.amount > 0, Posting.amount), else_=0)
    return (
        active_postings()
        .with_only_columns(
            Posting.transaction_id.label("transaction_id"),
            func.sum(positive).label("magnitude"),
        )
        .group_by(Posting.transaction_id)
        .subquery("active_txn")
    )


def _classification_owner() -> ColumnElement:
    """分類維度（商家、標籤）看的是哪一筆交易（P2 Step 8B）。

    退款沒有自己的分類：它是原交易的沖回，商家與標籤一律跟著原交易「目前」的值
    （查詢時判斷，不寫入退款）。其他交易看自己。日期、金額、排序仍然看交易自己。
    """
    return case(
        (Transaction.txn_type == TxnType.REFUND.value, Transaction.related_txn_id),
        else_=Transaction.id,
    )


def _merchant_scope(merchant_id: int) -> ColumnElement:
    """商家篩選：非退款看自己的 merchant_id；退款看原交易現在的 merchant_id。
    退款建立時複製的 merchant_id 是歷史紀錄，不拿來分類（原交易事後可能改商家）。"""
    owner = aliased(Transaction)
    return exists().where(owner.id == _classification_owner(), owner.merchant_id == merchant_id)


def tag_scope(tag_id: int) -> ColumnElement:
    """標籤篩選（「標籤範圍」的唯一定義；V1.5 標籤排行、特別預算也要用這個）：
    交易自己有這個標籤，或是退款而原交易現在有這個標籤。
    用 EXISTS：一筆交易有幾個標籤都只會是一列，不放大筆數與合計。"""
    return exists().where(
        TransactionTag.tag_id == tag_id,
        TransactionTag.transaction_id.in_([Transaction.id, _classification_owner()]),
    )


def _filtered_transactions(
    *,
    date_from: date | None,
    date_to: date | None,
    merchant_id: int | None,
    min_amount: int | None,
    max_amount: int | None,
    tag_id: int | None = None,
) -> Subquery:
    """依搜尋條件篩選後的有效交易，一筆交易一列：(transaction_id, magnitude)。

    商家、標籤是分類維度，退款跟著原交易目前的分類（_classification_owner）；
    筆數、分頁、合計都只從這個結果取，不在合計層另外判斷退款。"""
    active = _active_transactions()
    stmt = select(active.c.transaction_id, active.c.magnitude).join(
        Transaction, Transaction.id == active.c.transaction_id
    )
    stmt = _date_range(stmt, date_from, date_to)
    if merchant_id is not None:
        stmt = stmt.where(_merchant_scope(merchant_id))
    if tag_id is not None:
        stmt = stmt.where(tag_scope(tag_id))
    if min_amount is not None:
        stmt = stmt.where(active.c.magnitude >= min_amount)
    if max_amount is not None:
        stmt = stmt.where(active.c.magnitude <= max_amount)
    return stmt.subquery("filtered_txn")


def search_active_transactions(
    db: Session, *, limit: int, offset: int, **filters
) -> list[tuple[Transaction, str | None, int]]:
    """[(交易, 商家名稱或 None, magnitude)]，依 txn_date DESC, id DESC 排序後分頁。

    商家名稱和商家篩選用同一個分類來源（_classification_owner）：退款顯示原交易「目前」的商家，
    所以「搜商家 B」的結果裡，退款列也寫 B（P2 Step 8B）。退款自己存的 merchant_id 不變。"""
    found = _filtered_transactions(**filters)
    owner = aliased(Transaction)
    stmt = (
        select(Transaction, Merchant.name, found.c.magnitude)
        .join(found, found.c.transaction_id == Transaction.id)
        .outerjoin(owner, owner.id == _classification_owner())
        .outerjoin(Merchant, Merchant.id == owner.merchant_id)
        .order_by(Transaction.txn_date.desc(), Transaction.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return [(txn, name, magnitude) for txn, name, magnitude in db.execute(stmt).all()]


def count_active_transactions(db: Session, **filters) -> int:
    """符合條件的交易筆數（以交易計，不是分錄筆數）。"""
    found = _filtered_transactions(**filters)
    return int(db.scalar(select(func.count()).select_from(found)) or 0)


def active_class_sums_for_search(db: Session, **filters) -> dict[str, int]:
    """符合條件的交易中，EXPENSE、INCOME 科目分錄各自的原始加總（借正貸負，不翻號）。"""
    found = _filtered_transactions(**filters)
    stmt = (
        active_postings()
        .join(Account, Account.id == Posting.account_id)
        .where(
            Posting.transaction_id.in_(select(found.c.transaction_id)),
            Account.account_class.in_([AccountClass.EXPENSE.value, AccountClass.INCOME.value]),
        )
        .with_only_columns(Account.account_class, func.sum(Posting.amount))
        .group_by(Account.account_class)
    )
    return {account_class: int(total) for account_class, total in db.execute(stmt).all()}


# --- 已刪除交易列表（P2 Step 4B）-----------------------------------------------------------
#
# 規格 5.4 的例外：「交易的『已刪除』檢視與還原」可以讀取已刪除交易。
# 這裡只供已刪除列表逐列顯示，不做跨交易加總，也不得提供給餘額、支出／收入合計、報表、預算。


def deleted_transactions(
    db: Session, *, limit: int, offset: int
) -> list[tuple[Transaction, str | None, int]]:
    """[(已刪除的交易, 商家名稱或 None, 該交易自己的 magnitude)]，最近刪除的在前。

    magnitude 只用該交易自己的分錄計算（正數分錄加總），與列表、明細同一個定義，僅供顯示。
    """
    positive = case((Posting.amount > 0, Posting.amount), else_=0)
    own = (
        select(Posting.transaction_id, func.sum(positive).label("magnitude"))
        .group_by(Posting.transaction_id)
        .subquery("own_magnitude")
    )
    stmt = (
        select(Transaction, Merchant.name, func.coalesce(own.c.magnitude, 0))
        .outerjoin(own, own.c.transaction_id == Transaction.id)
        .outerjoin(Merchant, Merchant.id == Transaction.merchant_id)
        .where(Transaction.deleted_at.is_not(None))
        .order_by(Transaction.deleted_at.desc(), Transaction.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return [(txn, name, int(magnitude)) for txn, name, magnitude in db.execute(stmt).all()]


def count_deleted_transactions(db: Session) -> int:
    stmt = select(func.count()).select_from(Transaction).where(Transaction.deleted_at.is_not(None))
    return int(db.scalar(stmt) or 0)


# --- 快速記帳「常用科目」的使用次數（P2 Step 11，UF01）--------------------------------------


def expense_main_usage_counts(db: Session, account_ids: Collection[int]) -> dict[int, int]:
    """{科目 id: 使用次數}，只含次數 ≥ 1 的科目。

    次數 = 有效交易（active_postings()）中，txn_type 為 EXPENSE、且 MAIN 分錄落在該科目的交易筆數，
    全期累計。退款（REFUND）、手續費與利息分錄（FEE、INTEREST）、多分錄（MANUAL）都不算；
    草稿與已刪除的交易不在有效分錄裡。只用來排序快速記帳的科目選單，不是金額加總。
    """
    ids = list(account_ids)
    if not ids:
        return {}
    stmt = (
        active_postings()
        .with_only_columns(Posting.account_id, func.count(func.distinct(Posting.transaction_id)))
        .where(
            Transaction.txn_type == TxnType.EXPENSE.value,
            Posting.line_role == LineRole.MAIN.value,
            Posting.account_id.in_(ids),
        )
        .group_by(Posting.account_id)
    )
    return {account_id: int(count) for account_id, count in db.execute(stmt).all()}


# --- 連續補記「本次已記」（P2 Step 10）-------------------------------------------------------
#
# 依 id 取回指定的幾筆交易（含已刪除的），只供 /txn/new 的「本次已記 N 筆」逐列顯示。
# 和已刪除列表一樣：不做跨交易加總，不得提供給餘額、支出／收入合計、報表、預算。


def transactions_by_ids(
    db: Session, txn_ids: Collection[int]
) -> list[tuple[Transaction, str | None, int]]:
    """[(交易, 商家名稱或 None, 該交易自己的 magnitude)]，id 由大到小（最新建立的在前）。

    magnitude 只用該交易自己的分錄計算（正數分錄加總），與列表、明細同一個定義，僅供顯示。
    不存在的 id 不會出現在結果裡。
    """
    positive = case((Posting.amount > 0, Posting.amount), else_=0)
    own = (
        select(Posting.transaction_id, func.sum(positive).label("magnitude"))
        .where(Posting.transaction_id.in_(txn_ids))
        .group_by(Posting.transaction_id)
        .subquery("run_magnitude")
    )
    stmt = (
        select(Transaction, Merchant.name, func.coalesce(own.c.magnitude, 0))
        .outerjoin(own, own.c.transaction_id == Transaction.id)
        .outerjoin(Merchant, Merchant.id == Transaction.merchant_id)
        .where(Transaction.id.in_(txn_ids))
        .order_by(Transaction.id.desc())
    )
    return [(txn, name, int(magnitude)) for txn, name, magnitude in db.execute(stmt).all()]
