import re

import pytest
from sqlalchemy.exc import IntegrityError

from app import clock
from app.db import make_sessionmaker
from app.models import User, UserSession

ISO_TAIPEI = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+08:00$")


@pytest.fixture
def session(migrated_engine):
    s = make_sessionmaker(migrated_engine)()
    yield s
    s.rollback()
    s.close()


def make_user(username: str = "bo") -> User:
    return User(username=username, password_hash="not-a-real-hash")


def make_session(user: User, token_hash: str = "h1") -> UserSession:
    return UserSession(user=user, token_hash=token_hash, expires_at="2026-11-06T12:00:00+08:00")


def test_user_defaults(session):
    user = make_user()
    session.add(user)
    session.commit()

    assert user.id is not None
    assert user.failed_login_count == 0
    assert user.locked_until is None
    assert ISO_TAIPEI.match(user.created_at)
    assert ISO_TAIPEI.match(user.updated_at)


def test_updated_at_changes_on_update(session, monkeypatch):
    monkeypatch.setattr(clock, "now_iso", lambda: "2026-10-01T09:00:00+08:00")
    user = make_user()
    session.add(user)
    session.commit()

    monkeypatch.setattr(clock, "now_iso", lambda: "2026-10-02T10:00:00+08:00")
    user.failed_login_count = 1
    session.commit()

    assert user.created_at == "2026-10-01T09:00:00+08:00"
    assert user.updated_at == "2026-10-02T10:00:00+08:00"


def test_username_is_unique(session):
    session.add(make_user("bo"))
    session.commit()
    session.add(make_user("bo"))
    with pytest.raises(IntegrityError):
        session.commit()


def test_session_belongs_to_user(session):
    user = make_user()
    us = make_session(user)
    session.add(us)
    session.commit()

    assert us.user_id == user.id
    assert user.sessions == [us]
    assert us.revoked_at is None
    assert us.last_seen_at is None


def test_token_hash_is_unique(session):
    user = make_user()
    session.add_all([make_session(user, "same"), make_session(user, "same")])
    with pytest.raises(IntegrityError):
        session.commit()


def test_session_requires_existing_user(session):
    # 外鍵檢查：指向不存在的使用者必須被擋下
    session.add(
        UserSession(user_id=999, token_hash="orphan", expires_at="2026-11-06T12:00:00+08:00")
    )
    with pytest.raises(IntegrityError):
        session.commit()


def test_repr_hides_secrets(session):
    user = make_user()
    us = make_session(user, token_hash="super-secret-hash")
    session.add(us)
    session.commit()

    assert "not-a-real-hash" not in repr(user)
    assert "super-secret-hash" not in repr(us)
