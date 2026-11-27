"""Versioned schema upgrades for the panel, MAX bot and staged backups."""
from __future__ import annotations

import sqlite3
from pathlib import Path


def _add_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> set[str]:
    present = {column[1] for column in conn.execute(f"PRAGMA table_info({table})")}
    added = set(columns) - present
    for column, definition in columns.items():
        if column in added:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    return added


def _initialize_legacy_schema(conn: sqlite3.Connection, stamp: str) -> None:
    # Execute each bundled statement inside the caller's transaction.
    # sqlite3.executescript() implicitly commits a pending transaction.
    schema = Path(__file__).with_name("schema_v1.sql").read_text(encoding="utf-8")
    statement = ""
    for line in schema.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            conn.execute(statement)
            statement = ""
    if statement.strip():
        raise RuntimeError("Incomplete SQL in schema_v1.sql")

    _add_columns(conn, "max_outbox_ui", {
        "origin_mid": "TEXT",
        "delivery_mode": "TEXT NOT NULL DEFAULT 'menu'",
        "notify": "INTEGER NOT NULL DEFAULT 1",
        "chat_id": "TEXT",
    })
    _add_columns(conn, "temperature_alert_state", {
        "last_reading_id": "INTEGER NOT NULL DEFAULT 0",
    })
    _add_columns(conn, "notification_recipients", {"delivered_at": "TEXT"})
    added = _add_columns(conn, "sensor_devices", {
        "sensor_type": "TEXT NOT NULL DEFAULT 'sht4x'",
        "restore_pending": "INTEGER NOT NULL DEFAULT 0",
        "last_complete_sync": "INTEGER",
    })
    if "last_complete_sync" in added:
        conn.execute("UPDATE sensor_devices SET last_complete_sync=last_seen")

    conn.execute("""
        INSERT INTO room_activity(room_id,changed_at,active)
        SELECT id,?,0 FROM rooms r WHERE active=0 AND NOT EXISTS (
            SELECT 1 FROM room_activity a WHERE a.room_id=r.id
        )
    """, (stamp,))
    if conn.execute("INSERT OR IGNORE INTO app_state(key,value) VALUES('binding_history_since',?)",
                    (stamp,)).rowcount:
        conn.execute("""
            INSERT INTO max_binding_periods(user_id,room_id,starts_at)
            SELECT user_id,room_id,? FROM max_bindings
        """, (stamp,))


def _add_sensor_model(conn: sqlite3.Connection, _: str) -> None:
    _add_columns(conn, "sensor_devices", {"model": "TEXT NOT NULL DEFAULT 'c3-sht40-v1'"})


def _retire_unsupported_sensors(conn: sqlite3.Connection, _: str) -> None:
    # Keep device IDs and their history for reports and archived cards.
    conn.execute("UPDATE sensor_devices SET enabled=0 WHERE model<>'c3-sht40-v1'")


def _factory_tickets(conn: sqlite3.Connection, _: str) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS sensor_factory_tickets (
        ticket_hash TEXT PRIMARY KEY, hardware_id TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL, room_id INTEGER NOT NULL, sensor_type TEXT NOT NULL,
        config TEXT NOT NULL, expires INTEGER NOT NULL, consumed_fp TEXT
    )""")


def _sensor_history(conn: sqlite3.Connection, _: str) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS sensor_room_history (
        device_id TEXT NOT NULL REFERENCES sensor_devices(id),
        starts_at INTEGER NOT NULL, room_id INTEGER NOT NULL REFERENCES rooms(id),
        PRIMARY KEY(device_id, starts_at))""")
    conn.execute("INSERT OR IGNORE INTO sensor_room_history SELECT id,0,room_id FROM sensor_devices")
    conn.execute("""CREATE TABLE IF NOT EXISTS sensor_record_receipts (
        device_id TEXT NOT NULL REFERENCES sensor_devices(id),
        seq INTEGER NOT NULL, fingerprint TEXT NOT NULL,
        PRIMARY KEY(device_id, seq))""")


def _session_revocations(conn: sqlite3.Connection, _: str) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS session_revocations (
        digest TEXT PRIMARY KEY, expires_at INTEGER NOT NULL)""")
    conn.execute("CREATE INDEX IF NOT EXISTS session_revocations_expiry ON session_revocations(expires_at)")

def _max_delivery_failures(conn: sqlite3.Connection, _: str) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS max_delivery_failures (
        event_id TEXT PRIMARY KEY REFERENCES max_outbox(event_id) ON DELETE CASCADE,
        status_code INTEGER NOT NULL, failed_at TEXT NOT NULL)""")

