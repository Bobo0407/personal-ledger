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
