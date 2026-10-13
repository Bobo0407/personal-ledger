"""標籤：schema、LedgerService 原子寫入、修改語意、生命週期、稽核（P2 Step 8A、ADR-007）。"""

import json

import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError

from app.models import AuditLog, PaymentMethod, Posting, Tag, Transaction, TransactionTag
from app.repositories import tags as tag_repo
from app.services import audit, audit_history, tags, transactions
from app.services.entries import ExpenseForm, IncomeForm
from app.services.validation import ValidationFailed
from tests.ledger_helpers import TODAY, acc, freeze_today

TOKEN = "g" * 32


@pytest.fixture(autouse=True)
def _today(monkeypatch):
    freeze_today(monkeypatch)


@pytest.fixture
def t(seeded_db) -> dict[str, int]:
    """旅行、聚餐、演唱會（啟用）與 舊標籤（停用）。"""
    rows = [Tag(name=n) for n in ("旅行", "聚餐", "演唱會")] + [Tag(name="舊標籤", is_active=False)]
    seeded_db.add_all(rows)
    seeded_db.commit()
    return {r.name: r.id for r in rows}


def pm(db) -> int:
    return db.scalar(select(PaymentMethod.id).where(PaymentMethod.name == "LINE Pay"))


def form(db, amount=120, **kwargs) -> ExpenseForm:
    return ExpenseForm(TODAY, amount, acc(db, "5101"), pm(db), **kwargs)


def create(db, tag_ids=None, **kwargs) -> int:
    return transactions.record_expense(db, form(db), tag_ids=tag_ids, **kwargs).transaction_id


def ids(db, txn_id) -> tuple[int, ...]:
    return tag_repo.ids_for_transaction(db, txn_id)


def count(db, model) -> int:
    return db.scalar(select(func.count()).select_from(model))


def logs(db, txn_id) -> list[AuditLog]:
    return list(
        db.scalars(select(AuditLog).where(AuditLog.entity_id == txn_id).order_by(AuditLog.id))
    )


def codes(exc_info) -> list[tuple[str, str | None]]:
    return [(i.code, i.field) for i in exc_info.value.result.errors]


# --- schema（SQLite FK 要真的生效） --------------------------------------------------------


def test_duplicate_tag_name(seeded_db, t):
    seeded_db.add(Tag(name="旅行"))
    with pytest.raises(IntegrityError):
        seeded_db.commit()
    seeded_db.rollback()


def test_duplicate_relation(seeded_db, t):
    txn_id = create(seeded_db, [t["旅行"]])
    seeded_db.add(TransactionTag(transaction_id=txn_id, tag_id=t["旅行"]))
    with pytest.raises(IntegrityError):
        seeded_db.commit()
    seeded_db.rollback()


@pytest.mark.parametrize("bad", ["transaction", "tag"])
def test_bad_foreign_keys(seeded_db, t, bad):
    txn_id = create(seeded_db)
    link = (
        TransactionTag(transaction_id=99999, tag_id=t["旅行"])
        if bad == "transaction"
        else TransactionTag(transaction_id=txn_id, tag_id=99999)
    )
    seeded_db.add(link)
    with pytest.raises(IntegrityError):
        seeded_db.commit()
    seeded_db.rollback()


def test_referenced_tag_cannot_be_hard_deleted(seeded_db, t):
    create(seeded_db, [t["旅行"]])
    with pytest.raises(IntegrityError):
        seeded_db.execute(delete(Tag).where(Tag.id == t["旅行"]))
        seeded_db.commit()
    seeded_db.rollback()
    # 沒被引用的可以刪（規格沒有標籤刪除流程；這裡只確認 FK 行為）
    seeded_db.execute(delete(Tag).where(Tag.id == t["聚餐"]))
    seeded_db.commit()


# --- 新增 ------------------------------------------------------------------------------


def test_create_with_tags_dedupes_and_sorts(seeded_db, t):
    txn_id = create(seeded_db, [t["聚餐"], t["旅行"], t["聚餐"]])
    assert ids(seeded_db, txn_id) == tuple(sorted({t["旅行"], t["聚餐"]}))
    [log] = logs(seeded_db, txn_id)
    assert json.loads(log.after_json)["tag_ids"] == sorted([t["旅行"], t["聚餐"]])


def test_create_without_tags(seeded_db, t):
    txn_id = create(seeded_db)
    assert ids(seeded_db, txn_id) == ()
    assert json.loads(logs(seeded_db, txn_id)[0].after_json)["tag_ids"] == []


