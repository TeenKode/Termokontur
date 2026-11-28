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

from . import db, max_connection

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

def enqueue(update: dict, *, expected=None) -> bool:
    """Returns False for duplicate/irrelevant events. Raises on processing errors."""
    raw_key = event_key(update)
    if not raw_key:
        return False
    legacy_key = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
    now = db.utc_now()
    stale = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(timespec="seconds")
    with db.atomic() as conn:
        active = max_connection.require_current(conn, expected) if expected else max_connection.current(conn)
        scope = active.scope
        legacy = conn.execute("SELECT 1 FROM max_inbox WHERE event_id=? AND bot_scope=?",
                              (legacy_key, scope)).fetchone()
        key = hashlib.sha256(f"{scope}:{raw_key}".encode()).hexdigest() if scope and not legacy else legacy_key
        cur = conn.execute("""
            INSERT INTO max_inbox(event_id, state, updated_at, bot_scope)
            VALUES (?, 'processing', ?, ?)
            ON CONFLICT(event_id) DO UPDATE SET updated_at=excluded.updated_at
            WHERE state='processing' AND max_inbox.updated_at<?
        """, (key, now, scope, stale))
        claimed = bool(cur.rowcount)
        if not claimed:
            row = conn.execute("SELECT state FROM max_inbox WHERE event_id=?", (key,)).fetchone()
            if row and row["state"] == "processing":
                # Preserve the recovery policy for legacy processing claims.
                raise ProcessingPending("MAX event is still being processed")
            return False
        from .bot import process_update
        from .max_ui import keyboard
        response = process_update(update)
        if response:
            uid, reply = response
            conn.execute("""
                INSERT OR IGNORE INTO max_outbox(event_id, user_id, text, next_retry_at)
                VALUES (?, ?, ?, ?)
            """, (key, uid, reply[:4000], now))
            is_callback = update.get("update_type") == "message_callback"
            callback_id = (update.get("callback") or {}).get("callback_id") if is_callback else None
            # The button belongs to this bot's message; keep its ID so
            # later text commands can edit the menu rather than post anew.
            source_mid = ((update.get("message") or {}).get("body") or {}).get("mid") if is_callback else None
            recipient = ((update.get("message") or {}).get("recipient") or {})
            chat_id = recipient.get("chat_id") if recipient.get("chat_type") == "dialog" else None
            if not isinstance(chat_id, (str, int)) or not str(chat_id).lstrip("-").isdigit():
                chat_id = None
            conn.execute("""
                INSERT OR IGNORE INTO max_outbox_ui(
                    event_id, buttons_json, callback_id, origin_mid, chat_id
                ) VALUES (?, ?, ?, ?, ?)
            """, (key, json.dumps(keyboard(uid), ensure_ascii=False),
                  callback_id, str(source_mid) if source_mid else None,
                  str(chat_id) if chat_id is not None else None))
        conn.execute("""
            UPDATE max_inbox SET state='complete', updated_at=?
            WHERE event_id=?
        """, (db.utc_now(), key))
    return True


def queue_notice(conn, key: str, uid: str, text: str, stamp: str, *, notify=True) -> bool:
    scope = max_connection.current(conn).scope
    if not conn.execute("""INSERT OR IGNORE INTO max_inbox(event_id,state,updated_at,bot_scope)
                         VALUES(?,'complete',?,?)""", (key, stamp, scope)).rowcount:
        return False
    conn.execute("INSERT INTO max_outbox(event_id,user_id,text,next_retry_at) VALUES(?,?,?,?)",
                 (key, uid, text, stamp))
    conn.execute("INSERT INTO max_outbox_ui(event_id,delivery_mode,notify) VALUES(?,'notice',?)",
                 (key, int(notify)))
    return True


