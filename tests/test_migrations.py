"""SQLite startup and transactional schema upgrades."""
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from termokontur import auth, db, max_connection, max_delivery, migrations, settings


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


def test_upgrade_from_v4_preserves_existing_data(tmp_path, monkeypatch):
    from termokontur import maintenance, sensors
    path = tmp_path / "legacy.sqlite3"
    monkeypatch.setenv("TERMOKONTUR_DB", str(path))
    monkeypatch.setenv("TERMOKONTUR_ADMIN_PASSWORD", "legacy-test-password")
    monkeypatch.setenv("TERMOKONTUR_SECRET", "legacy-test-secret-which-is-long-enough")
    monkeypatch.setenv("MAX_BOT_TOKEN", "legacy-test-max-token-for-the-existing-bot")
    # 1.2.4 used the first four migrations. Construct that schema before
    # creating any records, then open it using the complete current chain.
    with monkeypatch.context() as old:
        old.setattr(migrations, "_MIGRATIONS", migrations._MIGRATIONS[:4])
        old.setattr(migrations, "SCHEMA_VERSION", 4)
        db.initialize()
        settings.bootstrap()
        auth.bootstrap()
        room = db.create_room("Школа", "1", "101", "Кабинет", 18, 24)
        db.add_reading(room["id"], 21.5, "manual")
        assert db.bind_code("123456", db.issue_code(room["id"]))
        settings.update({"organization_name": "Старая школа"})
        db.set_state("max_marker", "legacy-marker")
        with db.connect() as conn:
            conn.execute("INSERT INTO max_inbox(event_id,state,updated_at) VALUES('legacy','complete',?)",
                         (db.utc_now(),))
            conn.execute("""INSERT INTO max_outbox(event_id,user_id,text,next_retry_at)
                         VALUES('legacy','123456','Ответ',?)""", (db.utc_now(),))
            conn.execute("""INSERT INTO sensor_devices(id,name,room_id,enrollment_until,
                token_hash,config,created_at,ack_seq,last_seen,last_complete_sync) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                ("legacy-device", "Старый датчик", room["id"], 0, "old-token-hash",
                 sensors.Config().model_dump_json(), db.utc_now(), 2, 1700000001, 1700000000))
            conn.execute("""INSERT INTO sensor_records VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("legacy-device",1,room["id"],1700000000,1700000001,
                 "boot",1,"rtc",21.5,45.0,3900,1800,"","old-fingerprint"))
            assert migrations.read_version(conn) == 4
    db.initialize()
    db.initialize()
    assert auth.check_password("legacy-test-password")
    assert settings.get("organization_name") == "Старая школа"
    with db.connect() as conn:
        assert migrations.read_version(conn) == migrations.SCHEMA_VERSION == 13
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("SELECT value FROM readings").fetchone()[0] == 21.5
        assert conn.execute("SELECT user_id FROM max_bindings").fetchone()[0] == "123456"
        assert conn.execute("SELECT ack_seq FROM sensor_devices").fetchone()[0] == 2
        assert conn.execute("SELECT sync_incomplete FROM sensor_devices").fetchone()[0] == 1
        assert tuple(conn.execute("SELECT pending_sensor_type,config_restore_pending FROM sensor_devices").fetchone()) == (None, 0)
        assert conn.execute("SELECT fingerprint FROM sensor_records").fetchone()[0] == "old-fingerprint"
        assert tuple(conn.execute("SELECT device_id,room_id FROM sensor_room_history").fetchone()) == ("legacy-device",room["id"])
        for name in ("sensor_record_receipts", "session_revocations", "max_delivery_failures", "admin_audit", "sensor_config_history", "sensor_boot_debug"):
            assert conn.execute("SELECT name FROM sqlite_master WHERE name=?", (name,)).fetchone()
    maintenance.inspect(path)
    bot = max_connection.identify(max_connection.current(), {"user_id": 123, "is_bot": True})
    assert max_connection.marker(bot) == "legacy-marker"
    assert [item["event_id"] for item in max_delivery.pending(expected=bot)] == ["legacy"]
