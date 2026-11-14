"""Targeted administrative notifications through the durable MAX outbox."""
from __future__ import annotations

import hashlib
import json

from . import daily, db, settings


def recipients() -> list[dict]:
    """Only active room bindings; one MAX user can have multiple rooms."""
    missing = {r["id"] for r in daily.summary()["rooms"] if r["daily_status"] == "missing"}
    with db.connect() as conn:
        rows = conn.execute("""
            SELECT b.user_id, r.id AS room_id, r.building, r.name,
                   COALESCE(u.first_name, '') AS first_name,
                   COALESCE(u.last_name, '') AS last_name,
                   COALESCE(u.username, '') AS username
            FROM max_bindings b
            JOIN rooms r ON r.id=b.room_id
            LEFT JOIN max_users u ON u.user_id=b.user_id
            WHERE r.active=1
            ORDER BY u.last_name COLLATE NOCASE, u.first_name COLLATE NOCASE,
                     b.user_id, r.building, r.name
        """).fetchall()
    by_user: dict[str, dict] = {}
    for row in rows:
        uid = str(row["user_id"])
        if uid not in by_user:
            display = " ".join(x for x in (row["first_name"], row["last_name"]) if x).strip()
            by_user[uid] = {
                "user_id": uid,
                "name": display or ("@" + row["username"] if row["username"] else "MAX " + uid),
                "rooms": [],
                "missing": False,
            }
        by_user[uid]["rooms"].append({
            "id": row["room_id"], "building": row["building"], "name": row["name"]
        })
        by_user[uid]["missing"] |= row["room_id"] in missing
    return list(by_user.values())


def queue_batch(request_id: str, text: str, user_ids: list[str], silent: bool) -> dict:
    """Atomically queue a batch; retries with the same request id cannot duplicate it."""
    if not settings.get("max_token", ""):
        raise ValueError("Сначала настройте токен MAX")
    text = text.strip()
    if not text or len(text) > 2000:
        raise ValueError("Текст сообщения должен содержать от 1 до 2000 символов")
    if not user_ids or len(user_ids) > 250 or len(set(user_ids)) != len(user_ids):
        raise ValueError("Выберите от 1 до 250 разных сотрудников")
    payload_hash = hashlib.sha256(json.dumps(
        [text, sorted(user_ids), silent], ensure_ascii=False
    ).encode("utf-8")).hexdigest()
    stamp = db.utc_now()
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        previous = conn.execute(
            "SELECT recipients,payload_hash FROM notification_batches WHERE id=?", (request_id,)
        ).fetchone()
        if previous:
            if previous["payload_hash"] != payload_hash:
                raise ValueError("Этот идентификатор отправки уже использован для другого сообщения")
            return {"id": request_id, "queued": previous["recipients"], "already_queued": True}
        placeholders = ",".join("?" for _ in user_ids)
        eligible = {str(row["user_id"]) for row in conn.execute(f"""
            SELECT DISTINCT b.user_id FROM max_bindings b
            JOIN rooms r ON r.id=b.room_id
            WHERE r.active=1 AND b.user_id IN ({placeholders})
        """, user_ids)}
        if eligible != set(user_ids):
            raise ValueError("Список сотрудников изменился. Обновите получателей перед отправкой")
        conn.execute("""
            INSERT INTO notification_batches(id,created_at,text,recipients,silent,payload_hash)
            VALUES(?,?,?,?,?,?)
        """, (request_id, stamp, text, len(user_ids), int(silent), payload_hash))
        rendered = "Термоконтур · Сообщение администрации\n\n" + text
        for uid in user_ids:
            key = hashlib.sha256(f"admin:{request_id}:{uid}".encode()).hexdigest()
            conn.execute(
                "INSERT INTO max_inbox(event_id,state,updated_at) VALUES(?,'complete',?)",
                (key, stamp),
            )
            conn.execute("""
                INSERT INTO max_outbox(event_id,user_id,text,next_retry_at)
                VALUES(?,?,?,?)
            """, (key, uid, rendered, stamp))
            conn.execute("""
                INSERT INTO max_outbox_ui(event_id,delivery_mode,notify)
                VALUES(?,'notice',?)
            """, (key, 0 if silent else 1))
            conn.execute("""
                INSERT INTO notification_recipients(batch_id,user_id,event_id)
                VALUES(?,?,?)
            """, (request_id, uid, key))
    return {"id": request_id, "queued": len(user_ids), "already_queued": False}


def history(limit: int = 30) -> list[dict]:
    """Aggregate persisted delivery receipts, never claim queued is delivered."""
    with db.connect() as conn:
        return [dict(row) for row in conn.execute("""
            SELECT b.id, b.created_at, b.text, b.silent, b.recipients,
                   SUM(CASE WHEN n.delivered_at IS NOT NULL OR o.delivered_at IS NOT NULL
                            THEN 1 ELSE 0 END) AS delivered,
                   SUM(CASE WHEN n.delivered_at IS NULL AND o.delivered_at IS NULL
                                 AND COALESCE(o.attempts,0) > 0 THEN 1 ELSE 0 END) AS retrying
            FROM notification_batches b
            JOIN notification_recipients n ON n.batch_id=b.id
            LEFT JOIN max_outbox o ON o.event_id=n.event_id
            GROUP BY b.id
            ORDER BY b.created_at DESC, b.id DESC
            LIMIT ?
        """, (limit,))]
