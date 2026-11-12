"""Persistent MAX message inbox/outbox shared by polling and HTTPS webhooks.

Incoming events are claimed before parsing and queued before their transport
acknowledgment. Sending is retried independently in the MAX worker; failures
never force re-running the command merely to resend a confirmation.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone

import httpx

from . import db

logger = logging.getLogger("termokontur.max_delivery")

def event_key(update: dict) -> str | None:
    kind = update.get("update_type")
    if kind == "message_created":
        body = (update.get("message") or {}).get("body") or {}
        mid = body.get("mid")
        return "message:" + str(mid) if mid else None
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
        return False
    try:
        from .bot import process_update
        response = process_update(update)
        with db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if response:
                uid, reply = response
                conn.execute("""
                    INSERT OR IGNORE INTO max_outbox(event_id, user_id, text, next_retry_at)
                    VALUES (?, ?, ?, ?)
                """, (key, uid, reply[:4000], now))
            conn.execute("""
                UPDATE max_inbox SET state='complete', updated_at=?
                WHERE event_id=?
            """, (db.utc_now(), key))
    except Exception:
        with db.connect() as conn:
            conn.execute("DELETE FROM max_inbox WHERE event_id=? AND state='processing'", (key,))
        raise
    return True

def pending(limit: int = 20) -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute("""
            SELECT event_id, user_id, text, attempts FROM max_outbox
            WHERE delivered_at IS NULL AND next_retry_at<=? AND attempts<10
            ORDER BY next_retry_at, event_id LIMIT ?
        """, (db.utc_now(), limit))
        return [dict(row) for row in rows]

def mark_sent(key: str) -> None:
    with db.connect() as conn:
        conn.execute(
            "UPDATE max_outbox SET delivered_at=? WHERE event_id=?",
            (db.utc_now(), key),
        )

def mark_failed(key: str, attempts: int) -> None:
    delay = min(2 ** min(attempts, 8) * 5, 900)
    next_time = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat(timespec="seconds")
    with db.connect() as conn:
        conn.execute("""
            UPDATE max_outbox SET attempts=attempts+1, next_retry_at=?
            WHERE event_id=? AND delivered_at IS NULL
        """, (next_time, key))

async def drain(client: httpx.AsyncClient) -> None:
    for item in pending():
        try:
            response = await client.post(
                "/messages", params={"user_id": item["user_id"]},
                json={"text": item["text"]},
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            mark_failed(item["event_id"], item["attempts"])
            logger.warning("MAX message delivery failed for %s: %s", item["event_id"], exc)
        else:
            mark_sent(item["event_id"])

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
