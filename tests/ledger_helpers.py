"""帳務測試共用的小工具。

insert_raw_transaction() 繞過 LedgerService 直接寫入交易與分錄，只限測試使用：
用來製造草稿、已刪除、不平衡等 LedgerService 不會產生的狀態。
"""

from datetime import date, datetime

from app import clock
from app.models import Posting, Transaction
from app.repositories import accounts as account_repo
from app.services import ledger
from app.services.entries import ManualEntry, ManualForm, ManualLine

# 測試裡的「今天」固定為 2026-10-15（台北）
TODAY = date(2026, 10, 15)


def freeze_today(monkeypatch, today: date = TODAY) -> None:
    fixed = datetime(today.year, today.month, today.day, 12, 0, tzinfo=clock.APP_TIMEZONE)
    monkeypatch.setattr(clock, "now", lambda: fixed)


def acc(db, code: str) -> int:
    account = account_repo.get_by_code(db, code)
    assert account is not None, code
    return account.id


def manual_form(db, txn_date: date | None, *lines: tuple[str | None, int], **kwargs) -> ManualForm:
    """lines：(科目代碼, 帶正負號金額)；代碼為 None 代表尚未選科目。"""
    return ManualForm(
        txn_date=txn_date,
        lines=tuple(
            ManualLine(account_id=acc(db, code) if code else None, amount=amount)
            for code, amount in lines
        ),
        **kwargs,
    )


def post_manual(db, txn_date: date, *lines: tuple[str, int], confirmed=(), **kwargs):
    draft = ManualEntry.build(manual_form(db, txn_date, *lines, **kwargs))
    return ledger.post(db, draft, confirmed=confirmed)


def insert_raw_transaction(
    db,
    txn_date: str,
    *lines: tuple[str, int],
    status: str = "POSTED",
    deleted: bool = False,
    txn_type: str = "MANUAL",
) -> int:
    txn = Transaction(txn_date=txn_date, txn_type=txn_type, status=status)
    if deleted:
        txn.deleted_at = "2026-10-15T12:00:00+08:00"
    txn.postings = [
        Posting(line_no=i, account_id=acc(db, code), amount=amount)
        for i, (code, amount) in enumerate(lines, start=1)
    ]
    db.add(txn)
    db.commit()
    return txn.id
