"""架構守門（規格 5.4、7 節；決策 Q11；P1 Step 7）。

用掃原始碼（regex 或 AST）的方式守住分層規則，只檢查 app/ 的正式程式，測試不受限制：

- 只有 LedgerService（services/ledger.py）可以建立分錄與交易表頭，也只有它可以改 deleted_at。
- 「排除已刪除與草稿」的查詢條件只能寫在 repositories/ledger.py（active_postings() 的語意）。
  其他地方可以在 active_postings() 的基礎上做 SUM，但不能自己重寫排除條件。
- LedgerService 的寫入函式（post／update／soft_delete／restore）只由 services/transactions.py 呼叫。
- routers 不直接碰 repositories、LedgerService、Posting，也不自己 add／commit。
- repositories 不 commit，也不 import services。
- 規格第 11 節的帳務核心案例（T01–T14、T33、T34）都要有對應的 test_tXX_* 測試。

金額欄位必須是 INTEGER 的檢查在 tests/integration/test_ledger_schema.py（看實際 schema）。
"""

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "app"
TESTS = ROOT / "tests"

LEDGER_WRITERS = {"post", "update", "soft_delete", "restore"}
SESSION_WRITES = {"add", "add_all", "commit", "flush", "delete"}


def sources() -> dict[str, str]:
    return {
        path.relative_to(APP).as_posix(): path.read_text(encoding="utf-8")
        for path in APP.rglob("*.py")
    }


def trees() -> dict[str, ast.Module]:
    return {name: ast.parse(text, filename=name) for name, text in sources().items()}


def imported_modules(tree: ast.Module) -> set[str]:
    """這個模組 import 的完整名稱（含 from x import y 的 x.y）。"""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def ledger_service_aliases(tree: ast.Module) -> set[str]:
    """在這個模組裡代表 app.services.ledger 的名稱（處理 import ... as ...）。"""
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "app.services":
            aliases.update(a.asname or a.name for a in node.names if a.name == "ledger")
        elif isinstance(node, ast.Import):
            aliases.update(a.asname for a in node.names if a.name == "app.services.ledger")
    return {a for a in aliases if a}


def attribute_calls(tree: ast.Module) -> list[tuple[str | None, str]]:
    """所有 x.y(...) 呼叫的 (x 的名稱或 None, y)。"""
    calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            owner = node.func.value.id if isinstance(node.func.value, ast.Name) else None
            calls.append((owner, node.func.attr))
    return calls


# --- 分錄與交易的寫入 ---------------------------------------------------------------


def test_only_ledger_service_creates_postings():
    pattern = re.compile(r"(?<!class )\bPosting\(|insert\(\s*Posting\b|Posting\.__table__\.insert")
    offenders = [name for name, text in sources().items() if pattern.search(text)]
    assert offenders == ["services/ledger.py"]


def test_only_ledger_service_creates_transactions_and_changes_deleted_at():
    created, deleted_at = set(), set()
    for name, tree in trees().items():
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "Transaction"
            ):
                created.add(name)
            targets = (
                node.targets
                if isinstance(node, ast.Assign)
                else [node.target]
                if isinstance(node, ast.AugAssign | ast.AnnAssign)
                else []
            )
            if any(isinstance(t, ast.Attribute) and t.attr == "deleted_at" for t in targets):
                deleted_at.add(name)
    assert created == {"services/ledger.py"}
    assert deleted_at == {"services/ledger.py"}


def test_ledger_writes_only_from_transactions_service():
    offenders = set()
    for name, tree in trees().items():
        if name in {"services/ledger.py", "services/transactions.py"}:
            continue
        aliases = ledger_service_aliases(tree)
        if any(
            owner in aliases and attr in LEDGER_WRITERS for owner, attr in attribute_calls(tree)
        ):
            offenders.add(name)
        imported = imported_modules(tree)
        if any(f"app.services.ledger.{w}" in imported for w in LEDGER_WRITERS):
            offenders.add(name)
    assert offenders == set()


# --- 有效分錄的語意 -------------------------------------------------------------------


def test_active_filter_lives_only_in_ledger_repository():
    pattern = re.compile(r"deleted_at\.is_\(None\)|deleted_at\s+IS\s+NULL", re.IGNORECASE)
    allowed = {
        "repositories/ledger.py",  # active_postings()
        "models/transaction.py",  # 發票號碼部分唯一索引的條件，不是帳務加總
    }
    offenders = {name for name, text in sources().items() if pattern.search(text)}
    assert offenders <= allowed
    assert "repositories/ledger.py" in offenders


def test_transaction_status_and_deleted_at_queried_only_in_ledger_repository():
    """查詢條件 Transaction.status／Transaction.deleted_at（類別屬性）只能出現在
    repositories/ledger.py；其他地方要從 active_postings() 開始，不能自己組排除條件。
    （物件屬性 txn.status、txn.deleted_at 的讀寫不在此限。）"""
    pattern = re.compile(r"\bTransaction\.(status|deleted_at)\b")
    offenders = {name for name, text in sources().items() if pattern.search(text)}
    assert offenders <= {"repositories/ledger.py"}


# --- 分層 -----------------------------------------------------------------------------


def test_routers_do_not_touch_ledger_internals():
    forbidden = ("app.repositories", "app.services.ledger", "app.models.Posting")
    offenders = {}
    for name, tree in trees().items():
        if not name.startswith("routers/"):
            continue
        bad = {m for m in imported_modules(tree) if m.startswith(forbidden)}
        bad |= {
            f"{owner}.{attr}()" for owner, attr in attribute_calls(tree) if attr in SESSION_WRITES
        }
        if bad:
            offenders[name] = sorted(bad)
    assert offenders == {}


def test_repositories_do_not_commit_or_import_services():
    offenders = {}
    for name, tree in trees().items():
        if not name.startswith("repositories/"):
            continue
        bad = {m for m in imported_modules(tree) if m.startswith("app.services")}
        bad |= {f"{owner}.{attr}()" for owner, attr in attribute_calls(tree) if attr == "commit"}
        if bad:
            offenders[name] = sorted(bad)
    assert offenders == {}


# --- 規格第 11 節的帳務核心案例 -----------------------------------------------------------


def test_every_core_spec_case_has_a_test():
    required = {f"t{n:02d}" for n in (*range(1, 15), 33, 34)}
    pattern = re.compile(r"^def test_(t\d{2})_", re.MULTILINE)
    found = {
        match
        for path in TESTS.rglob("test_*.py")
        for match in pattern.findall(path.read_text(encoding="utf-8"))
    }
    assert sorted(required - found) == []
