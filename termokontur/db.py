"""SQLite persistence shared by web app and MAX bot."""
from __future__ import annotations

import hashlib
import math
import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def db_path() -> Path:
    return Path(os.environ.get("TERMOKONTUR_DB", "./data/termokontur.sqlite3"))

@contextmanager
def connect():
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=15000")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def initialize() -> None:
    with connect() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS rooms (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            building TEXT NOT NULL,
            floor TEXT NOT NULL DEFAULT '',
            name TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'Кабинет',
            min_temp REAL NOT NULL DEFAULT 18,
            max_temp REAL NOT NULL DEFAULT 24,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            UNIQUE(building, name),
            CHECK(min_temp < max_temp)
        );
        CREATE TABLE IF NOT EXISTS readings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            room_id INTEGER NOT NULL REFERENCES rooms(id),
            value REAL NOT NULL,
            source TEXT NOT NULL CHECK(source IN ('manual', 'max', 'ha')),
            reporter TEXT NOT NULL DEFAULT '',
            observed_at TEXT NOT NULL,
            external_id TEXT UNIQUE
        );
        CREATE INDEX IF NOT EXISTS readings_room_time ON readings(room_id, observed_at DESC);
        CREATE TABLE IF NOT EXISTS pairing_codes (
            digest TEXT PRIMARY KEY,
            room_id INTEGER NOT NULL REFERENCES rooms(id),
            expires_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS max_bindings (
            user_id TEXT NOT NULL,
            room_id INTEGER NOT NULL REFERENCES rooms(id),
            PRIMARY KEY(user_id, room_id)
        );
        CREATE TABLE IF NOT EXISTS app_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS admin_account (
            id INTEGER PRIMARY KEY CHECK(id=1),
            salt TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            session_epoch INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS max_inbox (
            event_id TEXT PRIMARY KEY,
            state TEXT NOT NULL CHECK(state IN ('processing', 'complete')),
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS max_outbox (
            event_id TEXT PRIMARY KEY REFERENCES max_inbox(event_id),
            user_id TEXT NOT NULL,
            text TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            next_retry_at TEXT NOT NULL,
            delivered_at TEXT
        );
        CREATE TABLE IF NOT EXISTS max_bind_events (
            event_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            room_id INTEGER NOT NULL REFERENCES rooms(id),
            recorded_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS reminder_deliveries (
            local_day TEXT NOT NULL,
            user_id TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('pending', 'sent', 'failed')),
            attempts INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(local_day, user_id)
        );
        """)

def room_list(active_only: bool = True) -> list[dict]:
    with connect() as conn:
        sql = "SELECT * FROM rooms" + (" WHERE active=1" if active_only else "") + " ORDER BY building, name"
        return [dict(x) for x in conn.execute(sql)]

def room_get(room_id: int) -> dict | None:
    with connect() as conn:
        found = conn.execute("SELECT * FROM rooms WHERE id=?", (room_id,)).fetchone()
        return dict(found) if found else None

def create_room(building: str, floor: str, name: str, kind: str, minimum: float, maximum: float) -> dict:
    with connect() as conn:
        cur = conn.execute(
            """INSERT INTO rooms(building, floor, name, kind, min_temp, max_temp, created_at)
               VALUES(?,?,?,?,?,?,?)""",
            (building, floor, name, kind, minimum, maximum, utc_now()),
        )
        room_id = cur.lastrowid
    return room_get(room_id)

def create_rooms_bulk(building: str, floor: str, kind: str, minimum: float,
                      maximum: float, names: list[str]) -> dict:
    """Insert a validated list in one transaction; duplicates are reported."""
    if not names or len(names) > 250:
        raise ValueError("Укажите от 1 до 250 помещений")
    names = [name.strip() for name in names]
    if any(not name or len(name) > 100 for name in names):
        raise ValueError("Некорректное название помещения")
    if len(set(names)) != len(names):
        raise ValueError("Повторяющиеся помещения в списке")
    if not building.strip() or len(building) > 100 or not kind.strip() or minimum >= maximum:
        raise ValueError("Некорректные параметры помещений")
    count = 0
    skipped = []
    with connect() as conn:
        for name in names:
            cur = conn.execute(
                """INSERT OR IGNORE INTO rooms(building, floor, name, kind,
                   min_temp, max_temp, created_at) VALUES(?,?,?,?,?,?,?)""",
                (building.strip(), floor.strip(), name, kind.strip(), minimum, maximum, utc_now()),
            )
            if cur.rowcount:
                count += 1
            else:
                skipped.append(name)
    return {"created": count, "skipped": skipped, "total": len(names)}


def update_room(room_id: int, patch: dict) -> dict | None:
    fields = {"building", "floor", "name", "kind", "min_temp", "max_temp", "active"}
    patch = {k: v for k, v in patch.items() if k in fields}
    if not patch:
        return room_get(room_id)
    with connect() as conn:
        sql = "UPDATE rooms SET " + ", ".join(f"{key}=?" for key in patch) + " WHERE id=?"
        conn.execute(sql, [*patch.values(), room_id])
    return room_get(room_id)

def add_reading(room_id: int, value: float, source: str, reporter: str = "",
                external_id: str | None = None) -> tuple[dict, bool]:
    if not math.isfinite(value) or not -40 <= value <= 60:
        raise ValueError("Температура должна быть в пределах −40…60 °C")
    if source not in ("manual", "max", "ha"):
        raise ValueError("Неизвестный источник")
    room = room_get(room_id)
    if not room or not room["active"]:
        raise ValueError("Кабинет не найден или отключён")
    with connect() as conn:
        try:
            cur = conn.execute(
                """INSERT INTO readings(room_id, value, source, reporter, observed_at, external_id)
                   VALUES(?,?,?,?,?,?)""",
                (room_id, value, source, reporter[:128], utc_now(), external_id),
            )
            reading_id, inserted = cur.lastrowid, True
        except sqlite3.IntegrityError:
            if not external_id:
                raise
            old = conn.execute("SELECT * FROM readings WHERE external_id=?", (external_id,)).fetchone()
            if old is None:
                raise
            if old["room_id"] != room_id:
                raise ValueError("Конфликт идентификатора измерения")
            reading_id, inserted = old["id"], False
        found = conn.execute("SELECT * FROM readings WHERE id=?", (reading_id,)).fetchone()
        return dict(found), inserted

def recent_readings(room_id: int | None = None, limit: int = 200,
                    since: str | None = None, until: str | None = None) -> list[dict]:
    parts, args = [], []
    if room_id is not None:
        parts.append("r.room_id=?")
        args.append(room_id)
    if since:
        parts.append("r.observed_at>=?")
        args.append(since)
    if until:
        parts.append("r.observed_at<=?")
        args.append(until)
    where = (" WHERE " + " AND ".join(parts)) if parts else ""
    sql = ("""SELECT r.*, p.building, p.name AS room_name, p.floor,
                     p.min_temp, p.max_temp
              FROM readings r JOIN rooms p ON p.id=r.room_id""" + where +
           " ORDER BY r.observed_at DESC, r.id DESC LIMIT ?")
    with connect() as conn:
        return [dict(x) for x in conn.execute(sql, [*args, limit])]

def dashboard(stale_hours: int = 26) -> dict:
    now = datetime.now(timezone.utc)
    result = []
    counts = {"normal": 0, "below": 0, "above": 0, "missing": 0}
    with connect() as conn:
        rows = conn.execute("""
          SELECT p.*, r.value AS last_temp, r.source AS last_source,
                 r.observed_at AS last_at
          FROM rooms p
          LEFT JOIN readings r ON r.id = (
              SELECT id FROM readings WHERE room_id=p.id
              ORDER BY observed_at DESC, id DESC LIMIT 1
          )
          WHERE p.active=1 ORDER BY p.building, p.name
        """)
        for row in rows:
            item = dict(row)
            latest = item["last_at"]
            if not latest or now - datetime.fromisoformat(latest) > timedelta(hours=stale_hours):
                status = "missing"
            elif item["last_temp"] < item["min_temp"]:
                status = "below"
            elif item["last_temp"] > item["max_temp"]:
                status = "above"
            else:
                status = "normal"
            item["status"] = status
            counts[status] += 1
            result.append(item)
    return {"counts": counts, "rooms": result, "generated_at": utc_now()}

def issue_codes_bulk(room_ids: list[int], validity_minutes: int = 1440) -> list[dict]:
    """Issue new one-time MAX codes atomically for 1..250 active rooms.

    Existing unused codes for the selected rooms are revoked. Plaintext codes
    are returned exactly once; only their SHA-256 digests are stored in SQLite.
    """
    if not room_ids or len(room_ids) > 250 or len(set(room_ids)) != len(room_ids):
        raise ValueError("Выберите от 1 до 250 уникальных кабинетов")
    if any(type(room_id) is not int or room_id <= 0 for room_id in room_ids):
        raise ValueError("Некорректный ID кабинета")
    if validity_minutes not in (30, 120, 1440, 10080):
        raise ValueError("Недопустимый срок действия кода")
    placeholders = ",".join("?" for _ in room_ids)
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=validity_minutes)).isoformat(timespec="seconds")
    issued = []
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        found = [
            dict(row) for row in conn.execute(
                f"SELECT id, building, floor, name FROM rooms "
                f"WHERE active=1 AND id IN ({placeholders}) ORDER BY building, name, id",
                room_ids,
            )
        ]
        if len(found) != len(room_ids):
            raise ValueError("Часть кабинетов отсутствует или отключена")
        conn.execute("DELETE FROM pairing_codes WHERE expires_at<=?", (utc_now(),))
        conn.execute(f"DELETE FROM pairing_codes WHERE room_id IN ({placeholders})", room_ids)
        for room in found:
            code = secrets.token_hex(8).upper()
            digest = hashlib.sha256(code.encode("ascii")).hexdigest()
            conn.execute(
                "INSERT INTO pairing_codes(digest, room_id, expires_at) VALUES (?,?,?)",
                (digest, room["id"], expires_at),
            )
            issued.append({**room, "code": code, "expires_at": expires_at})
    return issued


def issue_code(room_id: int, validity_minutes: int = 30) -> str:
    """Issue a single code, revoking old unused ones for this room."""
    return issue_codes_bulk([room_id], validity_minutes)[0]["code"]

def bind_code(user_id: str, code: str, event_id: str | None = None) -> dict | None:
    digest = hashlib.sha256(code.strip().upper().encode()).hexdigest()
    with connect() as conn:
        # A write transaction ensures a code can be consumed by exactly one user.
        # BEGIN IMMEDIATE serializes simultaneous bind attempts before lookup.
        conn.execute("BEGIN IMMEDIATE")
        if event_id:
            previous = conn.execute(
                """SELECT r.id AS room_id, r.building, r.name FROM max_bind_events e
                   JOIN rooms r ON r.id=e.room_id
                   WHERE e.event_id=? AND e.user_id=?""",
                (event_id, str(user_id)),
            ).fetchone()
            if previous:
                return dict(previous)
        found = conn.execute(
            """SELECT p.room_id, r.building, r.name FROM pairing_codes p
               JOIN rooms r ON r.id=p.room_id
               WHERE digest=? AND expires_at>? AND r.active=1""",
            (digest, utc_now()),
        ).fetchone()
        if not found:
            return None
        deleted = conn.execute("DELETE FROM pairing_codes WHERE digest=?", (digest,))
        if deleted.rowcount != 1:
            return None
        conn.execute("INSERT OR IGNORE INTO max_bindings(user_id, room_id) VALUES (?,?)",
                     (str(user_id), found["room_id"]))
        if event_id:
            conn.execute(
                "INSERT OR IGNORE INTO max_bind_events(event_id,user_id,room_id,recorded_at) VALUES(?,?,?,?)",
                (event_id, str(user_id), found["room_id"], utc_now()),
            )
        return dict(found)

def bound_rooms(user_id: str) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            """SELECT r.* FROM rooms r JOIN max_bindings b ON b.room_id=r.id
               WHERE b.user_id=? AND r.active=1 ORDER BY r.building, r.name""",
            (str(user_id),),
        )
        return [dict(x) for x in rows]

def unbind(user_id: str, room_id: int) -> bool:
    with connect() as conn:
        cur = conn.execute("DELETE FROM max_bindings WHERE user_id=? AND room_id=?",
                           (str(user_id), room_id))
        return bool(cur.rowcount)

def get_state(key: str) -> str | None:
    with connect() as conn:
        row = conn.execute("SELECT value FROM app_state WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

def set_state(key: str, value: str) -> None:
    with connect() as conn:
        conn.execute(
            """INSERT INTO app_state(key, value) VALUES (?,?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
            (key, value),
        )
