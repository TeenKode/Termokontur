"""Persistent MAX message inbox/outbox shared by polling and HTTPS webhooks.

Incoming events are claimed before parsing and queued before their transport
acknowledgment. Sending is retried independently in the MAX worker; failures
never force re-running the command merely to resend a confirmation.
"""
from __future__ import annotations

import hashlib
import json
import asyncio
import logging
from datetime import datetime, timedelta, timezone

import httpx

from . import db

logger = logging.getLogger("termokontur.max_delivery")


class ProcessingPending(Exception):
    """Message is claimed by an active/incomplete worker; retry later."""


def event_key(update: dict) -> str | None:
    kind = update.get("update_type")
    if kind == "message_created":
        body = (update.get("message") or {}).get("body") or {}
        mid = body.get("mid")
        return "message:" + str(mid) if mid else None
    if kind == "message_callback":
        callback = update.get("callback") or {}
        callback_id = callback.get("callback_id")
        return "callback:" + str(callback_id) if callback_id else None
    if kind == "bot_started":
        user = update.get("user") or {}
        uid = user.get("user_id")
        timestamp = update.get("timestamp")
        if uid is None or timestamp is None:
            return None
        return f"start:{uid}:{timestamp}"
    return None

def enqueue(update: dict) -> bool:
    """Returns False for duplicate/irrelevant events. Raises on processing errors."""
    raw_key = event_key(update)
    if not raw_key:
        return False
    key = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
    now = db.utc_now()
    stale = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(timespec="seconds")
    with db.connect() as conn:
        cur = conn.execute("""
            INSERT INTO max_inbox(event_id, state, updated_at)
            VALUES (?, 'processing', ?)
            ON CONFLICT(event_id) DO UPDATE SET updated_at=excluded.updated_at
            WHERE state='processing' AND max_inbox.updated_at<?
        """, (key, now, stale))
        claimed = bool(cur.rowcount)
    if not claimed:
        with db.connect() as conn:
            row=conn.execute("SELECT state FROM max_inbox WHERE event_id=?", (key,)).fetchone()
        if row and row["state"] == "processing":
            # Do not ACK a webhook or advance a polling marker while another
            # worker may have applied an effect but not persisted its reply.
            raise ProcessingPending("MAX event is still being processed")
        return False
    try:
        from .bot import process_update
        from .max_ui import keyboard
        response = process_update(update)
        with db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if response:
                uid, reply = response
                conn.execute("""
                    INSERT OR IGNORE INTO max_outbox(event_id, user_id, text, next_retry_at)
                    VALUES (?, ?, ?, ?)
                """, (key, uid, reply[:4000], now))
                callback_id = ((update.get("callback") or {}).get("callback_id")
                               if update.get("update_type") == "message_callback" else None)
                conn.execute("""
                    INSERT OR IGNORE INTO max_outbox_ui(event_id, buttons_json, callback_id)
                    VALUES (?, ?, ?)
                """, (key, json.dumps(keyboard(uid), ensure_ascii=False), callback_id))
            conn.execute("""
                UPDATE max_inbox SET state='complete', updated_at=?
                WHERE event_id=?
            """, (db.utc_now(), key))
    except Exception:
        with db.connect() as conn:
            conn.execute("DELETE FROM max_inbox WHERE event_id=? AND state='processing'", (key,))
        raise
    return True

def diagnostics() -> dict:
    """Counts for authenticated administrators; never exposes message contents."""
    stale = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(timespec="seconds")
    with db.connect() as conn:
        outbox = conn.execute("""
            SELECT
                COUNT(*) AS undelivered,
                SUM(CASE WHEN attempts >= 10 THEN 1 ELSE 0 END) AS repeatedly_failed,
                SUM(CASE WHEN next_retry_at <= ? THEN 1 ELSE 0 END) AS ready
            FROM max_outbox WHERE delivered_at IS NULL
        """, (db.utc_now(),)).fetchone()
        stuck = conn.execute("""
            SELECT COUNT(*) FROM max_inbox
            WHERE state='processing' AND updated_at < ?
        """, (stale,)).fetchone()[0]
    return {
        "undelivered": outbox["undelivered"],
        "repeatedly_failed": outbox["repeatedly_failed"] or 0,
        "ready": outbox["ready"] or 0,
        "stalled_events": stuck,
    }


