"""帳務資料表使用的列舉值（規格第 6 節）。

資料庫以 TEXT 儲存，並用 CHECK 限制可用的值；這裡是 Python 端的唯一來源。
migration 會把當下的值寫成字面常數（migration 不 import app 程式碼，
避免日後改列舉時連帶改到舊 migration），兩邊是否一致由 test_ledger_schema.py 把關。
"""

from enum import StrEnum


class AccountClass(StrEnum):
    ASSET = "ASSET"
    LIABILITY = "LIABILITY"
    EQUITY = "EQUITY"
    INCOME = "INCOME"
    EXPENSE = "EXPENSE"


class AccountSubtype(StrEnum):
    CASH = "CASH"
    BANK = "BANK"
    CREDIT_CARD = "CREDIT_CARD"
    RECEIVABLE = "RECEIVABLE"
    PAYABLE = "PAYABLE"
    PREPAID_TAX = "PREPAID_TAX"
    PENSION = "PENSION"
    INVESTMENT = "INVESTMENT"  # ETF 預留
    OTHER = "OTHER"


class TaxCategory(StrEnum):
    SALARY = "SALARY"
    INTEREST = "INTEREST"
    INSURANCE = "INSURANCE"
    MEDICAL = "MEDICAL"
    WITHHOLDING = "WITHHOLDING"
    PENSION_SELF = "PENSION_SELF"


class SystemAccountKey(StrEnum):
    """系統科目識別（決策 D2）：程式用它找科目，不依賴固定 id。"""

    OPENING_EQUITY = "OPENING_EQUITY"  # 權益-期初權益
    ADJUSTMENT = "ADJUSTMENT"  # 權益-帳務調整


class TxnType(StrEnum):
    EXPENSE = "EXPENSE"
    INCOME = "INCOME"
    TRANSFER = "TRANSFER"
    CARD_PAYMENT = "CARD_PAYMENT"
    REFUND = "REFUND"
    PAYROLL = "PAYROLL"
    OPENING = "OPENING"
    ADJUSTMENT = "ADJUSTMENT"
    MANUAL = "MANUAL"


class TxnStatus(StrEnum):
    POSTED = "POSTED"
    DRAFT = "DRAFT"


class VoucherType(StrEnum):
    EINVOICE = "EINVOICE"
    PAPER_INVOICE = "PAPER_INVOICE"
    RECEIPT = "RECEIPT"
    NONE = "NONE"


class TxnSource(StrEnum):
    MANUAL = "MANUAL"
    RECURRING = "RECURRING"
    CARD_AUTOPAY = "CARD_AUTOPAY"
    IMPORT = "IMPORT"


class LineRole(StrEnum):
    MAIN = "MAIN"
    FEE = "FEE"
    INTEREST = "INTEREST"
    TAX = "TAX"
    INSURANCE = "INSURANCE"
    PENSION = "PENSION"


class AuditAction(StrEnum):
    CREATE = "CREATE"
    UPDATE = "UPDATE"
    DELETE = "DELETE"
    RESTORE = "RESTORE"


def sql_in(column: str, values: type[StrEnum]) -> str:
    """產生 CHECK 用的 `column IN ('A', 'B')` 片段。"""
    quoted = ", ".join(f"'{v.value}'" for v in values)
    return f"{column} IN ({quoted})"
