"""SQLite persistence shared by web app and MAX bot."""
from __future__ import annotations

import hashlib
import math
import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
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
        CREATE TABLE IF NOT EXISTS max_users (
            user_id TEXT PRIMARY KEY,
            first_name TEXT NOT NULL DEFAULT '',
            last_name TEXT NOT NULL DEFAULT '',
            username TEXT NOT NULL DEFAULT '',
            seen_at TEXT NOT NULL
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
        CREATE TABLE IF NOT EXISTS temperature_alert_state (
            room_id INTEGER NOT NULL REFERENCES rooms(id),
            user_id TEXT NOT NULL,
            last_status TEXT NOT NULL DEFAULT 'normal',
            last_at TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(room_id, user_id)
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
        CREATE TABLE IF NOT EXISTS max_user_sessions (
            user_id TEXT PRIMARY KEY,
            mode TEXT NOT NULL DEFAULT '',
            room_id INTEGER,
            page INTEGER NOT NULL DEFAULT 0,
            local_day TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS max_reading_corrections (
            event_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            reading_id INTEGER NOT NULL REFERENCES readings(id) ON DELETE CASCADE,
            old_value REAL NOT NULL,
            new_value REAL NOT NULL,
            old_observed_at TEXT NOT NULL,
            corrected_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS max_outbox_ui (
            event_id TEXT PRIMARY KEY REFERENCES max_outbox(event_id) ON DELETE CASCADE,
            buttons_json TEXT,
            callback_id TEXT
        );
        CREATE TABLE IF NOT EXISTS notification_batches (
            id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            text TEXT NOT NULL,
            recipients INTEGER NOT NULL,
            silent INTEGER NOT NULL DEFAULT 0,
            payload_hash TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS notification_recipients (
            batch_id TEXT NOT NULL REFERENCES notification_batches(id) ON DELETE CASCADE,
            user_id TEXT NOT NULL,
            event_id TEXT NOT NULL UNIQUE,
            delivered_at TEXT,
            PRIMARY KEY(batch_id, user_id)
        );
        CREATE TABLE IF NOT EXISTS max_menu_messages (
            user_id TEXT PRIMARY KEY,
            message_id TEXT NOT NULL
        );
        """)
        # Add optional callback source ID without changing existing installations.
        ui_columns = {
            col["name"] for col in conn.execute("PRAGMA table_info(max_outbox_ui)")
        }
        if "origin_mid" not in ui_columns:
            conn.execute("ALTER TABLE max_outbox_ui ADD COLUMN origin_mid TEXT")
        if "delivery_mode" not in ui_columns:
            conn.execute("ALTER TABLE max_outbox_ui ADD COLUMN delivery_mode TEXT NOT NULL DEFAULT 'menu'")
        if "notify" not in ui_columns:
            conn.execute("ALTER TABLE max_outbox_ui ADD COLUMN notify INTEGER NOT NULL DEFAULT 1")
        alert_columns = {
            col["name"] for col in conn.execute("PRAGMA table_info(temperature_alert_state)")
        }
        if "last_reading_id" not in alert_columns:
            conn.execute("ALTER TABLE temperature_alert_state ADD COLUMN last_reading_id INTEGER NOT NULL DEFAULT 0")
        if "delivered_at" not in {
            col["name"] for col in conn.execute("PRAGMA table_info(notification_recipients)")
        }:
            conn.execute("ALTER TABLE notification_recipients ADD COLUMN delivered_at TEXT")

def room_list(active_only: bool = True) -> list[dict]:
    """Admin room catalog with MAX assignments, fetched without N+1 queries."""
    with connect() as conn:
        sql = "SELECT * FROM rooms" + (" WHERE active=1" if active_only else "") + " ORDER BY building, name"
        rooms = [dict(row) for row in conn.execute(sql)]
        users_by_room: dict[int, list[dict]] = {}
        for row in conn.execute("""
            SELECT b.room_id, b.user_id,
                   COALESCE(u.first_name, '') AS first_name,
                   COALESCE(u.last_name, '') AS last_name,
                   COALESCE(u.username, '') AS username
            FROM max_bindings b LEFT JOIN max_users u ON u.user_id=b.user_id
            ORDER BY CASE WHEN COALESCE(u.first_name, '')='' AND COALESCE(u.last_name, '')=''
                          THEN 1 ELSE 0 END,
                     u.last_name COLLATE NOCASE, u.first_name COLLATE NOCASE, b.user_id
        """):
            user = dict(row)
            users_by_room.setdefault(user.pop("room_id"), []).append(user)
        for room in rooms:
            room["max_users"] = users_by_room.get(room["id"], [])
        return rooms

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

def dashboard_history(days: int = 14, timezone_name: str = "UTC",
                      building: str = "", now: datetime | None = None) -> dict:
    """Daily school average with equal weight for each reporting room.

    First average multiple readings within one room and local calendar day,
    then average those room-day values. Missing rooms are not treated as 0 °C.
    Dates use the configured IANA timezone, including DST transitions.
    """
    if days not in (7, 14, 30, 90):
        raise ValueError("Недопустимый период")
    zone = ZoneInfo(timezone_name)
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    today = instant.astimezone(zone).date()
    start_day = today - timedelta(days=days - 1)
    start_utc = datetime.combine(start_day, time.min, tzinfo=zone).astimezone(timezone.utc)
    end_utc = datetime.combine(today + timedelta(days=1), time.min, tzinfo=zone).astimezone(timezone.utc)
    calendar = [(start_day + timedelta(days=i)).isoformat() for i in range(days)]

    where_building = " AND p.building=?" if building else ""
    args = (building,) if building else ()
    totals: dict[tuple[int, str], list[float | int]] = {}
    with connect() as conn:
        rooms = [dict(row) for row in conn.execute(
            "SELECT id, name, building, min_temp, max_temp FROM rooms p "
            "WHERE p.active=1" + where_building + " ORDER BY p.building,p.name,p.id",
            args,
        )]
        # The (room_id, observed_at) index makes the bounded scans efficient.
        measurements = conn.execute(
            "SELECT r.room_id, r.value, r.observed_at FROM rooms p "
            "JOIN readings r ON r.room_id=p.id AND r.observed_at>=? AND r.observed_at<? "
            "WHERE p.active=1" + where_building + " ORDER BY r.room_id,r.observed_at",
            (start_utc.isoformat(timespec="seconds"), end_utc.isoformat(timespec="seconds"), *args),
        )
        for row in measurements:
            observed = datetime.fromisoformat(row["observed_at"])
            if observed.tzinfo is None:
                observed = observed.replace(tzinfo=timezone.utc)
            day = observed.astimezone(zone).date().isoformat()
            if not calendar[0] <= day <= calendar[-1]:
                continue
            pair = totals.setdefault((row["room_id"], day), [0.0, 0])
            pair[0] += row["value"]
            pair[1] += 1

    per_day: dict[str, list[tuple[float, float, float, int]]] = {
        day: [] for day in calendar
    }
    by_building = {name: {day: [] for day in calendar} for name in sorted({r['building'] for r in rooms})}
    building_counts = {name: sum(r['building'] == name for r in rooms) for name in by_building}
    by_room = []
    for room in rooms:
        samples = {}
        averages = []
        for day in calendar:
            pair = totals.get((room["id"], day))
            if pair is None:
                continue
            mean = pair[0] / pair[1]
            averages.append(mean)
            samples[day] = {"average": round(mean, 2), "count": pair[1]}
            per_day[day].append((mean, room["min_temp"], room["max_temp"], pair[1]))
            by_building[room["building"]][day].append((mean, room["min_temp"], room["max_temp"], pair[1]))
        by_room.append({
            **room,
            "average": round(sum(averages) / len(averages), 2) if averages else None,
            "days_reported": len(averages),
            "daily": samples,
        })

    def summarize(day: str, samples: list[tuple[float, float, float, int]],
                  room_count: int) -> dict:
        values = [item[0] for item in samples]
        return {
            "date": day,
            "average": round(sum(values) / len(values), 2) if values else None,
            "minimum": round(min(values), 2) if values else None,
            "maximum": round(max(values), 2) if values else None,
            "rooms_measured": len(values),
            "rooms_total": room_count,
            "coverage": round(100 * len(values) / room_count) if room_count else 0,
            "out_of_range": sum(not low <= val <= high for val, low, high, _ in samples),
            "readings_count": sum(item[3] for item in samples),
        }

    series = [summarize(day, per_day[day], len(rooms)) for day in calendar]
    buildings = [
        {
            "name": name,
            "rooms_total": building_counts[name],
            "series": [
                summarize(day, days_map[day], building_counts[name]) for day in calendar
            ],
        }
        for name, days_map in by_building.items()
    ]

    return {
        "timezone": timezone_name,
        "days": days,
        "building": building,
        "rooms_total": len(rooms),
        "series": series,
        "buildings": buildings,
        "rooms": by_room,
        "generated_at": utc_now(),
    }


def list_pairing_codes() -> list[dict]:
    """Active pairing codes metadata only: the secret is intentionally unrecoverable."""
    with connect() as conn:
        return [dict(row) for row in conn.execute("""
            SELECT p.room_id, r.building, r.floor, r.name, p.expires_at
              FROM pairing_codes p JOIN rooms r ON r.id=p.room_id
             WHERE p.expires_at>? AND r.active=1
             ORDER BY r.building, r.name
        """, (utc_now(),))]


def revoke_pairing_code(room_id: int) -> bool:
    """Invalidate an unused code without affecting existing MAX bindings."""
    with connect() as conn:
        result = conn.execute("DELETE FROM pairing_codes WHERE room_id=?", (room_id,))
        return result.rowcount > 0


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

def remember_max_user(user: dict) -> None:
    """Record MAX's own profile fields from a verified private-chat update.

    Missing/empty fields in later updates must not erase a previously known name.
    """
    if not isinstance(user, dict) or user.get("is_bot") or user.get("user_id") is None:
        return
    user_id = str(user["user_id"])
    if not user_id or len(user_id) > 64:
        return
    def profile_field(name: str) -> str:
        value = user.get(name)
        return value.strip()[:120] if isinstance(value, str) else ""
    with connect() as conn:
        conn.execute("""
            INSERT INTO max_users(user_id, first_name, last_name, username, seen_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
              first_name=COALESCE(NULLIF(excluded.first_name, ''), max_users.first_name),
              last_name=COALESCE(NULLIF(excluded.last_name, ''), max_users.last_name),
              username=COALESCE(NULLIF(excluded.username, ''), max_users.username),
              seen_at=excluded.seen_at
        """, (user_id, profile_field("first_name"),
              profile_field("last_name"), profile_field("username"), utc_now()))


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