def pending(limit: int = 20) -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute("""
            SELECT o.event_id, o.user_id, o.text, o.attempts,
                   u.buttons_json, u.callback_id
            FROM max_outbox o
            LEFT JOIN max_outbox_ui u ON u.event_id=o.event_id
            WHERE o.delivered_at IS NULL AND o.next_retry_at<=?
            ORDER BY o.next_retry_at, o.event_id LIMIT ?
        """, (db.utc_now(), limit))
        return [dict(row) for row in rows]

def mark_sent(key: str) -> None:
    with db.connect() as conn:
        conn.execute(
            "UPDATE max_outbox SET delivered_at=? WHERE event_id=?",
            (db.utc_now(), key),
        )

def mark_failed(key: str, attempts: int) -> None:
    # Keep retrying with a capped backoff. A prolonged school network outage
    # must not silently strand MAX replies after ten failed attempts.
    delay = min(2 ** min(attempts, 8) * 5, 900)
    next_time = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat(timespec="seconds")
    with db.connect() as conn:
        conn.execute("""
            UPDATE max_outbox SET attempts=attempts+1, next_retry_at=?
            WHERE event_id=? AND delivered_at IS NULL
        """, (next_time, key))

async def drain(client: httpx.AsyncClient) -> None:
    from .max_ui import keyboard
    for item in pending():
        attachments = (json.loads(item["buttons_json"]) if item["buttons_json"]
                       else keyboard(item["user_id"]))
        message = {"text": item["text"], "attachments": attachments}
        try:
            if item["callback_id"]:
                # MAX callbacks must be acknowledged via /answers. Editing the
                # clicked keyboard gives immediate feedback and preserves menu.
                try:
                    response = await client.post(
                        "/answers", params={"callback_id": item["callback_id"]},
                        json={"message": message},
                    )
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code not in (400, 404, 410):
                        raise
                    # MAX no longer accepts this callback (e.g. after outage).
                    # The result still needs to be shown, without re-executing.
                    response = await client.post(
                        "/messages", params={"user_id": item["user_id"]}, json=message,
                    )
                    response.raise_for_status()
            else:
                response = await client.post(
                    "/messages", params={"user_id": item["user_id"]}, json=message,
                )
                response.raise_for_status()
        except httpx.HTTPError as exc:
            mark_failed(item["event_id"], item["attempts"])
            logger.warning("MAX message delivery failed for %s: %s", item["event_id"], exc)
        else:
            mark_sent(item["event_id"])
        await asyncio.sleep(0.06)


async def ensure_webhook_subscription(client: httpx.AsyncClient) -> bool:
    """Refresh a legacy MAX subscription to receive button clicks.

    MAX explicitly supports updating an existing subscription through POST
    /subscriptions. Do not regenerate the secret: it must continue matching the
    value validated by the inbound webhook endpoint.
    Returns True if already current or successfully updated.
    """
    from . import settings
    if settings.get("max_mode", "polling") != "webhook":
        return False
    url = settings.get("max_webhook_url", "") or ""
    secret = settings.get("max_webhook_secret", "") or ""
    if not url or not secret:
        return False
    response = await client.get("/subscriptions")
    response.raise_for_status()
    subscriptions = response.json().get("subscriptions", [])
    if not isinstance(subscriptions, list):
        raise ValueError("Invalid MAX subscriptions response")
    current = next((s for s in subscriptions
                    if isinstance(s, dict) and s.get("url") == url), None)
    required = ("message_created", "bot_started", "message_callback")
    # A null set of update types in MAX means all types are subscribed.
    if current and (current.get("update_types") is None or
                    set(required).issubset(set(current["update_types"]))):
        return True
    response = await client.post("/subscriptions", json={
        "url": url, "secret": secret, "update_types": list(required),
    })
    response.raise_for_status()
    if response.json().get("success") is not True:
        raise ValueError("MAX did not accept subscription refresh")
    logger.info("Updated MAX webhook subscription event types")
    return True


