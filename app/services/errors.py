"""服務層共用的規則錯誤。

主資料規則（規格 9.2 的 M01–M08，以及 P1 拍板的補充規則）違反時一律丟 RuleViolation：
- code：規則編號（例如 "M03"）或補充規則代碼（例如 "S9"），給測試與日後的介面判斷用。
- message：可以直接顯示給使用者看的中文說明。
"""


class RuleViolation(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def __repr__(self) -> str:
        return f"RuleViolation({self.code!r}, {self.message!r})"


class NotFound(RuleViolation):
    def __init__(self, what: str, key: object) -> None:
        super().__init__("NOT_FOUND", f"找不到{what}：{key}")


class MissingSystemAccount(RuntimeError):
    """找不到系統科目（例如還沒執行 seed）。這是系統／主資料異常，不是使用者輸入錯誤（R6）。"""

    def __init__(self, system_key: str) -> None:
        super().__init__(
            f"找不到系統科目 {system_key}，請先執行 python scripts/seed_master_data.py"
        )
        self.system_key = system_key


class InvalidSetting(RuntimeError):
    """系統設定存在但內容不合法。這是設定資料異常，不靜默改用預設值。

    放在 errors.py 而不是 validation.py：budgets.py 要讀設定，而 validation.py 之後會匯入 budgets，
    定義在 validation.py 會形成循環匯入。validation.py 仍然 re-export，既有的匯入不受影響。
    """

    def __init__(self, key: str, value: object, expected: str = "正整數") -> None:
        super().__init__(f"系統設定 {key} 的值不合法：{value!r}（必須是{expected}）")
        self.key = key
        self.value = value
