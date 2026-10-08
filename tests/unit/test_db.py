import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.db import make_engine, make_sessionmaker


@pytest.fixture
def engine(tmp_path):
    eng = make_engine(f"sqlite:///{tmp_path / 'nested' / 'test.db'}")
    yield eng
    eng.dispose()


def test_creates_parent_directory(tmp_path):
    db_file = tmp_path / "a" / "b" / "ledger.db"
    eng = make_engine(f"sqlite:///{db_file}")
    with eng.connect() as conn:
        conn.execute(text("SELECT 1"))
    eng.dispose()
    assert db_file.exists()


def test_foreign_keys_on_for_every_connection(engine):
    # 同時開兩條連線，兩條都必須開啟外鍵檢查
    with engine.connect() as c1, engine.connect() as c2:
        assert c1.execute(text("PRAGMA foreign_keys")).scalar() == 1
        assert c2.execute(text("PRAGMA foreign_keys")).scalar() == 1


def test_foreign_key_violation_is_rejected(engine):
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE parent (id INTEGER PRIMARY KEY)"))
        conn.execute(
            text(
                "CREATE TABLE child (id INTEGER PRIMARY KEY, "
                "parent_id INTEGER NOT NULL REFERENCES parent(id))"
            )
        )
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO child (parent_id) VALUES (999)"))


def test_sessionmaker_uses_engine(engine):
    session = make_sessionmaker(engine)()
    try:
        assert session.execute(text("PRAGMA foreign_keys")).scalar() == 1
    finally:
        session.close()


def test_rejects_non_sqlite_url():
    with pytest.raises(ValueError, match="SQLite"):
        make_engine("postgresql://user:pw@localhost/db")
