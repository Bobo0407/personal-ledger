"""帳平檢查（規格 7.1 integrity.py、5.4、V02；決策 Q9）。

刻意檢查「全部」交易，包含已刪除與草稿：它們隨時可能被還原或確認入帳。
這是規格 5.4 允許讀取已刪除與草稿交易的例外之一，不能拿來算餘額或報表。

問題代碼：
- UNBALANCED：分錄加總不為 0
- TOO_FEW_POSTINGS：分錄少於 2 筆（含 0 筆）
- NON_LEAF_ACCOUNT：分錄記在非末層科目（M03 理論上會擋住，這是第二道防線）

每日排程（03:30）在 P3 接上；發現問題時首頁顯示警示（規格 9.3）。
"""

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.repositories import ledger as ledger_repo


@dataclass(frozen=True)
class Problem:
    transaction_id: int
    code: str
    detail: str
    status: str
    is_deleted: bool


def check_all(db: Session) -> list[Problem]:
    problems: list[Problem] = []
    info: dict[int, tuple[str, bool]] = {}
    for txn_id, status, deleted_at, count, total in ledger_repo.all_transaction_sums(db):
        deleted = deleted_at is not None
        info[txn_id] = (status, deleted)
        if count < 2:
            problems.append(
                Problem(txn_id, "TOO_FEW_POSTINGS", f"只有 {count} 筆分錄", status, deleted)
            )
        if total != 0:
            problems.append(
                Problem(txn_id, "UNBALANCED", f"借貸不平衡，差額 {total:,} 元", status, deleted)
            )
    for txn_id, posting_id, account_id in ledger_repo.all_postings_on_non_leaf_accounts(db):
        status, deleted = info[txn_id]
        problems.append(
            Problem(
                txn_id,
                "NON_LEAF_ACCOUNT",
                f"分錄 {posting_id} 記在非末層科目（科目 id {account_id}）",
                status,
                deleted,
            )
        )
    return sorted(problems, key=lambda p: (p.transaction_id, p.code))
