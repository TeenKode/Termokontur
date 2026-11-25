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

_MIGRATIONS = (_initialize_legacy_schema, _add_sensor_model, _retire_unsupported_sensors, _factory_tickets, _sensor_history, _session_revocations, _max_delivery_failures, _admin_audit)
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