def queue_temperature_alerts() -> int:
    """Queue limited deviation alerts for users assigned to affected rooms."""
    from . import settings
    if settings.get("alerts_enabled", "0") != "1" or not settings.get("max_token", ""):
        return 0
    interval = int(settings.get("alert_cooldown_minutes", "60"))
    now = datetime.now(timezone.utc)
    stamp = now.isoformat(timespec="seconds")
    count = 0
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute("""
            SELECT p.id AS room_id, p.name, p.building, p.min_temp, p.max_temp,
                   b.user_id, x.id AS reading_id, x.value, x.observed_at
            FROM rooms p
            JOIN max_bindings b ON b.room_id=p.id
            JOIN readings x ON x.id=(
                SELECT id FROM readings WHERE room_id=p.id
                ORDER BY observed_at DESC,id DESC LIMIT 1
            )
            WHERE p.active=1
        """).fetchall()
        for row in rows:
            freshness_hours = int(settings.get("stale_hours", "26"))
            if now - datetime.fromisoformat(row["observed_at"]) > timedelta(hours=freshness_hours):
                continue
            state = ("below" if row["value"] < row["min_temp"]
                     else "above" if row["value"] > row["max_temp"] else "normal")
            old=conn.execute("""
                SELECT last_status,last_at FROM temperature_alert_state
                WHERE room_id=? AND user_id=?
            """,(row["room_id"],row["user_id"])).fetchone()
            if state=="normal":
                if old and old["last_status"]!="normal":
                    conn.execute("""UPDATE temperature_alert_state SET last_status='normal'
                        WHERE room_id=? AND user_id=?""",(row["room_id"],row["user_id"]))
                continue
            if old and old["last_status"]==state and old["last_at"]:
                if now - datetime.fromisoformat(old["last_at"]) < timedelta(minutes=interval):
                    continue
            event_raw=f"alert:{row['room_id']}:{row['user_id']}:{row['reading_id']}:{stamp}"
            key=hashlib.sha256(event_raw.encode("utf-8")).hexdigest()
            label="ниже минимума" if state=="below" else "выше максимума"
            msg=(f"Термоконтур: температура {label}!\n"
                 f"{row['building']} / {row['name']}: {row['value']:g} °C\n"
                 f"Допустимый диапазон: {row['min_temp']:g}–{row['max_temp']:g} °C.")
            conn.execute("""INSERT OR IGNORE INTO max_inbox(event_id,state,updated_at)
                            VALUES(?,'complete',?)""",(key,stamp))
            conn.execute("""INSERT OR IGNORE INTO max_outbox(event_id,user_id,text,next_retry_at)
                            VALUES(?,?,?,?)""",(key,row["user_id"],msg,stamp))
            conn.execute("""
                INSERT INTO temperature_alert_state(room_id,user_id,last_status,last_at)
                VALUES(?,?,?,?)
                ON CONFLICT(room_id,user_id) DO UPDATE SET
                    last_status=excluded.last_status,last_at=excluded.last_at
            """,(row["room_id"],row["user_id"],state,stamp))
            count+=1
    return count


def prune(days: int = 30) -> dict:
    """Prune processed message receipts and successfully sent notifications."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    with db.connect() as conn:
        sent=conn.execute("DELETE FROM max_outbox WHERE delivered_at IS NOT NULL AND delivered_at<?", (cutoff,)).rowcount
        inbox=conn.execute("""
            DELETE FROM max_inbox WHERE state='complete' AND updated_at<?
              AND NOT EXISTS (SELECT 1 FROM max_outbox WHERE max_outbox.event_id=max_inbox.event_id)
        """,(cutoff,)).rowcount
    return {"outbox": sent, "inbox": inbox}
