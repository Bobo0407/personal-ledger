"""驗證問題的資料結構：builder.check()、validation.py、txn_structure.py 共用。"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Issue:
    code: str  # 規則代碼，例如 "V02"、"TXN_STRUCTURE"
    message: str  # 可以直接顯示給使用者的中文說明
    line: int | None = None  # 第幾筆分錄（從 1 開始）；表頭或表單欄位為 None
    field: str | None = None  # 對應的表單欄位，例如 "amount"；供 P2 的 422 在欄位旁顯示（P10）