def _admin_audit(conn: sqlite3.Connection, _: str) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS admin_audit (
        id INTEGER PRIMARY KEY, recorded_at TEXT NOT NULL,
        method TEXT NOT NULL, route TEXT NOT NULL, target TEXT NOT NULL,
        status INTEGER NOT NULL)""")


def _sensor_sync_status(conn: sqlite3.Connection, _: str) -> None:
    added = _add_columns(conn, "sensor_devices", {
        "sync_incomplete": "INTEGER NOT NULL DEFAULT 0 CHECK(sync_incomplete IN (0,1))",
    })
    if added:
        conn.execute("""UPDATE sensor_devices SET sync_incomplete=1
            WHERE last_seen IS NOT NULL AND (last_complete_sync IS NULL OR last_seen > last_complete_sync)""")


def _sensor_schedule(conn: sqlite3.Connection, _: str) -> None:
    _add_columns(conn, "sensor_devices", {"next_send_hint": "INTEGER"})
    conn.execute("""CREATE TABLE IF NOT EXISTS sensor_config_history (
        device_id TEXT NOT NULL REFERENCES sensor_devices(id) ON DELETE CASCADE,
        version INTEGER NOT NULL, config TEXT NOT NULL,
        PRIMARY KEY(device_id, version))""")


def _sensor_boot_debug(conn: sqlite3.Connection, _: str) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS sensor_boot_debug (
        device_id TEXT PRIMARY KEY REFERENCES sensor_devices(id) ON DELETE CASCADE,
        command_id TEXT NOT NULL UNIQUE,
        duration_s INTEGER NOT NULL CHECK(duration_s IN (300,900,1800,3600)),
        send_now INTEGER NOT NULL CHECK(send_now IN (0,1)),
        preserve_schedule INTEGER NOT NULL CHECK(preserve_schedule IN (0,1)),
        expires_at INTEGER NOT NULL,
        accepted_at INTEGER
    )""")


def _sensor_setup_recovery(conn: sqlite3.Connection, _: str) -> None:
    _add_columns(conn, "sensor_devices", {
        "pending_sensor_type": "TEXT",
        "config_restore_pending": "INTEGER NOT NULL DEFAULT 0 CHECK(config_restore_pending IN (0,1))",
    })


def _max_bot_scope(conn: sqlite3.Connection, _: str) -> None:
    from .max_connection import credential_scope

    _add_columns(conn, "max_inbox", {"bot_scope": "TEXT NOT NULL DEFAULT ''"})
    conn.execute("CREATE INDEX IF NOT EXISTS max_inbox_bot_scope ON max_inbox(bot_scope)")
    token = conn.execute("SELECT value FROM settings WHERE key='max_token'").fetchone()
    if token and token[0]:
        scope = credential_scope(token[0])
        conn.execute("UPDATE max_inbox SET bot_scope=? WHERE bot_scope=''", (scope,))
        conn.execute("""INSERT OR IGNORE INTO app_state(key,value)
                     SELECT ?,value FROM app_state WHERE key='max_marker' AND value<>''""",
                     ("max_marker:" + scope,))


_MIGRATIONS = (_initialize_legacy_schema, _add_sensor_model, _retire_unsupported_sensors,
               _factory_tickets, _sensor_history, _session_revocations, _max_delivery_failures,
               _admin_audit, _sensor_sync_status, _sensor_schedule, _sensor_boot_debug,
               _sensor_setup_recovery, _max_bot_scope)
SCHEMA_VERSION = len(_MIGRATIONS)


def read_version(conn: sqlite3.Connection) -> int:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if not 0 <= version <= SCHEMA_VERSION:
        raise ValueError(f"Неподдерживаемая версия схемы Термоконтура: {version}")
    return version


def apply(conn: sqlite3.Connection, stamp: str) -> None:
    """Upgrade under one writer lock. The caller commits or rolls back."""
    if read_version(conn) == SCHEMA_VERSION:
        return
    conn.execute("BEGIN IMMEDIATE")
    current = read_version(conn)
    for version, migration in enumerate(_MIGRATIONS[current:], start=current + 1):
        migration(conn, stamp)
        conn.execute(f"PRAGMA user_version={version}")
