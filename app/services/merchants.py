"""商家：名稱正規化與比對（規格 6.4、7.1 merchants.py；P2 Step 2）。

正規化（規格 6.4 merchant_aliases.alias_normalized）只做三件事，結果可預測：
1. 全形 ASCII（U+FF01–U+FF5E）轉半形，全形空白（U+3000）轉一般空白
2. 去除所有空白
3. 轉小寫
不做 NFKC：NFKC 會連帶改變其他相容字元（例如 ① → 1、㈱ → (株)），超出規格。

比對（resolve）：
- 先用 alias_normalized 找別名（資料庫 UNIQUE，M06）。
- 再比對商家標準名稱：merchants 沒有 name_normalized 欄位（不為此新增欄位／migration），
  所以把所有商家名稱在 Python 端正規化後比對。單一使用者的商家數量很少，這個成本可以接受。
- 停用的商家也會被找到，交給 P1 的 V04 回報「已停用」，不在這裡自行換成新商家。

快速記帳找不到商家時，以使用者輸入的名稱（去頭尾空白）建立新商家（P2 Step 2 決策 M2），
不另外建立別名：標準名稱本身已經能被比對到；「真正不同的別名」由 /merchants 新增（P2 Step 9）。

別名（P2 Step 9）：新增時依序檢查（見 add_alias），保證任何一個正規化字串最多只對應一個商家，
resolve() 的結果才會唯一。刪除別名只刪那一列，交易引用的是 merchant_id，不受影響。
"""

from dataclasses import dataclass

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Merchant, MerchantAlias
from app.repositories import merchants as merchant_repo
from app.services.errors import NotFound, RuleViolation

_FULLWIDTH_START = 0xFF01
_FULLWIDTH_END = 0xFF5E
_FULLWIDTH_OFFSET = 0xFEE0
_IDEOGRAPHIC_SPACE = "　"


def _to_halfwidth(ch: str) -> str:
    code = ord(ch)
    if _FULLWIDTH_START <= code <= _FULLWIDTH_END:
        return chr(code - _FULLWIDTH_OFFSET)
    if ch == _IDEOGRAPHIC_SPACE:
        return " "
    return ch


def normalize(name: str) -> str:
    halfwidth = "".join(_to_halfwidth(ch) for ch in name)
    return "".join(ch for ch in halfwidth if not ch.isspace()).lower()


def resolve(db: Session, name: str) -> Merchant | None:
    """依名稱找商家：別名優先，其次是標準名稱。找不到回傳 None。"""
    key = normalize(name)
    if not key:
        return None
    merchant = merchant_repo.get_by_alias_normalized(db, key)
    if merchant is not None:
        return merchant
    return next((m for m in merchant_repo.list_all(db) if normalize(m.name) == key), None)


def resolve_or_create(db: Session, name: str) -> Merchant:
    """找得到就回傳既有商家；找不到就建立並 commit。

    必須在 LedgerService 寫入交易之前 commit（P1 Q3：post() 拒絕有未提交變更的 Session），
    所以交易之後若被 422／409 擋下，這個商家會留著，沒有交易引用（決策 M2 接受）。
    """
    merchant = resolve(db, name)
    if merchant is not None:
        return merchant
    if not normalize(name):
        raise ValueError("商家名稱不能是空白")
    merchant = Merchant(name=name.strip())
    try:
        merchant_repo.add(db, merchant)
        db.commit()
    except IntegrityError:
        # 同名商家剛好被另一個請求建立（merchants.name UNIQUE）：改用那一筆
        db.rollback()
        existing = resolve(db, name)
        if existing is None:
            raise
        return existing
    return merchant


# --- 別名（P2 Step 9，F17）---------------------------------------------------------------


@dataclass(frozen=True)
class AliasView:
    id: int
    alias: str


@dataclass(frozen=True)
class MerchantView:
    id: int
    name: str
    is_active: bool
    aliases: tuple[AliasView, ...]


@dataclass(frozen=True)
class MerchantOption:
    """<datalist> 的一個選項。canonical 有值代表這是別名，畫面可提示對應的標準名稱。"""

    value: str
    canonical: str | None = None


def list_with_aliases(db: Session) -> list[MerchantView]:
    """/merchants 用：全部商家（含停用）與各自的別名，依商家名稱排序。"""
    by_merchant: dict[int, list[AliasView]] = {}
    for alias in merchant_repo.list_aliases(db):
        by_merchant.setdefault(alias.merchant_id, []).append(AliasView(alias.id, alias.alias))
    return [
        MerchantView(m.id, m.name, m.is_active, tuple(by_merchant.get(m.id, ())))
        for m in merchant_repo.list_all(db)
    ]


