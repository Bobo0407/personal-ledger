"""架構守門（規格 5.4、7 節；決策 Q11）。

- 只有 LedgerService（app/services/ledger.py）可以建立分錄。
- 「排除已刪除與草稿」的條件只能寫在 repositories/ledger.py 的 active_postings()，
  其他程式要做帳務加總一律從那裡開始，不自行撰寫排除條件。
"""

import re
from pathlib import Path

APP = Path(__file__).resolve().parents[2] / "app"


def sources() -> dict[str, str]:
    return {
        path.relative_to(APP).as_posix(): path.read_text(encoding="utf-8")
        for path in APP.rglob("*.py")
    }


def test_only_ledger_service_creates_postings():
    pattern = re.compile(r"(?<!class )\bPosting\(|insert\(\s*Posting\b|Posting\.__table__\.insert")
    offenders = [name for name, text in sources().items() if pattern.search(text)]
    assert offenders == ["services/ledger.py"]


def test_active_filter_lives_only_in_ledger_repository():
    pattern = re.compile(r"deleted_at\.is_\(None\)|deleted_at\s+IS\s+NULL", re.IGNORECASE)
    allowed = {
        "repositories/ledger.py",  # active_postings()
        "models/transaction.py",  # 發票號碼部分唯一索引的條件，不是帳務加總
    }
    offenders = {name for name, text in sources().items() if pattern.search(text)}
    assert offenders <= allowed
    assert "repositories/ledger.py" in offenders
