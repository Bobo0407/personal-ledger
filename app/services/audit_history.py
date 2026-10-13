"""交易的異動紀錄：把 audit_logs 的快照轉成可讀摘要（P2 Step 4B）。

只讀。不顯示原始 before／after JSON、不顯示 user_id、不做通用 diff。

- 順序：由舊到新（occurred_at, id；repositories/audit.py）。
- CREATE：「建立交易」+ 金額；DELETE：「刪除交易」；RESTORE：「還原交易」。
- UPDATE：只列出前後真的不同的主要欄位（日期、金額、商家、付款方式、備註、憑證類型、發票號碼、
  不計入預算），以及分錄：
  - 「分錄有沒有變」用完整的分錄內容比較（除了 line_no 以外的所有欄位，包含 memo、原幣），
    不在乎順序；
  - 「怎麼寫成人話」另外判斷：科目、金額、分錄角色有變 → 「分錄：前 → 後」（科目代碼 + 名稱 +
    增加／減少 + 金額）；只有 memo、原幣這類內容變了 → 「分錄內容已變更」。
  - 其他表頭欄位若有不同（正常流程不會發生），也不會被當成「內容沒有變更」。
  - 標籤（P2 Step 8A）：「標籤：A、B → A、C」，用現在的名稱，查不到顯示 #id；
    前後快照都有 tag_ids 才比較（8A 之前的舊紀錄沒有這個 key，視為未知，不比較）。
- 金額 = 交易規模（正數分錄加總），與列表、明細同一個定義；增加／減少依 NORMAL_SIGN。
- 名稱用「現在」的主資料（快照只存 id 與科目代碼）：科目改名後舊紀錄也顯示新名稱，
  但一律保留快照裡的科目代碼；查不到時退回代碼或 #id（P2 Step 4B 決策）。
"""

import json
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.models import Merchant, PaymentMethod
from app.models.enums import AuditAction, LineRole
from app.repositories import accounts as account_repo
from app.repositories import audit as audit_repo
from app.repositories import tags as tag_repo
from app.services.entry_options import VOUCHER_LABELS
from app.services.ledger import AUDIT_ENTITY
from app.services.ledger_queries import NORMAL_SIGN

ACTION_LABELS = {
    AuditAction.CREATE: "建立交易",
    AuditAction.UPDATE: "修改交易",
    AuditAction.DELETE: "刪除交易",
    AuditAction.RESTORE: "還原交易",
}
ROLE_LABELS = {LineRole.FEE: "手續費", LineRole.INTEREST: "分期利息"}
NOTHING = "（無）"

# UPDATE 摘要逐一比較的表頭欄位；其他表頭欄位只用來避免誤判「內容沒有變更」
_SUMMARIZED = (
    "txn_date",
    "merchant_id",
    "payment_method_id",
    "description",
    "voucher_type",
    "invoice_no",
    "exclude_from_budget",
)
_IGNORED = {"id", "postings", "tag_ids"}  # tag_ids 另外比較（P2 Step 8A）


@dataclass(frozen=True)
class HistoryEntry:
    occurred_at: datetime
    action: str
    label: str
    changes: tuple[str, ...]


def magnitude(snapshot: dict[str, Any]) -> int:
    """快照的交易規模：正數分錄加總。"""
    return sum(p["amount"] for p in snapshot.get("postings", []) if p["amount"] > 0)


class _Names:
    """依 id 查現在的主資料名稱（同一次查詢內快取）。"""

    def __init__(self, db: Session) -> None:
        self.db = db
        self._cache: dict[tuple[str, int], Any] = {}

    def _get(self, kind: str, key: int, load):
        if (kind, key) not in self._cache:
            self._cache[(kind, key)] = load(key)
        return self._cache[(kind, key)]

    def account(self, account_id: int):
        return self._get("account", account_id, lambda k: account_repo.get(self.db, k))

    def merchant(self, merchant_id: int | None) -> str:
        if merchant_id is None:
            return NOTHING
        m = self._get("merchant", merchant_id, lambda k: self.db.get(Merchant, k))
        return m.name if m is not None else f"#{merchant_id}"

    def tags(self, tag_ids: list[int]) -> str:
        """標籤用現在的名稱（同科目、商家）；查不到顯示 #id。"""
        if not tag_ids:
            return NOTHING
        missing = [t for t in tag_ids if ("tag", t) not in self._cache]
        if missing:
            found = tag_repo.get_many(self.db, missing)
            for t in missing:
                self._cache[("tag", t)] = found.get(t)
        labels = []
        for t in tag_ids:
            tag = self._cache[("tag", t)]
            labels.append(tag.name if tag is not None else f"#{t}")
        return "、".join(labels)

    def payment_method(self, pm_id: int | None) -> str:
        if pm_id is None:
            return NOTHING
        pm = self._get("pm", pm_id, lambda k: self.db.get(PaymentMethod, k))
        return pm.name if pm is not None else f"#{pm_id}"


