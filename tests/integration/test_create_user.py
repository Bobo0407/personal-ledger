"""建立唯一帳號：服務函式與 scripts/create_user.py。"""

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app import security
from app.db import make_sessionmaker
from app.models import User
from app.services.auth import UserAlreadyExists, create_initial_user

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "create_user.py"


def load_script():
    spec = importlib.util.spec_from_file_location("create_user_script", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def db(migrated_engine):
    s = make_sessionmaker(migrated_engine)()
    yield s
    s.close()


def users(db) -> list[User]:
    db.expire_all()
    return list(db.scalars(select(User)))


def count(db) -> int:
    return db.scalar(select(func.count()).select_from(User))


# --- 服務函式 ---


def test_create_initial_user_hashes_password(db):
    user = create_initial_user(db, "  bo  ", "password-0123")
    assert user.username == "bo"  # 前後空白去除
    assert user.password_hash.startswith("$argon2id$")
    assert security.verify_password(user.password_hash, "password-0123")


def test_only_one_user_allowed(db):
    create_initial_user(db, "bo", "password-0123")
    with pytest.raises(UserAlreadyExists):
        create_initial_user(db, "another", "password-0456")
    assert count(db) == 1


@pytest.mark.parametrize(("username", "password"), [("", "pw"), ("   ", "pw"), ("bo", "")])
def test_blank_username_or_password_rejected(db, username, password):
    with pytest.raises(ValueError):
        create_initial_user(db, username, password)
    assert count(db) == 0


# --- 腳本 ---


def run_script(db_url, answers, passwords, environ=None, getpass_fn=None):
    messages: list[str] = []
    answers, passwords = iter(answers), iter(passwords)
    code = load_script().main(
        database_url=db_url,
        input_fn=lambda _prompt: next(answers),
        getpass_fn=getpass_fn or (lambda _prompt: next(passwords)),
        out=messages.append,
        # 預設給空的環境，避免開發者本機的環境變數影響測試
        environ={} if environ is None else environ,
    )
    return code, messages


def test_script_creates_user(db, db_url):
    code, messages = run_script(db_url, ["bo"], ["password-0123", "password-0123"])
    assert code == 0
    assert messages == ["已建立帳號：bo"]
    assert [u.username for u in users(db)] == ["bo"]


def test_script_rejects_mismatched_passwords(db, db_url):
    code, messages = run_script(db_url, ["bo"], ["password-0123", "password-0999"])
    assert code == 1
    assert "不一致" in messages[0]
    assert count(db) == 0


def test_script_refuses_when_user_exists(db, db_url):
    create_initial_user(db, "bo", "password-0123")
    # 不應該再詢問帳號密碼：iter([]) 一被呼叫就會 StopIteration
    code, messages = run_script(db_url, [], [])
    assert code == 1
    assert "只允許一個帳號" in messages[0]


def test_script_requires_migrations(tmp_path):
    empty_db = f"sqlite:///{(tmp_path / 'empty.db').as_posix()}"
    code, messages = run_script(empty_db, [], [])
    assert code == 1
    assert "alembic upgrade head" in messages[0]


# --- 環境變數 fallback ---

ENV = "LEDGER_INITIAL_PASSWORD"


def getpass_must_not_be_called(_prompt):
    raise AssertionError("有環境變數時不應該呼叫 getpass")


def test_env_password_fallback_creates_user(db, db_url):
    environ = {ENV: "env-secret-pw"}
    code, messages = run_script(
        db_url, ["bo"], [], environ=environ, getpass_fn=getpass_must_not_be_called
    )
    assert code == 0
    assert ENV not in environ  # 讀取後立即從程序環境移除
    assert any("Remove-Item Env:LEDGER_INITIAL_PASSWORD" in m for m in messages)
    assert all("env-secret-pw" not in m for m in messages)  # 不回顯密碼

    [user] = users(db)
    assert user.password_hash.startswith("$argon2id$")  # 資料庫仍只存雜湊
    assert security.verify_password(user.password_hash, "env-secret-pw")


def test_empty_env_password_falls_back_to_getpass(db, db_url):
    code, _ = run_script(db_url, ["bo"], ["password-0123", "password-0123"], environ={ENV: ""})
    assert code == 0
    assert security.verify_password(users(db)[0].password_hash, "password-0123")


@pytest.mark.parametrize("error", [EOFError, KeyboardInterrupt])
def test_getpass_failure_points_to_env_fallback(db, db_url, error):
    def broken_getpass(_prompt):
        raise error

    code, messages = run_script(db_url, ["bo"], [], getpass_fn=broken_getpass)
    assert code == 1
    assert ENV in messages[-1]
    assert count(db) == 0


def test_env_password_is_not_in_env_example():
    env_example = SCRIPT.parents[1] / ".env.example"
    assert ENV not in env_example.read_text(encoding="utf-8")


# --- 密碼最低長度（至少 12 個字元，不要求組合）---


@pytest.mark.parametrize(
    ("password", "ok"),
    [
        ("a" * 11, False),
        ("a" * 12, True),  # 剛好 12 個字元、全小寫也接受
        ("密碼密碼密碼密碼密碼密碼", True),  # 以字元數計算，不是位元組
    ],
)
def test_service_enforces_min_password_length(db, password, ok):
    if ok:
        create_initial_user(db, "bo", password)
        assert count(db) == 1
    else:
        with pytest.raises(ValueError, match="12"):
            create_initial_user(db, "bo", password)
        assert count(db) == 0


def test_script_rejects_short_password_before_confirmation(db, db_url):
    prompts: list[str] = []

    def fake_getpass(prompt):
        prompts.append(prompt)
        return "short-pw"

    code, messages = run_script(db_url, ["bo"], [], getpass_fn=fake_getpass)
    assert code == 1
    assert len(prompts) == 1  # 太短就不再要求確認
    assert "12" in messages[-1]
    assert count(db) == 0


def test_script_rejects_short_env_password(db, db_url):
    environ = {ENV: "short-pw"}
    code, messages = run_script(
        db_url, ["bo"], [], environ=environ, getpass_fn=getpass_must_not_be_called
    )
    assert code == 1
    assert ENV not in environ
    assert "12" in messages[-1]
    assert count(db) == 0
