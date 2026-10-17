"""期初餘額精靈的讀取端（P3-7；規格 F04、7.3 `/setup/opening`）。只讀。

- 可輸入期初的帳戶 = 啟用中、最末層的資產／負債科目（和 OpeningEntry 的結構規則一致）。
- 每個帳戶的「目前餘額」來自 ledger_queries.balance()（active_postings()，正常餘額方向），
  這裡不另寫加總、不存餘額、不算借貸。
- 「已有期初」= 該帳戶已經有有效的 OPENING 分錄（D12）。真正的防重複在 validation.py 的
  OPENING_EXISTS；這裡只是讓畫面不再提供輸入欄位。
- 寫入走 transactions.record_opening()（OpeningEntry → LedgerService），這個模組不寫任何資料。
"""

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models.enums import AccountClass
from app.repositories import accounts as account_repo
from app.repositories import ledger as ledger_repo
from app.services.ledger_queries import balance

OPENING_CLASSES = (AccountClass.ASSET, AccountClass.LIABILITY)


@dataclass(frozen=True)
class OpeningRow:
    account_id: int
    name: str
    account_class: str
    balance: int  # 正常餘額方向（負債為正 = 欠款）
    has_opening: bool

    @property
    def is_liability(self) -> bool:
        return self.account_class == AccountClass.LIABILITY

    @property
    def has_activity(self) -> bool:
        """還沒有期初、但帳上已經有餘額（通常是已經開始記帳）：新輸入的期初會加到現有餘額上。"""
        return not self.has_opening and self.balance != 0


def _eligible_accounts(db: Session):
    return [a for a in account_repo.leaf_accounts(db) if a.account_class in OPENING_CLASSES]


def opening_rows(db: Session) -> tuple[OpeningRow, ...]:
    accounts = _eligible_accounts(db)
    done = ledger_repo.accounts_with_active_opening(db, [a.id for a in accounts])
    return tuple(
        OpeningRow(a.id, a.name, a.account_class, balance(db, a.id), a.id in done) for a in accounts
    )


def needs_setup(db: Session) -> bool:
    """首頁提示用：有可輸入的帳戶，而且還沒有任何一個帳戶建立過期初。"""
    ids = [a.id for a in _eligible_accounts(db)]
    return bool(ids) and not ledger_repo.accounts_with_active_opening(db, ids)
