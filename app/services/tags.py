"""標籤（P2 Step 8A 讀取；8B 正規化、解析與建立）。

- 正規化（tags 專用，不共用商家的規則：標籤中間的空白可能是分詞）：
  全形 ASCII → 半形、全形空白 → 一般空白、去頭尾空白、連續空白縮成一個；比對時轉小寫。
  不做 NFKC（①、㈱ 等保持原樣）。「Tokyo Trip」「Ｔｏｋｙｏ　Ｔｒｉｐ」「tokyo  trip」相同，
  「TokyoTrip」是另一個標籤。
- 保存的名稱：第一次建立時正規化（不含轉小寫）後的文字，保留大小寫。
- 比對：沒有 normalized 欄位，讀出全部標籤在 Python 比較（單人使用、標籤數量少）。
- 建立（resolve_or_create_many）：找不到就建立並 commit，必須在 LedgerService 寫入交易之前
  （post() 拒絕有未提交變更的 Session）。交易之後若被 422／409 擋下，新標籤會留著、沒有交易引用；
  和商家相同的取捨（決策 M2），是主資料殘留，不是帳務錯誤。
- 找到的標籤若已停用，回傳那一筆，不另建新的；新加上停用標籤由 P1 回 V04。
- 寫入 transaction_tags 只在 LedgerService（ADR-007）。
"""

import re

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Tag
from app.repositories import tags as tag_repo

# 標籤欄位的分隔符號：頓號、半形逗號、全形逗號（標籤名稱本身不能包含這些字元）
SEPARATORS = re.compile(r"[、,，]")
_SPACES = re.compile(r"\s+")


def _halfwidth(ch: str) -> str:
    code = ord(ch)
    if code == 0x3000:  # 全形空白
        return " "
    if 0xFF01 <= code <= 0xFF5E:  # 全形 ASCII
        return chr(code - 0xFEE0)
    return ch


def normalize(name: str) -> str:
    """保存用的名稱：半形化、去頭尾空白、連續空白縮成一個；保留大小寫。"""
    halfwidth = "".join(_halfwidth(ch) for ch in name)
    return _SPACES.sub(" ", halfwidth).strip()


def key(name: str) -> str:
    """比對用的鍵：正規化後轉小寫。"""
    return normalize(name).lower()


def parse(text: str | None) -> list[str]:
    """把標籤欄位解析成名稱清單：依分隔符號切開、正規化、忽略空片段、
    以比對鍵去重（保留第一次出現的寫法與順序）。"""
    names: list[str] = []
    seen: set[str] = set()
    for part in SEPARATORS.split(text or ""):
        name = normalize(part)
        if name and key(name) not in seen:
            seen.add(key(name))
            names.append(name)
    return names


def resolve(db: Session, name: str) -> Tag | None:
    """依比對鍵找標籤（含已停用）；找不到回傳 None。不建立。"""
    target = key(name)
    if not target:
        return None
    return next((t for t in tag_repo.list_all(db) if key(t.name) == target), None)


def resolve_or_create_many(db: Session, names: list[str]) -> tuple[int, ...]:
    """每個名稱找得到就用既有標籤，找不到就建立並 commit；回傳 id（排序、去重）。"""
    existing = {key(t.name): t for t in tag_repo.list_all(db)}
    ids: set[int] = set()
    for name in names:
        tag = existing.get(key(name))
        if tag is None:
            tag = _create(db, name)
            existing[key(tag.name)] = tag
        ids.add(tag.id)
    return tuple(sorted(ids))


def _create(db: Session, name: str) -> Tag:
    tag = Tag(name=normalize(name))
    try:
        tag_repo.add(db, tag)
        db.commit()
    except IntegrityError:
        # 同名標籤剛好被另一個請求建立（tags.name UNIQUE）：改用那一筆
        db.rollback()
        found = resolve(db, name)
        if found is None:
            raise
        return found
    return tag


def active_names(db: Session) -> list[str]:
    """標籤欄位 <datalist> 用：只列啟用中的標籤。"""
    return [t.name for t in tag_repo.list_all(db, active_only=True)]


def tags_for_transaction(db: Session, txn_id: int) -> list[Tag]:
    """交易目前的標籤（依名稱排序），含已停用的標籤；已刪除的交易也讀得到。"""
    return tag_repo.tags_for_transaction(db, txn_id)


def text_for_transaction(db: Session, txn_id: int) -> str:
    """修改表單的預填文字（含已停用的標籤，保留它們不會被當成新加上）。"""
    return "、".join(t.name for t in tags_for_transaction(db, txn_id))
