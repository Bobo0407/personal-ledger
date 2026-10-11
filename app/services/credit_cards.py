"""信用卡設定（規格 6.2、7.1 credit_cards.py、9.2 M07；P1 決策 S1、S1-a、S1-b）。

- 信用卡帳戶（subtype = CREDIT_CARD）可以先存在、暫時沒有設定，視為「尚未設定信用卡週期」；
  這段期間仍可正常記帳，因為分錄不依賴結帳日。
- M07 只約束「建立或修改設定」的那一刻：
  結帳日、繳款日必填（1–31）；扣繳帳戶若有設定，必須是啟用中、最末層的 BANK 資產帳戶。
- 帳單週期、本期應繳（current_cycle、amount_due）在 P3 實作；沒有設定時不推測週期。
"""

from sqlalchemy.orm import Session

from app.models import CreditCardProfile
from app.models.enums import AccountClass, AccountSubtype
from app.repositories import accounts as account_repo
from app.services.accounts import get_account
from app.services.errors import RuleViolation


def check_day(value: int | None, what: str) -> int:
    if value is None:
        raise RuleViolation("M07", f"信用卡必須設定{what}")
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 31:
        raise RuleViolation("M07", f"{what}必須是 1 到 31 的整數")
    return value


def get_profile(db: Session, card_account_id: int) -> CreditCardProfile | None:
    """None 代表「尚未設定信用卡週期」。"""
    return db.get(CreditCardProfile, card_account_id)


def set_profile(
    db: Session,
    card_account_id: int,
    *,
    closing_day: int | None,
    due_day: int | None,
    autopay_account_id: int | None = None,
    credit_limit: int | None = None,
) -> CreditCardProfile:
    """建立或修改信用卡設定。autopay_account_id、credit_limit 傳 None 代表不設定（或清除）。"""
    card = get_account(db, card_account_id)
    if card.subtype != AccountSubtype.CREDIT_CARD:
        raise RuleViolation("NOT_CREDIT_CARD", f"「{card.name}」不是信用卡帳戶")
    if not card.is_active:
        raise RuleViolation("INACTIVE", f"「{card.name}」已停用，不能修改信用卡設定")
    closing_day = check_day(closing_day, "結帳日")
    due_day = check_day(due_day, "繳款日")

    if autopay_account_id is not None:
        autopay = get_account(db, autopay_account_id)
        if (
            autopay.account_class != AccountClass.ASSET
            or autopay.subtype != AccountSubtype.BANK
            or not autopay.is_active
            or account_repo.has_children(db, autopay.id)
        ):
            raise RuleViolation("M07", "扣繳帳戶必須是啟用中、最末層的銀行（BANK）帳戶")

    if credit_limit is not None and (
        isinstance(credit_limit, bool) or not isinstance(credit_limit, int) or credit_limit <= 0
    ):
        raise RuleViolation("CREDIT_LIMIT", "信用額度必須是大於 0 的整數")

    profile = get_profile(db, card.id)
    if profile is None:
        profile = CreditCardProfile(account_id=card.id, closing_day=closing_day, due_day=due_day)
        db.add(profile)
    profile.closing_day = closing_day
    profile.due_day = due_day
    profile.autopay_account_id = autopay_account_id
    profile.credit_limit = credit_limit
    db.commit()
    return profile