def get_view(db: Session, merchant_id: int) -> MerchantView:
    merchant = _get_merchant(db, merchant_id)
    aliases = tuple(
        AliasView(a.id, a.alias)
        for a in merchant_repo.list_aliases(db)
        if a.merchant_id == merchant.id
    )
    return MerchantView(merchant.id, merchant.name, merchant.is_active, aliases)


def suggestions(db: Session) -> list[MerchantOption]:
    """商家欄位 <datalist> 用：啟用中商家的標準名稱與別名；停用商家與它的別名都不列。

    以正規化後的字串去重（先出現的保留，標準名稱排在自己的別名前面）。
    """
    aliases: dict[int, list[str]] = {}
    for alias in merchant_repo.list_aliases(db):
        aliases.setdefault(alias.merchant_id, []).append(alias.alias)
    options: list[MerchantOption] = []
    seen: set[str] = set()
    for m in merchant_repo.list_all(db, active_only=True):
        for option in (
            MerchantOption(m.name),
            *(MerchantOption(a, canonical=m.name) for a in aliases.get(m.id, ())),
        ):
            key = normalize(option.value)
            if key and key not in seen:
                seen.add(key)
                options.append(option)
    return options


def _get_merchant(db: Session, merchant_id: int) -> Merchant:
    merchant = merchant_repo.get(db, merchant_id)
    if merchant is None:
        raise NotFound("商家", merchant_id)
    return merchant


def _check_new_alias(db: Session, merchant: Merchant, text: str) -> str:
    """回傳正規化後的別名；不能新增時丟 RuleViolation。檢查順序見 add_alias。"""
    if not merchant.is_active:
        raise RuleViolation(
            "ALIAS_MERCHANT_INACTIVE", f"商家「{merchant.name}」已停用，不能新增別名"
        )
    key = normalize(text)
    if not key:
        raise RuleViolation("ALIAS_BLANK", "請輸入別名")
    existing = merchant_repo.get_alias_by_normalized(db, key)
    if existing is not None:
        if existing.merchant_id == merchant.id:
            raise RuleViolation("M06", f"「{existing.alias}」已經是這個商家的別名")
        raise RuleViolation(
            "M06", f"「{existing.alias}」已經是商家「{existing.merchant.name}」的別名"
        )
    if normalize(merchant.name) == key:
        raise RuleViolation("ALIAS_SAME_AS_NAME", "和商家名稱相同，不需要另外加別名")
    other = next(
        (m for m in merchant_repo.list_all(db) if m.id != merchant.id and normalize(m.name) == key),
        None,
    )
    if other is not None:
        raise RuleViolation("ALIAS_NAME_CONFLICT", f"已經有商家叫「{other.name}」，不能當成別名")
    return key


def add_alias(db: Session, merchant_id: int, text: str) -> MerchantAlias:
    """幫商家新增別名並 commit。

    檢查順序：商家存在 → 商家啟用中 → 不是空白 → 不和既有別名重複（M06，任何商家）
    → 不等於自己的標準名稱 → 不等於其他商家（含停用）的標準名稱。
    最後一項確保 resolve() 不會因為「A 的別名 = B 的名稱」而變成看別名先後決定。
    保存去頭尾空白後的原文與正規化字串。
    """
    merchant = _get_merchant(db, merchant_id)
    key = _check_new_alias(db, merchant, text)
    alias = MerchantAlias(merchant_id=merchant.id, alias=text.strip(), alias_normalized=key)
    try:
        merchant_repo.add_alias(db, alias)
        db.commit()
    except IntegrityError:
        # 同一個別名剛好被另一個請求加上（alias_normalized UNIQUE）：重新檢查，回報 M06
        db.rollback()
        _check_new_alias(db, _get_merchant(db, merchant_id), text)
        raise
    return alias


def remove_alias(db: Session, merchant_id: int, alias_id: int) -> None:
    """刪除別名並 commit（停用商家的別名也可以刪）。只刪別名這一列，交易不受影響。"""
    merchant = _get_merchant(db, merchant_id)
    alias = merchant_repo.get_alias(db, alias_id)
    if alias is None or alias.merchant_id != merchant.id:
        raise NotFound("別名", alias_id)
    merchant_repo.delete_alias(db, alias)
    db.commit()
