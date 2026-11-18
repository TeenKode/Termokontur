"""Durable scheduled MAX report digests for building responsibles.

Subscriptions are explicitly configured in the panel and are disabled by
default. The existing maintenance worker evaluates them roughly hourly.
Each period is enqueued at most once per subscription.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone

from . import db, reports, settings


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
        return cursor.rowcount > 0


def remove(subscription_id: int) -> bool:
    with db.connect() as conn:
        return conn.execute("DELETE FROM report_subscriptions WHERE id=?",
                            (subscription_id,)).rowcount > 0


def _scope(s: dict, local: datetime):
    if local.strftime("%H:%M") < s["send_time"] or (
        s["frequency"] == "weekly" and local.weekday() != s["weekday"]
    ):
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
    local = instant.astimezone(reports.period(
        instant.date().isoformat(), instant.date().isoformat(),
        now=instant,
    )[5])
    queued = 0
    cached = {}
    for s in subscriptions():
        if not s["enabled"]:
            continue
        scope = _scope(s, local)
        if scope is None:
            continue
        start, end, period_key = scope
        with db.connect() as conn:
            if conn.execute(
                "SELECT 1 FROM report_dispatches WHERE subscription_id=? AND period_key=?",
                (s["id"], period_key),
            ).fetchone():
                continue
        key = (start, end, s["building"])
        if key not in cached:
            cached[key] = reports.build(start, end, s["building"], now=instant)
        message = reports.max_summary(cached[key])
        event_id = hashlib.sha256(f"report:{s['id']}:{period_key}".encode()).hexdigest()
        stamp = db.utc_now()
        # May overlap a second worker; primary-key claim and outbox transaction
        # make this safe under at-least-once schedule evaluation.
        with db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            exists = conn.execute("""
                SELECT 1 FROM report_dispatches WHERE subscription_id=? AND period_key=?
            """, (s["id"],period_key)).fetchone()
            active = conn.execute("""
                SELECT 1 FROM report_subscriptions WHERE id=? AND enabled=1
            """, (s["id"],)).fetchone()
            if exists or not active:
                continue
            conn.execute("""
                INSERT INTO max_inbox(event_id,state,updated_at) VALUES(?,'complete',?)
            """, (event_id,stamp))
            conn.execute("""
                INSERT INTO max_outbox(event_id,user_id,text,next_retry_at)
                VALUES(?,?,?,?)
            """, (event_id,s["user_id"],message,stamp))
            conn.execute("""
                INSERT INTO max_outbox_ui(event_id,delivery_mode,notify)
                VALUES(?,'notice',1)
            """, (event_id,))
            conn.execute("""
                INSERT INTO report_dispatches(subscription_id,period_key,event_id)
                VALUES(?,?,?)
            """, (s["id"],period_key,event_id))
            queued += 1
    return {"queued": queued}
