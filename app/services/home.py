"""首頁（P3-1／P3-2B；規格 8.1、F26）：帳戶餘額、信用卡本期應繳、帳平檢查狀態。只讀，不寫入。

帳戶餘額
- 只列 show_on_home = 1 的資產、負債科目；其他五大類即使勾了也略過（規格 8.1 只定義這兩類）。
- 停用的帳戶照樣顯示並標示「已停用」，保留真實餘額（停用不代表沒有錢）。
- 金額一律來自 ledger_queries.balance()（active_postings()，截至今天，含子孫科目），
  已是正常餘額方向：負債為正代表欠款。這裡不另寫加總、不重寫排除條件。
- 不提供合計：show_on_home 的科目可能有上下層關係，逐列相加會重複計算。

帳平檢查
- 每次開首頁即時呼叫 integrity.check_all()（刻意包含已刪除與草稿交易，規格 5.4 的例外）。
- 規格 7.2 原本是每日 03:30 排程；P3-1 的差異與上線審查事項記在 docs/decisions/p3.md。
- 同一筆交易的多個問題合併成一列，連到交易明細。
"""

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models.enums import AccountClass, AccountSubtype, TxnStatus
from app.repositories import accounts as account_repo
from app.services import credit_cards, integrity
from app.services.ledger_queries import balance

HOME_ACCOUNT_CLASSES = (AccountClass.ASSET, AccountClass.LIABILITY)

PROBLEM_LABELS = {
    "UNBALANCED": "借貸不平衡",
    "TOO_FEW_POSTINGS": "分錄不足兩筆",
    "NON_LEAF_ACCOUNT": "分錄記在非末層科目",
}


@dataclass(frozen=True)
class HomeBalance:
    account_id: int
    name: str
    account_class: str
    amount: int  # 正常餘額方向（負債為正 = 欠款）
    is_active: bool

    @property
    def is_debt(self) -> bool:
        """負債且餘額為正 → 顯示「欠款 N」；負數照實顯示，不加前綴（溢繳語意留到 P3-2）。"""
        return self.account_class == AccountClass.LIABILITY and self.amount > 0


@dataclass(frozen=True)
class IntegrityItem:
    transaction_id: int
    labels: tuple[str, ...]  # 這筆交易的問題（中文說明，依問題代碼排序）
    is_deleted: bool
    is_draft: bool


@dataclass(frozen=True)
class IntegrityStatus:
    items: tuple[IntegrityItem, ...]

    @property
    def ok(self) -> bool:
        return not self.items


@dataclass(frozen=True)
class HomeCard:
    """首頁信用卡區塊的一列（P3-2B）。due = None 代表尚未設定結帳日。

    金額與日期全部來自 credit_cards.amount_due()；這裡只加顯示用的判斷，不重算。
    """

    account_id: int
    name: str
    is_active: bool
    due: credit_cards.CardDue | None

    @property
    def is_overdue(self) -> bool:
        """繳款日已過且仍有本期應繳（只是顯示判斷，不是 P3-2A 的旗標）。"""
        return (
            self.due is not None and self.due.cycle.days_until_due < 0 and self.due.amount_due > 0
        )

    @property
    def overdue_days(self) -> int:
        return -self.due.cycle.days_until_due if self.due is not None else 0


@dataclass(frozen=True)
class HomeSummary:
    balances: tuple[HomeBalance, ...]
    integrity: IntegrityStatus
    cards: tuple[HomeCard, ...] = ()


def home_balances(db: Session) -> tuple[HomeBalance, ...]:
    return tuple(
        HomeBalance(
            account_id=account.id,
            name=account.name,
            account_class=account.account_class,
            amount=balance(db, account.id),
            is_active=account.is_active,
        )
        for account in account_repo.list_shown_on_home(db)
        if account.account_class in HOME_ACCOUNT_CLASSES
    )


def home_cards(db: Session) -> tuple[HomeCard, ...]:
    """show_on_home = 1 的信用卡。停用且沒有設定的卡略過（停用卡不能改設定，連結沒有用）。"""
    cards = []
    for account in account_repo.list_shown_on_home(db):
        if account.subtype != AccountSubtype.CREDIT_CARD:
            continue
        due = credit_cards.amount_due(db, account.id)
        if due is None and not account.is_active:
            continue
        cards.append(HomeCard(account.id, account.name, account.is_active, due))
    return tuple(cards)


def integrity_status(db: Session) -> IntegrityStatus:
    grouped: dict[int, list[integrity.Problem]] = {}
    for problem in integrity.check_all(db):  # 已依 (transaction_id, code) 排序
        grouped.setdefault(problem.transaction_id, []).append(problem)
    return IntegrityStatus(
        tuple(
            IntegrityItem(
                transaction_id=txn_id,
                labels=tuple(PROBLEM_LABELS.get(p.code, p.code) for p in problems),
                is_deleted=problems[0].is_deleted,
                is_draft=problems[0].status == TxnStatus.DRAFT,
            )
            for txn_id, problems in grouped.items()
        )
    )


def home_summary(db: Session) -> HomeSummary:
    return HomeSummary(
        balances=home_balances(db), integrity=integrity_status(db), cards=home_cards(db)
    )
