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
不另外建立別名：標準名稱本身已經能被比對到；「真正不同的別名」由日後的商家管理頁新增。
"""

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Merchant
from app.repositories import merchants as merchant_repo

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


def active_names(db: Session) -> list[str]:
    """記帳表單 <datalist> 用：啟用中的商家標準名稱。"""
    return [m.name for m in merchant_repo.list_all(db, active_only=True)]