def diagnostics() -> dict:
    """Counts for authenticated administrators; never exposes message contents."""
    stale = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(timespec="seconds")
    with db.connect() as conn:
        scope = max_connection.current(conn).scope
        outbox = conn.execute("""
            SELECT
                COUNT(*) AS undelivered,
                SUM(CASE WHEN attempts >= 10 THEN 1 ELSE 0 END) AS repeatedly_failed,
                SUM(CASE WHEN next_retry_at <= ? THEN 1 ELSE 0 END) AS ready
            FROM max_outbox JOIN max_inbox USING(event_id) WHERE delivered_at IS NULL AND bot_scope=?
              AND NOT EXISTS (SELECT 1 FROM max_delivery_failures f WHERE f.event_id=max_outbox.event_id)
        """, (db.utc_now(), scope)).fetchone()
        terminal = conn.execute("""SELECT count(*) FROM max_delivery_failures JOIN max_inbox USING(event_id)
                                 WHERE bot_scope=?""", (scope,)).fetchone()[0]
        stuck = conn.execute("""
            SELECT COUNT(*) FROM max_inbox
            WHERE state='processing' AND updated_at < ? AND bot_scope=?
        """, (stale, scope)).fetchone()[0]
        parked = conn.execute("""SELECT count(*) FROM max_outbox o JOIN max_inbox i USING(event_id)
                              WHERE o.delivered_at IS NULL AND i.bot_scope<>?
                                AND NOT EXISTS (SELECT 1 FROM max_delivery_failures f WHERE f.event_id=o.event_id)
                              """, (scope,)).fetchone()[0]
    return {
        "undelivered": outbox["undelivered"],
        "repeatedly_failed": outbox["repeatedly_failed"] or 0,
        "ready": outbox["ready"] or 0,
        "stalled_events": stuck,
        "permanently_failed": terminal,
        "parked_other_bots": parked,
    }


def pending(limit: int = 20, *, expected=None) -> list[dict]:
    with db.connect() as conn:
        active = max_connection.require_current(conn, expected) if expected else max_connection.current(conn)
        rows = conn.execute("""
            SELECT o.event_id, o.user_id, o.text, o.attempts,
                   u.buttons_json, u.callback_id, u.origin_mid, u.chat_id,
                   COALESCE(u.delivery_mode, 'menu') AS delivery_mode,
                   COALESCE(u.notify, 1) AS notify
            FROM max_outbox o
            JOIN max_inbox i ON i.event_id=o.event_id
            LEFT JOIN max_outbox_ui u ON u.event_id=o.event_id
            WHERE i.bot_scope=? AND o.delivered_at IS NULL AND o.next_retry_at<=?
              AND NOT EXISTS (SELECT 1 FROM max_delivery_failures f WHERE f.event_id=o.event_id)
              AND (COALESCE(u.delivery_mode,'menu') <> 'menu' OR NOT EXISTS (
                  SELECT 1 FROM max_outbox earlier
                  JOIN max_inbox ei ON ei.event_id=earlier.event_id
                  LEFT JOIN max_outbox_ui eu ON eu.event_id=earlier.event_id
                  WHERE ei.bot_scope=i.bot_scope AND earlier.user_id=o.user_id AND earlier.rowid<o.rowid
                    AND earlier.delivered_at IS NULL AND earlier.next_retry_at>?
                    AND COALESCE(eu.delivery_mode,'menu')='menu'
                    AND NOT EXISTS (SELECT 1 FROM max_delivery_failures f WHERE f.event_id=earlier.event_id)
              ))
            ORDER BY o.rowid LIMIT ?
        """, (active.scope, db.utc_now(), db.utc_now(), limit))
        return [dict(row) for row in rows]

