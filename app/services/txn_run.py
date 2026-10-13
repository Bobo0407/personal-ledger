"""連續補記「本次已記 N 筆」的顯示資料（P2 Step 10，UF02）。

只是 UI／session 的記帳：輸入是 Cookie 裡的 transaction id（app/entry_run.py），
輸出是畫面要顯示的列。不寫入任何東西，也不做任何帳務加總。

N 的定義：本輪成功建立的交易，且資料庫找得到。軟刪除、還原都不改變 N；
已刪除的交易仍列在清單（標示「已刪除」，可點進明細還原）。資料庫完全找不到的 id 略過。
"""

from collections.abc import Collection
from dataclasses import dataclass
from datetime import date

from sqlalchemy.orm import Session

from app.repositories import ledger as ledger_repo
from app.services.search import TXN_TYPE_LABELS, TxnRow


@dataclass(frozen=True)
class RunTxnRow:
    row: TxnRow  # amount 是該交易自己的 magnitude，僅供顯示
    is_deleted: bool


@dataclass(frozen=True)
class RunSummary:
    count: int  # 清單中資料庫找得到的筆數（含已刪除）
    rows: tuple[RunTxnRow, ...]  # 最新建立的在前（id 由大到小）


def summarize(db: Session, txn_ids: Collection[int]) -> RunSummary:
    if not txn_ids:
        return RunSummary(count=0, rows=())
    rows = tuple(
        RunTxnRow(
            row=TxnRow(
                id=txn.id,
                txn_date=date.fromisoformat(txn.txn_date),
                txn_type=txn.txn_type,
                type_label=TXN_TYPE_LABELS.get(txn.txn_type, txn.txn_type),
                merchant_name=merchant_name,
                description=txn.description,
                amount=magnitude,
            ),
            is_deleted=txn.deleted_at is not None,
        )
        for txn, merchant_name, magnitude in ledger_repo.transactions_by_ids(db, txn_ids)
    )
    return RunSummary(count=len(rows), rows=rows)