def test_create_invalid_and_inactive_tags(seeded_db, t):
    with pytest.raises(ValidationFailed) as exc:
        create(seeded_db, [99999])
    assert codes(exc) == [("V05", "tag_ids")]
    with pytest.raises(ValidationFailed) as exc:
        create(seeded_db, [t["舊標籤"]])
    assert codes(exc) == [("V04", "tag_ids")]
    assert count(seeded_db, Transaction) == 0


def test_income_and_transfer_accept_tags(seeded_db, t):
    income = transactions.record_income(
        seeded_db,
        IncomeForm(TODAY, 100, acc(seeded_db, "4201"), acc(seeded_db, "1102")),
        tag_ids=[t["旅行"]],
    ).transaction_id
    assert ids(seeded_db, income) == (t["旅行"],)


def test_replay_does_not_duplicate_relations(seeded_db, t):
    first = transactions.record_expense(
        seeded_db, form(seeded_db), tag_ids=[t["旅行"]], submission_token=TOKEN
    )
    again = transactions.record_expense(
        seeded_db, form(seeded_db), tag_ids=[t["聚餐"]], submission_token=TOKEN
    )
    assert again.replayed and again.transaction_id == first.transaction_id
    assert ids(seeded_db, first.transaction_id) == (t["旅行"],)
    assert count(seeded_db, TransactionTag) == 1


# --- 原子性 ----------------------------------------------------------------------------


def test_tag_write_failure_rolls_back_everything(seeded_db, t, monkeypatch):
    """繞過驗證、讓不存在的 tag id 在寫入時撞 FK：交易、分錄、稽核、關聯全部回滾。"""
    real = tag_repo.get_many
    monkeypatch.setattr(
        tag_repo,
        "get_many",
        lambda db, ids_: {i: Tag(id=i, name="假", is_active=True) for i in ids_},
    )
    with pytest.raises(IntegrityError):
        create(seeded_db, [99999])
    monkeypatch.setattr(tag_repo, "get_many", real)
    for model in (Transaction, Posting, AuditLog, TransactionTag):
        assert count(seeded_db, model) == 0, model


