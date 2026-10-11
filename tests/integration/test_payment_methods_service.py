"""付款方式（規格 6.3、9.2 M08；決策 D4、S6-a）。"""

import pytest
from sqlalchemy import select

from app.models import PaymentMethod, Setting
from app.repositories import accounts as account_repo
from app.services import accounts as account_service
from app.services import payment_methods as svc
from app.services.errors import RuleViolation


def code_id(db, code: str) -> int:
    return account_repo.get_by_code(db, code).id


def pm_named(db, name: str) -> PaymentMethod:
    return db.scalar(select(PaymentMethod).where(PaymentMethod.name == name))


def defaults(db) -> list[str]:
    return [
        pm.name
        for pm in db.scalars(select(PaymentMethod).where(PaymentMethod.is_default.is_(True)))
    ]


def assert_rule(rule: str, fn, /, *args, **kwargs) -> None:
    with pytest.raises(RuleViolation) as exc_info:
        fn(*args, **kwargs)
    assert exc_info.value.code == rule, exc_info.value.message


def test_create_payment_method(seeded_db):
    pm = svc.create_payment_method(
        seeded_db, name=" 街口支付 ", account_id=code_id(seeded_db, "1102")
    )
    assert pm.name == "街口支付"
    assert pm.is_default is False
    assert pm.is_active is True


@pytest.mark.parametrize("code", ["5101", "4101", "3101", "5100"])
def test_account_must_be_asset_or_liability_leaf(seeded_db, code):
    assert_rule(
        "PM_ACCOUNT",
        svc.create_payment_method,
        seeded_db,
        name="錯誤",
        account_id=code_id(seeded_db, code),
    )


def test_account_must_be_active(seeded_db):
    other = account_service.create_account(
        seeded_db, code="1104", name="其他銀行", account_class="ASSET", subtype="BANK"
    )
    account_service.deactivate_account(seeded_db, other.id)
    assert_rule(
        "PM_ACCOUNT", svc.create_payment_method, seeded_db, name="其他", account_id=other.id
    )


def test_name_must_be_unique(seeded_db):
    assert_rule(
        "PM_NAME",
        svc.create_payment_method,
        seeded_db,
        name="現金",
        account_id=code_id(seeded_db, "1101"),
    )


def test_m08_set_default_unsets_old(seeded_db):
    svc.set_default(seeded_db, pm_named(seeded_db, "現金").id)
    assert defaults(seeded_db) == ["現金"]
    assert svc.get_default(seeded_db).name == "現金"


def test_m08_create_as_default_unsets_old(seeded_db):
    svc.create_payment_method(
        seeded_db, name="街口支付", account_id=code_id(seeded_db, "1102"), is_default=True
    )
    assert defaults(seeded_db) == ["街口支付"]


def test_default_lives_only_in_payment_methods(seeded_db):
    # 決策 D4：settings 不保存預設付款方式
    svc.set_default(seeded_db, pm_named(seeded_db, "現金").id)
    assert seeded_db.get(Setting, "default_payment_method_id") is None


def test_cannot_deactivate_default(seeded_db):
    assert_rule("M08", svc.deactivate_payment_method, seeded_db, pm_named(seeded_db, "LINE Pay").id)


def test_inactive_cannot_become_default(seeded_db):
    pm = pm_named(seeded_db, "台灣Pay")
    svc.deactivate_payment_method(seeded_db, pm.id)
    assert_rule("M08", svc.set_default, seeded_db, pm.id)
    assert defaults(seeded_db) == ["LINE Pay"]


def test_deactivate_and_activate(seeded_db):
    pm = pm_named(seeded_db, "台灣Pay")
    assert svc.deactivate_payment_method(seeded_db, pm.id).is_active is False
    assert pm.name not in [p.name for p in svc.list_payment_methods(seeded_db)]
    assert svc.activate_payment_method(seeded_db, pm.id).is_active is True


def test_change_account(seeded_db):
    # S6-a：扣款帳戶允許日後修改
    pm = svc.change_account(
        seeded_db, pm_named(seeded_db, "台灣Pay").id, code_id(seeded_db, "1103")
    )
    assert pm.account_id == code_id(seeded_db, "1103")


def test_change_account_validates_target(seeded_db):
    assert_rule(
        "PM_ACCOUNT",
        svc.change_account,
        seeded_db,
        pm_named(seeded_db, "台灣Pay").id,
        code_id(seeded_db, "5101"),
    )
