"""Daily coverage and optional MAX reminders.

Days are calculated in the installation's configured local timezone.
All measurements remain UTC in storage.
"""
from __future__ import annotations

import os
import re
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from . import db, settings


def local_zone() -> ZoneInfo:
    name = settings.get("timezone", "UTC")
    return ZoneInfo(name)


def daily_bounds(now: datetime | None = None) -> tuple[date, str, str]:
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    zone = local_zone()
    day = instant.astimezone(zone).date()
    start = datetime.combine(day, time.min, tzinfo=zone).astimezone(timezone.utc)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=zone).astimezone(timezone.utc)
    return day, start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds")


def summary(now: datetime | None = None) -> dict:
    day, start, end = daily_bounds(now)
    with db.connect() as conn:
        rows = conn.execute("""
            SELECT p.id, p.building, p.floor, p.name, p.kind,
                   p.min_temp, p.max_temp,
                   r.value AS measured_temp, r.observed_at AS measured_at,
                   r.source AS measured_source
            FROM rooms p
            LEFT JOIN readings r ON r.id = (
                SELECT id FROM readings
                WHERE room_id=p.id AND observed_at>=? AND observed_at<?
                ORDER BY observed_at DESC, id DESC LIMIT 1
            )
            WHERE p.active=1
            ORDER BY p.building, p.name
        """, (start, end))
        rooms = [dict(row) for row in rows]
    counts = {"measured": 0, "missing": 0, "out_of_range": 0}
    for room in rooms:
        if room["measured_at"] is None:
            room["daily_status"] = "missing"
            counts["missing"] += 1
        else:
            counts["measured"] += 1
            if not room["min_temp"] <= room["measured_temp"] <= room["max_temp"]:
                room["daily_status"] = "out_of_range"
                counts["out_of_range"] += 1
            else:
                room["daily_status"] = "normal"
    return {
        "local_date": day.isoformat(),
        "timezone": settings.get("timezone", "UTC"),
        "counts": counts,
        "rooms": rooms,
    }


def pending_users(snapshot: dict) -> dict[str, list[dict]]:
    missing_ids = {r["id"] for r in snapshot["rooms"] if r["daily_status"] == "missing"}
    result: dict[str, list[dict]] = {}
    if not missing_ids:
        return result
    with db.connect() as conn:
        rows = conn.execute("""
            SELECT b.user_id, r.id, r.name, r.building
            FROM max_bindings b JOIN rooms r ON b.room_id=r.id
            WHERE r.active=1 ORDER BY b.user_id, r.building, r.name
        """)
        for row in rows:
            if row["id"] in missing_ids:
                result.setdefault(row["user_id"], []).append(dict(row))
    return result


def reminders_due(now: datetime | None = None) -> bool:
    if settings.get("reminders_enabled", "0") != "1":
        return False
    zone = local_zone()
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    local = instant.astimezone(zone)
    weekdays = settings.get("reminder_days", "0,1,2,3,4")
    try:
        allowed = {int(value.strip()) for value in weekdays.split(",")}
        hour, minute = map(int, settings.get("reminder_time", "09:00").split(":"))
        window = int(settings.get("reminder_window_minutes", "120"))
    except ValueError as exc:
        raise ValueError("Invalid reminder settings") from exc
    if not all(0 <= value <= 6 for value in allowed) or not (0 <= hour <= 23 and 0 <= minute <= 59) or not (1 <= window <= 240):
        raise ValueError("Invalid reminder settings")
    if local.weekday() not in allowed:
        return False
    begin = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return begin <= local < begin + timedelta(minutes=window)


def claim_reminder(day: str, user_id: str, now: datetime | None = None) -> bool:
    """Claim a reminder, retrying stale/failed attempts with a five-minute gap.

    A crash between sending and marking delivery may cause a duplicate
    reminder; this is at-least-once delivery, not exactly-once.
    """
    instant = now or datetime.now(timezone.utc)
    stamp = instant.astimezone(timezone.utc).isoformat(timespec="seconds")
    threshold = (instant.astimezone(timezone.utc) - timedelta(minutes=5)).isoformat(timespec="seconds")
    with db.connect() as conn:
        cursor = conn.execute("""
            INSERT INTO reminder_deliveries(local_day, user_id, status, attempts, updated_at)
            VALUES (?, ?, 'pending', 1, ?)
            ON CONFLICT(local_day, user_id) DO UPDATE SET
                status='pending',
                attempts=reminder_deliveries.attempts+1,
                updated_at=excluded.updated_at
            WHERE reminder_deliveries.status IN ('failed', 'pending')
              AND reminder_deliveries.attempts < 3
              AND reminder_deliveries.updated_at <= ?
        """, (day, str(user_id), stamp, threshold))
        return bool(cursor.rowcount)


def mark_reminder(day: str, user_id: str, sent: bool) -> None:
    with db.connect() as conn:
        conn.execute("""
            UPDATE reminder_deliveries SET status=?, updated_at=?
            WHERE local_day=? AND user_id=? AND status='pending'
        """, ("sent" if sent else "failed", db.utc_now(), day, str(user_id)))


def reminder_text(missing: list[dict]) -> str:
    names = [f"• {room['building']} / {room['name']} (ID {room['id']})" for room in missing]
    return ("Термоконтур: на сегодня ещё нет измерений в помещениях:\n"
            + "\n".join(names[:25])
            + ("\n…и других помещениях." if len(names) > 25 else "")
            + "\n\nОтправьте /temp 21,5 для одного кабинета или /temp ID 21,5 для нескольких.")