def test_audit_failure_rolls_back_tag_relations(seeded_db, t, monkeypatch):
    monkeypatch.setattr(audit, "record", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    with pytest.raises(RuntimeError):
        create(seeded_db, [t["旅行"]])
    assert count(seeded_db, Transaction) == 0
    assert count(seeded_db, TransactionTag) == 0


def test_audit_failure_on_update_keeps_old_tags(seeded_db, t, monkeypatch):
    txn_id = create(seeded_db, [t["旅行"], t["聚餐"]])
    monkeypatch.setattr(audit, "record", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    with pytest.raises(RuntimeError):
        transactions.update_transaction(seeded_db, txn_id, form(seeded_db), tag_ids=[t["演唱會"]])
    seeded_db.expire_all()
    assert ids(seeded_db, txn_id) == tuple(sorted([t["旅行"], t["聚餐"]]))
    assert len(logs(seeded_db, txn_id)) == 1


# --- 修改 ------------------------------------------------------------------------------


def test_update_none_preserves_tags(seeded_db, t):
    """既有的修改流程（4A／5B／6）不傳 tag_ids：不能把標籤清掉。"""
    txn_id = create(seeded_db, [t["旅行"]])
    transactions.update_transaction(seeded_db, txn_id, form(seeded_db, description="改備註"))
    assert ids(seeded_db, txn_id) == (t["旅行"],)


def test_update_replace_all_and_audit_before_after(seeded_db, t):
    txn_id = create(seeded_db, [t["旅行"], t["聚餐"]])
    transactions.update_transaction(
        seeded_db, txn_id, form(seeded_db), tag_ids=[t["演唱會"], t["旅行"]]
    )
    assert ids(seeded_db, txn_id) == tuple(sorted([t["旅行"], t["演唱會"]]))
    log = logs(seeded_db, txn_id)[-1]
    assert json.loads(log.before_json)["tag_ids"] == sorted([t["旅行"], t["聚餐"]])
    assert json.loads(log.after_json)["tag_ids"] == sorted([t["旅行"], t["演唱會"]])


def test_update_empty_clears_all(seeded_db, t):
    txn_id = create(seeded_db, [t["旅行"], t["聚餐"]])
    transactions.update_transaction(seeded_db, txn_id, form(seeded_db), tag_ids=())
    assert ids(seeded_db, txn_id) == ()
    assert json.loads(logs(seeded_db, txn_id)[-1].after_json)["tag_ids"] == []


def test_update_inactive_unchanged_kept_newly_added_blocked(seeded_db, t):
    txn_id = create(seeded_db, [t["旅行"]])
    seeded_db.execute(update(Tag).where(Tag.id == t["旅行"]).values(is_active=False))
    seeded_db.commit()
    # 原本就有、這次沒動 → 可以保留
    transactions.update_transaction(
        seeded_db, txn_id, form(seeded_db), tag_ids=[t["旅行"], t["聚餐"]]
    )
    assert ids(seeded_db, txn_id) == tuple(sorted([t["旅行"], t["聚餐"]]))
    # 新加上停用標籤 → V04；不存在 → V05
    with pytest.raises(ValidationFailed) as exc:
        transactions.update_transaction(
            seeded_db, txn_id, form(seeded_db), tag_ids=[t["旅行"], t["舊標籤"]]
        )
    assert codes(exc) == [("V04", "tag_ids")]
    with pytest.raises(ValidationFailed) as exc:
        transactions.update_transaction(seeded_db, txn_id, form(seeded_db), tag_ids=[99999])
    assert codes(exc) == [("V05", "tag_ids")]
    assert ids(seeded_db, txn_id) == tuple(sorted([t["旅行"], t["聚餐"]]))


def test_update_same_tags_is_p1_noop_behavior(seeded_db, t):
    txn_id = create(seeded_db, [t["旅行"]])
    transactions.update_transaction(seeded_db, txn_id, form(seeded_db), tag_ids=[t["旅行"]])
    assert [log.action for log in logs(seeded_db, txn_id)] == ["CREATE", "UPDATE"]
    history = audit_history.history(seeded_db, txn_id)
    assert history[-1].changes == ("內容沒有變更",)


# --- 生命週期 ---------------------------------------------------------------------------


def test_soft_delete_and_restore_preserve_tags(seeded_db, t):
    txn_id = create(seeded_db, [t["旅行"], t["聚餐"]])
    transactions.delete_transaction(seeded_db, txn_id)
    assert ids(seeded_db, txn_id) == tuple(sorted([t["旅行"], t["聚餐"]]))
    assert [x.name for x in tags.tags_for_transaction(seeded_db, txn_id)] == sorted(
        ["旅行", "聚餐"]
    )
    delete_log = logs(seeded_db, txn_id)[-1]
    assert (
        json.loads(delete_log.before_json)["tag_ids"]
        == json.loads(delete_log.after_json)["tag_ids"]
    )
    transactions.restore_transaction(seeded_db, txn_id)
    assert ids(seeded_db, txn_id) == tuple(sorted([t["旅行"], t["聚餐"]]))


def test_restore_does_not_recheck_inactive_tags(seeded_db, t):
    txn_id = create(seeded_db, [t["旅行"]])
    transactions.delete_transaction(seeded_db, txn_id)
    seeded_db.execute(update(Tag).where(Tag.id == t["旅行"]).values(is_active=False))
    seeded_db.commit()
    transactions.restore_transaction(seeded_db, txn_id)
    assert ids(seeded_db, txn_id) == (t["旅行"],)


# --- 稽核摘要 ---------------------------------------------------------------------------


def test_tag_only_update_history_summary(seeded_db, t):
    txn_id = create(seeded_db, [t["旅行"], t["聚餐"]])
    transactions.update_transaction(
        seeded_db, txn_id, form(seeded_db), tag_ids=[t["旅行"], t["演唱會"]]
    )
    names = sorted(["旅行", "聚餐"]), sorted(["旅行", "演唱會"])
    expected = f"標籤：{'、'.join(names[0])} → {'、'.join(names[1])}"
    assert audit_history.history(seeded_db, txn_id)[-1].changes == (expected,)
    # 改名後顯示現在的名稱；查不到顯示 #id
    seeded_db.execute(update(Tag).where(Tag.id == t["演唱會"]).values(name="演唱會2026"))
    seeded_db.commit()
    assert "演唱會2026" in audit_history.history(seeded_db, txn_id)[-1].changes[0]
    transactions.update_transaction(seeded_db, txn_id, form(seeded_db), tag_ids=())
    assert audit_history.history(seeded_db, txn_id)[-1].changes[0].endswith("→ （無）")


def test_old_audit_without_tag_ids_is_not_misreported(seeded_db, t):
    """8A 之前的紀錄沒有 tag_ids：視為未知，不是空集合。"""
    txn_id = create(seeded_db, [t["旅行"]])
    transactions.update_transaction(seeded_db, txn_id, form(seeded_db, description="改"))
    log = logs(seeded_db, txn_id)[-1]
    before, after = json.loads(log.before_json), json.loads(log.after_json)
    for variant in ({"before": before, "after": after}, {"before": before}):
        b, a = dict(before), dict(after)
        b.pop("tag_ids")
        if "after" in variant:
            a.pop("tag_ids")
        log.before_json, log.after_json = json.dumps(b), json.dumps(a)
        seeded_db.commit()
        changes = audit_history.history(seeded_db, txn_id)[-1].changes
        assert changes == ("備註：（無） → 改",), changes
