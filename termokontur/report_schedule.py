"""Durable scheduled MAX report digests for building responsibles.

Subscriptions are explicitly configured in the panel and are disabled by
default. The existing maintenance worker evaluates them roughly hourly.
Each period is enqueued at most once per subscription.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, time, timedelta, timezone

from . import db, max_connection, max_delivery, reports, settings


def subscriptions() -> list[dict]:
    with db.connect() as conn:
        return [dict(row) for row in conn.execute("""
            SELECT s.id, s.user_id, s.building, s.frequency, s.send_time,
                   s.weekday, s.enabled, s.created_at,
                   COALESCE(NULLIF(TRIM(COALESCE(u.first_name,'') || ' ' ||
                                        COALESCE(u.last_name,'')), ''),
                            NULLIF(u.username,''),
                            'MAX ID ' || s.user_id) AS recipient,
                   (SELECT COUNT(*) FROM report_dispatches d
                    WHERE d.subscription_id=s.id) AS periods_queued,
                   (SELECT COUNT(*) FROM report_waiting w
                    WHERE w.subscription_id=s.id) AS periods_waiting,
                   (SELECT MAX(o.delivered_at) FROM report_dispatches d
                    JOIN max_outbox o ON o.event_id=d.event_id
                    WHERE d.subscription_id=s.id) AS last_delivered_at
            FROM report_subscriptions s
            LEFT JOIN max_users u ON u.user_id=s.user_id
            ORDER BY s.id DESC
        """)]


def create(user_id: str, building: str, frequency: str, send_time: str,
           weekday: int = 0, enabled: bool = True) -> dict:
    if not user_id.isdecimal() or len(user_id) > 64:
        raise ValueError("Некорректный MAX ID")
    if frequency not in ("daily", "weekly") or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", send_time):
        raise ValueError("Некорректное расписание")
    if type(weekday) is not int or not 0 <= weekday <= 6:
        raise ValueError("Неверный день недели")
    if type(enabled) is not bool or len(building) > 100:
        raise ValueError("Некорректные параметры")
    with db.connect() as conn:
        if not conn.execute("SELECT 1 FROM max_users WHERE user_id=?", (user_id,)).fetchone():
            raise ValueError("Сотрудник не найден. Сначала он должен обратиться к боту MAX")
        if building and not conn.execute(
            "SELECT 1 FROM rooms WHERE building=? AND active=1", (building,)
        ).fetchone():
            raise ValueError("Здание не найдено")
        cur = conn.execute("""
            INSERT INTO report_subscriptions(user_id,building,frequency,send_time,weekday,enabled,created_at)
            VALUES(?,?,?,?,?,?,?)
        """, (user_id, building, frequency, send_time, weekday, int(enabled), db.utc_now()))
        subscription_id = cur.lastrowid
    return next(s for s in subscriptions() if s["id"] == subscription_id)


def set_enabled(subscription_id: int, enabled: bool) -> bool:
    with db.connect() as conn:
        cursor = conn.execute("UPDATE report_subscriptions SET enabled=? WHERE id=?",
                              (int(enabled), subscription_id))
        if not enabled:
            conn.execute("DELETE FROM report_waiting WHERE subscription_id=?", (subscription_id,))
        return cursor.rowcount > 0


def remove(subscription_id: int) -> bool:
    with db.connect() as conn:
        return conn.execute("DELETE FROM report_subscriptions WHERE id=?",
                            (subscription_id,)).rowcount > 0


def _scope(s: dict, local: datetime):
    hour, minute = map(int, s["send_time"].split(":"))
    deadline = datetime.combine(local.date(), time(hour, minute),
                                tzinfo=local.tzinfo).astimezone(timezone.utc)
    if local.astimezone(timezone.utc) < deadline or (
            s["frequency"] == "weekly" and local.weekday() != s["weekday"]):
        return None
    last_day = local.date() - timedelta(days=1)
    start_day = last_day - timedelta(days=6 if s["frequency"] == "weekly" else 0)
    # Same calendar period remains unique even across restarts and retries.
    period_key = f"{s['frequency']}:{start_day.isoformat()}:{last_day.isoformat()}"
    return start_day.isoformat(), last_day.isoformat(), period_key


def queue_due(now: datetime | None = None) -> dict:
    """Enqueue once per recipient/scope/period; never send directly over HTTP."""
    if not settings.get("max_token", ""):
        return {"queued": 0, "reason": "no_max_token"}
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    zone = settings.local_zone()
    local = instant.astimezone(zone)
    queued = 0
    cached = {}
    for s in subscriptions():
        if not s["enabled"]:
            continue
        with db.connect() as conn:
            scopes = {row["period_key"]: (row["first_day"], row["last_day"], row["period_key"])
                      for row in conn.execute("""
                          SELECT * FROM report_waiting WHERE subscription_id=? AND last_day<?
                      """, (s["id"], local.date().isoformat()))}
        scope = _scope(s, local)
        if scope:
            scopes[scope[2]] = scope
        for start, end, period_key in sorted(scopes.values()):
            queued += _queue_period(s, start, end, period_key, instant, zone, cached)
    return {"queued": queued}


def _queue_period(subscription, start, end, period_key, instant, zone, cached) -> int:
    sid = subscription["id"]
    with db.connect() as conn:
        if conn.execute("SELECT 1 FROM report_dispatches WHERE subscription_id=? AND period_key=?",
                        (sid, period_key)).fetchone():
            return 0
    key = (start, end, subscription["building"])
    if key not in cached:
        cached[key] = reports.build(*key, now=instant, zone=zone)
    report = cached[key]
    if report["summary"]["sync_pending"]:
        with db.connect() as conn:
            conn.execute("""
                INSERT OR IGNORE INTO report_waiting(subscription_id,period_key,first_day,last_day)
                SELECT id,?,?,? FROM report_subscriptions WHERE id=? AND enabled=1
                AND NOT EXISTS (
                    SELECT 1 FROM report_dispatches WHERE subscription_id=? AND period_key=?
                )
            """, (period_key, start, end, sid, sid, period_key))
        return 0
    message = reports.max_summary(report)
    event_id = hashlib.sha256(f"report:{sid}:{period_key}".encode()).hexdigest()
    stamp = db.utc_now()
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if not max_connection.current(conn).token:
            return 0
        claim = conn.execute("""
            INSERT OR IGNORE INTO report_dispatches(subscription_id,period_key,event_id)
            SELECT id,?,? FROM report_subscriptions WHERE id=? AND enabled=1
        """, (period_key, event_id, sid))
        if not claim.rowcount:
            return 0
        max_delivery.queue_notice(conn, event_id, subscription["user_id"], message, stamp)
        conn.execute("DELETE FROM report_waiting WHERE subscription_id=? AND period_key=?", (sid, period_key))
    return 1