def mark_sent(key: str) -> None:
    stamp = db.utc_now()
    with db.connect() as conn:
        conn.execute(
            "UPDATE max_outbox SET delivered_at=? WHERE event_id=?",
            (stamp, key),
        )
        conn.execute(
            "UPDATE notification_recipients SET delivered_at=? WHERE event_id=?",
            (stamp, key),
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

def mark_permanent_failure(key: str, status: int) -> None:
    with db.connect() as conn:
        conn.execute("UPDATE max_outbox SET attempts=attempts+1 WHERE event_id=? AND delivered_at IS NULL", (key,))
        conn.execute("INSERT OR IGNORE INTO max_delivery_failures VALUES(?,?,?)", (key, status, db.utc_now()))


def menu_message_id(uid: str, *, expected=None) -> str | None:
    """The last bot message containing the reusable menu for this user."""
    with db.connect() as conn:
        db.begin(conn)
        if expected:
            max_connection.require_current(conn, expected)
        row = conn.execute(
            "SELECT message_id FROM max_menu_messages WHERE user_id=?", (str(uid),)
        ).fetchone()
    return row["message_id"] if row else None


def set_menu_message_id(uid: str, mid: str, *, expected=None) -> None:
    if not isinstance(mid, str) or not mid:
        return
    with db.connect() as conn:
        db.begin(conn, immediate=True)
        if expected:
            try:
                max_connection.require_current(conn, expected)
            except max_connection.Changed:
                return
        conn.execute("""
            INSERT INTO max_menu_messages(user_id, message_id) VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET message_id=excluded.message_id
        """, (str(uid), mid))


def _response_mid(response: httpx.Response) -> str | None:
    # MAX POST /messages returns {message: {body: {mid: "..."}}}.
    try:
        payload = response.json()
        mid = ((payload.get("message") or {}).get("body") or {}).get("mid")
    except (AttributeError, TypeError, ValueError):
        return None
    return str(mid) if mid else None



async def _send_typing(client: httpx.AsyncClient, chat_id: str) -> None:
    """Best-effort native MAX typing indicator, not an extra chat message.

    MAX documents this action for group chats; some clients may not display
    it in private dialogs. A rejected/unsupported action must never block
    the real answer or affect outbox retries.
    """
    try:
        response = await client.post(
            f"/chats/{chat_id}/actions", json={"action": "typing_on"}, timeout=2.0,
        )
        response.raise_for_status()
        try:
            accepted = response.json().get("success")
        except (ValueError, TypeError, AttributeError):
            accepted = None
        if accepted is False:
            logger.debug("MAX typing action was declined")
    except httpx.HTTPError as exc:
        logger.debug("MAX typing indicator unavailable (%s)", type(exc).__name__)


async def _edit_or_post(client: httpx.AsyncClient, uid: str, message: dict, expected) -> None:
    """Prefer one editable menu per user; create only when editing is impossible.

    Keep the MAX keyboard in every bot message, so it stays editable even
    after the seven-day limit for ordinary text-only messages.
    """
    mid = menu_message_id(uid, expected=expected)
    if mid:
        try:
            response = await client.put(
                "/messages", params={"message_id": mid}, json={**message, "notify": False},
            )
            response.raise_for_status()
            return
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in (400, 404, 410):
                raise
            # The previous bot message was removed or can no longer be edited.
            # Replace the tracked ID only after MAX accepts a new message.
    response = await client.post(
        "/messages", params={"user_id": uid}, json=message,
    )
    response.raise_for_status()
    new_mid = _response_mid(response)
    if new_mid:
        set_menu_message_id(uid, new_mid, expected=expected)


async def drain(client: httpx.AsyncClient, *, expected=None) -> None:
    from .max_ui import keyboard
    blocked_users = set()
    for item in pending(expected=expected):
        if expected:
            with db.connect() as conn:
                max_connection.require_current(conn, expected)
        if item["delivery_mode"] == "menu" and item["user_id"] in blocked_users:
            continue
        if item["delivery_mode"] == "notice":
            message = {"text": item["text"]}
        else:
            attachments = (json.loads(item["buttons_json"]) if item["buttons_json"]
                           else keyboard(item["user_id"]))
            message = {"text": item["text"], "attachments": attachments}
        try:
            # Keep native typing action separate from the reusable menu:
            # never send a fake "typing..." message or delay the reply.
            if item["delivery_mode"] == "menu" and item["chat_id"] and item["attempts"] == 0:
                await _send_typing(client, item["chat_id"])
            if item["delivery_mode"] == "notice":
                # Administrative notices and deviation alerts are new messages;
                # editing the silent menu would hide critical alerts from staff.
                response = await client.post(
                    "/messages", params={"user_id": item["user_id"]},
                    json={"text": item["text"], "notify": bool(item["notify"])},
                )
                response.raise_for_status()
            elif item["callback_id"]:
                # /answers acknowledges a press and edits that SAME menu.
                # Commands entered as text use PUT /messages on the last menu.
                try:
                    response = await client.post(
                        "/answers", params={"callback_id": item["callback_id"]},
                        json={"message": message},
                    )
                    response.raise_for_status()
                    if item["origin_mid"]:
                        set_menu_message_id(item["user_id"], item["origin_mid"], expected=expected)
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code not in (400, 404, 410):
                        raise
                    # The callback expired while queued. Edit the existing
                    # menu instead of creating an unnecessary new message.
                    await _edit_or_post(client, item["user_id"], message, expected)
            else:
                await _edit_or_post(client, item["user_id"], message, expected)
        except httpx.HTTPError as exc:
            status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
            if status in (400, 403, 404, 410):
                # Final rejection, after any supported edit/callback fallback.
                # Preserve an undelivered status and expose it to administrators.
                mark_permanent_failure(item["event_id"], status)
            else:
                mark_failed(item["event_id"], item["attempts"])
                if item["delivery_mode"] == "menu":
                    blocked_users.add(item["user_id"])
            logger.warning("MAX message delivery failed for %s: %s", item["event_id"], exc)
        else:
            mark_sent(item["event_id"])
        await asyncio.sleep(0.06)


async def ensure_webhook_subscription(client: httpx.AsyncClient, *, expected=None) -> bool:
    """Refresh a legacy MAX subscription to receive button clicks.

    MAX explicitly supports updating an existing subscription through POST
    /subscriptions. Do not regenerate the secret: it must continue matching the
    value validated by the inbound webhook endpoint.
    Returns True if already current or successfully updated.
    """
    active = expected or max_connection.current()
    if active.mode != "webhook":
        return False
    url, secret = active.webhook_url, active.webhook_secret
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
    if expected:
        with db.connect() as conn:
            max_connection.require_current(conn, expected)
    response = await client.post("/subscriptions", json={
        "url": url, "secret": secret, "update_types": list(required),
    })
    response.raise_for_status()
    if response.json().get("success") is not True:
        raise ValueError("MAX did not accept subscription refresh")
    logger.info("Updated MAX webhook subscription event types")
    return True


def queue_temperature_alerts(*, expected=None) -> int:
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
        active = max_connection.require_current(conn, expected) if expected else max_connection.current(conn)
        if not active.token:
            return 0
        rows = conn.execute("""
            SELECT p.id AS room_id, p.name, p.building, p.min_temp, p.max_temp,
                   b.user_id, x.id AS reading_id, x.value, x.observed_at, x.source
            FROM rooms p
            JOIN max_bindings b ON b.room_id=p.id
            JOIN readings x ON x.id=(
                SELECT id FROM readings WHERE room_id=p.id
                ORDER BY observed_at DESC,id DESC LIMIT 1
            )
            WHERE p.active=1
        """).fetchall()
        for row in rows:
            # Human-entered values are already known to the person entering them.
            # Only automatic sensor measurements can trigger push notifications.
            if row["source"] != "ha":
                continue
            freshness_hours = int(settings.get("stale_hours", "26"))
            if now - datetime.fromisoformat(row["observed_at"]) > timedelta(hours=freshness_hours):
                continue
            state = ("below" if row["value"] < row["min_temp"]
                     else "above" if row["value"] > row["max_temp"] else "normal")
            old=conn.execute("""
                SELECT last_status,last_at,last_reading_id FROM temperature_alert_state
                WHERE room_id=? AND user_id=?
            """,(row["room_id"],row["user_id"])).fetchone()
            if state=="normal":
                if old and old["last_status"]!="normal":
                    conn.execute("""UPDATE temperature_alert_state SET last_status='normal'
                        WHERE room_id=? AND user_id=?""",(row["room_id"],row["user_id"]))
                continue
            if old and old["last_status"] == state:
                # Do not notify endlessly about a single old measurement.
                if old["last_reading_id"] == row["reading_id"]:
                    continue
                if old["last_at"] and now - datetime.fromisoformat(old["last_at"]) < timedelta(minutes=interval):
                    continue
            event_raw=f"alert:{row['room_id']}:{row['user_id']}:{row['reading_id']}:{stamp}"
            key=hashlib.sha256(event_raw.encode("utf-8")).hexdigest()
            label="ниже минимума" if state=="below" else "выше максимума"
            msg=(f"⚠️ Термоконтур: температура {label}!\n"
                 f"{row['building']} / {row['name']}: {row['value']:g} °C\n"
                 f"Допустимый диапазон: {row['min_temp']:g}–{row['max_temp']:g} °C.")
            queue_notice(conn, key, row["user_id"], msg, stamp)
            conn.execute("""
                INSERT INTO temperature_alert_state(room_id,user_id,last_status,last_at,last_reading_id)
                VALUES(?,?,?,?,?)
                ON CONFLICT(room_id,user_id) DO UPDATE SET
                    last_status=excluded.last_status,last_at=excluded.last_at,
                    last_reading_id=excluded.last_reading_id
            """,(row["room_id"],row["user_id"],state,stamp,row["reading_id"]))
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
        # Keep aggregated history for six months, even after queue rows
        # have been pruned, without retaining message texts indefinitely.
        history_cutoff = (datetime.now(timezone.utc) - timedelta(days=180)).isoformat(timespec="seconds")
        conn.execute("DELETE FROM max_outbox WHERE event_id IN (SELECT event_id FROM max_delivery_failures WHERE failed_at<?)", (history_cutoff,))
        conn.execute("DELETE FROM notification_batches WHERE created_at<?", (history_cutoff,))
    return {"outbox": sent, "inbox": inbox}
