"""SQLite startup and transactional schema upgrades."""
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from termokontur import auth, db, migrations, settings


def test_concurrent_first_start(tmp_path, monkeypatch):
    path = tmp_path / "new.sqlite3"
    monkeypatch.setenv("TERMOKONTUR_DB", str(path))
    password = "initial-test-password"
    monkeypatch.setenv("TERMOKONTUR_ADMIN_PASSWORD", password)
    monkeypatch.setenv("TERMOKONTUR_ORGANIZATION", "Тестовая школа")
    barrier = Barrier(4)

    def start():
        barrier.wait(timeout=10)
        db.initialize()
        barrier.wait(timeout=10)
        settings.bootstrap()
        barrier.wait(timeout=10)
        auth.bootstrap()

    with ThreadPoolExecutor(max_workers=4) as pool:
        starts = [pool.submit(start) for _ in range(4)]
        for startup in starts:
            startup.result()

    with db.connect() as conn:
        assert migrations.read_version(conn) == migrations.SCHEMA_VERSION
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT COUNT(*) FROM admin_account").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM app_state WHERE key='binding_history_since'").fetchone()[0] == 1
    assert settings.get("organization_name") == "Тестовая школа"
    assert auth.check_password(password)


def test_schema_rolls_back_if_version_cannot_be_written(tmp_path):
    path = tmp_path / "interrupted.sqlite3"

    def deny_version_write(action, pragma, value, *_):
        if action == sqlite3.SQLITE_PRAGMA and pragma == "user_version" and value is not None:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
        with db.connect(path) as conn:
            conn.set_authorizer(deny_version_write)
            migrations.apply(conn, db.utc_now())

    with db.connect(path) as conn:
        assert migrations.read_version(conn) == 0
        assert conn.execute("SELECT name FROM sqlite_master").fetchall() == []
    db.initialize(path)
    with db.connect(path) as conn:
        assert migrations.read_version(conn) == migrations.SCHEMA_VERSION
        assert conn.execute("SELECT COUNT(*) FROM rooms").fetchone()[0] == 0