def _posting_text(names: _Names, posting: dict[str, Any]) -> str:
    """「5101 正餐 增加 120」；查不到科目時退回代碼與金額（無法判斷增加／減少）。"""
    code = posting.get("account_code")
    account = names.account(posting["account_id"])
    amount = abs(posting["amount"])
    role = ROLE_LABELS.get(posting.get("line_role"))
    suffix = f"（{role}）" if role else ""
    if account is None:
        label = code or f"#{posting['account_id']}"
        return f"{label} {amount:,}{suffix}"
    label = f"{code or account.code} {account.name}"
    signed = posting["amount"] * NORMAL_SIGN[account.account_class]
    effect = "增加" if signed > 0 else "減少"
    return f"{label} {effect} {amount:,}{suffix}"


def _postings_text(names: _Names, snapshot: dict[str, Any]) -> str:
    ordered = sorted(snapshot.get("postings", []), key=lambda p: p.get("line_no", 0))
    return "、".join(_posting_text(names, p) for p in ordered) or NOTHING


def _full_identity(posting: dict[str, Any]) -> tuple:
    """判斷分錄是否改變：除了 line_no 以外的全部欄位（含 memo、原幣）。"""
    return tuple(sorted((k, v) for k, v in posting.items() if k != "line_no"))


def _core_identity(posting: dict[str, Any]) -> tuple:
    """決定怎麼寫摘要：科目、金額、分錄角色。"""
    return (
        posting.get("account_code"),
        posting["account_id"],
        posting["amount"],
        posting.get("line_role"),
    )


def _bag(snapshot: dict[str, Any], key) -> Counter:
    return Counter(key(p) for p in snapshot.get("postings", []))


def _fmt(field: str, value: Any, names: _Names) -> str:
    if field == "merchant_id":
        return names.merchant(value)
    if field == "payment_method_id":
        return names.payment_method(value)
    if field == "voucher_type":
        return VOUCHER_LABELS.get(value, value) if value else NOTHING
    if field == "exclude_from_budget":
        return "是" if value else "否"
    if value is None or value == "":
        return NOTHING
    return str(value)


_FIELD_LABELS = {
    "txn_date": "日期",
    "merchant_id": "商家",
    "payment_method_id": "付款方式",
    "description": "備註",
    "voucher_type": "憑證類型",
    "invoice_no": "發票號碼",
    "exclude_from_budget": "不計入預算",
}


def update_changes(names: _Names, before: dict[str, Any], after: dict[str, Any]) -> tuple[str, ...]:
    changes: list[str] = []
    if before.get("txn_date") != after.get("txn_date"):
        changes.append(f"日期：{before.get('txn_date')} → {after.get('txn_date')}")
    old_amount, new_amount = magnitude(before), magnitude(after)
    if old_amount != new_amount:
        changes.append(f"金額：{old_amount:,} → {new_amount:,}")
    for field in _SUMMARIZED[1:]:
        if before.get(field) != after.get(field):
            old = _fmt(field, before.get(field), names)
            new = _fmt(field, after.get(field), names)
            changes.append(f"{_FIELD_LABELS[field]}：{old} → {new}")

    if _bag(before, _full_identity) != _bag(after, _full_identity):
        if _bag(before, _core_identity) != _bag(after, _core_identity):
            changes.append(
                f"分錄：{_postings_text(names, before)} → {_postings_text(names, after)}"
            )
        else:
            changes.append("分錄內容已變更")

    # 標籤（P2 Step 8A）：只有前後兩份快照都有 tag_ids 才比較。
    # 舊紀錄沒有這個 key 代表「未知」，不是空集合，不能誤報「標籤：（無）→ A」。
    if "tag_ids" in before and "tag_ids" in after:
        old_tags, new_tags = sorted(before["tag_ids"]), sorted(after["tag_ids"])
        if old_tags != new_tags:
            changes.append(f"標籤：{names.tags(old_tags)} → {names.tags(new_tags)}")

    others = (set(before) | set(after)) - _IGNORED - set(_SUMMARIZED)
    if any(before.get(k) != after.get(k) for k in sorted(others)):
        changes.append("其他欄位已變更")

    return tuple(changes) or ("內容沒有變更",)


def _load(text: str | None) -> dict[str, Any]:
    return json.loads(text) if text else {}


def history(db: Session, txn_id: int) -> tuple[HistoryEntry, ...]:
    names = _Names(db)
    entries = []
    for log in audit_repo.list_for(db, AUDIT_ENTITY, txn_id):
        before, after = _load(log.before_json), _load(log.after_json)
        if log.action == AuditAction.CREATE:
            changes: tuple[str, ...] = (f"金額 {magnitude(after):,}",)
        elif log.action == AuditAction.UPDATE:
            changes = update_changes(names, before, after)
        else:
            changes = ()
        entries.append(
            HistoryEntry(
                occurred_at=datetime.fromisoformat(log.occurred_at),
                action=log.action,
                label=ACTION_LABELS.get(log.action, log.action),
                changes=changes,
            )
        )
    return tuple(entries)
