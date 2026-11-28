"""MAX bot with durable long polling; no public IP or incoming ports required."""
from __future__ import annotations

import asyncio
import logging
import os
import re
import socket

import httpx

from . import db, daily, max_connection, max_delivery, max_ui, settings, write_gate

API = "https://platform-api2.max.ru"
logger = logging.getLogger("termokontur.max")


def _poll_failure_reason(exc: Exception) -> str:
    """Return safe diagnostic codes instead of exposing transport internals."""
    if isinstance(exc, httpx.HTTPStatusError):
        if exc.response.status_code in (401, 403):
            return "authorization"
        if exc.response.status_code == 429:
            return "rate_limit"
        return "api_error"
    if isinstance(exc, httpx.TransportError):
        if "certificate verify failed" in str(exc).lower():
            return "tls_certificate"
        return "connection"
    if isinstance(exc, max_delivery.ProcessingPending):
        return "event_processing"
    return "invalid_response"


def _heartbeat(mode: str) -> None:
    """Expose whether the MAX background service is alive, without disclosing secrets."""
    db.set_state("max_worker_heartbeat", f"{db.utc_now()}|{mode}")

HELP = ("🌡️ Термоконтур — показания температуры.\n"
        "/bind КОД — привязать кабинет (код выдаёт администратор)\n"
        "/rooms — мои кабинеты\n"
        "/today — статус замеров за сегодня\n"
        "/temp 21,5 — температура для одного кабинета\n"
        "/temp ID 21,5 — температура для выбранного кабинета\n"
        "/unbind ID — отключить кабинет\n"
        "/help — помощь")

def interpret(user_id: str, command: str, event_id: str) -> str:
    """Process a text message, only for an authenticated MAX private dialog."""
    text = command.strip()
    parts = text.split()
    if not parts or parts[0].lower() in ("/start", "/help", "помощь"):
        return HELP
    action = parts[0].lower().split("@")[0]
    if action == "/bind":
        if len(parts) != 2:
            return "🔗 Запросите код у администратора и отправьте /bind КОД."
        room = db.bind_code(user_id, parts[1], event_id)
        if room is None:
            return "⚠️ Код недействителен или истёк. Запросите новый."
        return f"✅ Кабинет привязан: {room['building']} / {room['name']} (ID {room['room_id']})."
    if action == "/rooms":
        items = db.bound_rooms(user_id)
        if not items:
            return "🔗 Нет привязанных кабинетов. Введите /bind КОД."
        return "🏫 Ваши кабинеты:\n" + "\n".join(
            f"ID {r['id']}: {r['building']} / {r['name']}" for r in items)
    if action == "/today":
        snapshot = daily.summary()
        selected_ids = {room["id"] for room in db.bound_rooms(user_id)}
        if not selected_ids:
            return "🔗 Нет привязанных кабинетов. Введите /bind КОД."
        selected = [room for room in snapshot["rooms"] if room["id"] in selected_ids]
        lines = ["📅 Показания за " + snapshot["local_date"] + ":"]
        for room in selected:
            name = room["building"] + " / " + room["name"]
            if room["measured_temp"] is not None:
                lines.append(f"✅ {room['measured_temp']:g} °C — " + name)
            elif room["daily_status"] == "awaiting":
                lines.append("📶 Ожидаем выгрузку датчика — " + name)
            elif room["expected"]:
                lines.append("⏳ Нет показаний — " + name)
            else:
                lines.append("📅 По графику свободно — " + name)
        return "\n".join(lines) if len(lines) > 1 else "ℹ️ Все привязанные кабинеты отключены."
    if action == "/unbind":
        if len(parts) != 2 or not parts[1].isdigit():
            return "⚠️ Формат: /unbind ID"
        if db.unbind(user_id, int(parts[1])):
            return "✅ Кабинет отвязан."
        return "ℹ️ Этот кабинет не привязан."
    if action == "/temp" or re.fullmatch(r"[-+]?\d{1,2}(?:[.,]\d)?", text):
        values = parts[1:] if action == "/temp" else parts
        allowed = db.bound_rooms(user_id)
        if not allowed:
            return "🔗 Сначала привяжите кабинет: /bind КОД"
        if len(values) == 1:
            if len(allowed) != 1:
                return "🏫 Укажите кабинет: /temp ID 21,5"
            room = allowed[0]
            raw = values[0]
        elif len(values) == 2 and values[0].isdigit():
            room = next((r for r in allowed if r["id"] == int(values[0])), None)
            if not room:
                return "⛔ Этот кабинет не привязан к вашему аккаунту."
            raw = values[1]
        else:
            return "⚠️ Формат: /temp 21,5 или /temp ID 21,5"
        # Use the same transaction and shared room/day rule as callback buttons.
        # Legacy /temp commands must never bypass the common daily MAX entry.
        return max_ui._write(user_id, room["id"], "s", raw, f"max:message:{event_id}")
    return HELP

def process_update(update: dict) -> tuple[str, str] | None:
    """Accept private messages, bot starts and MAX callback button presses."""
    kind = update.get("update_type")
    if kind == "bot_started":
        user = update.get("user") or {}
        uid = user.get("user_id")
        if uid is None or user.get("is_bot"):
            return None
        uid = str(uid)
        db.remember_max_user(user)
        payload = update.get("payload") or ""
        return uid, max_ui.handle_start(uid, str(payload), f"start:{update.get('timestamp', '')}")
    if kind == "message_callback":
        callback = update.get("callback") or {}
        user = callback.get("user") or {}
        message = update.get("message") or {}
        recipient = message.get("recipient") or {}
        uid = user.get("user_id")
        callback_id = callback.get("callback_id")
        if uid is None or user.get("is_bot") or not callback_id or recipient.get("chat_type") != "dialog":
            return None
        uid = str(uid)
        db.remember_max_user(user)
        return uid, max_ui.handle_callback(uid, str(callback.get("payload") or ""), str(callback_id))
    if kind != "message_created":
        return None
    message = update.get("message") or {}
    sender = message.get("sender") or {}
    recipient = message.get("recipient") or {}
    if sender.get("is_bot") or recipient.get("chat_type") != "dialog":
        return None
    uid = sender.get("user_id")
    body = message.get("body") or {}
    text = body.get("text")
    mid = body.get("mid")
    if uid is None or not isinstance(text, str) or not mid:
        return None
    uid = str(uid)
    db.remember_max_user(sender)
    stripped = text.strip()
    if stripped.split(maxsplit=1)[0:1] == ["/start"]:
        payload = stripped.split(maxsplit=1)
        return uid, max_ui.handle_start(uid, payload[1].strip() if len(payload) > 1 else "", str(mid))
    interactive = max_ui.handle_message(uid, text, str(mid))
    if interactive is not None:
        return uid, interactive
    result = interpret(uid, text, str(mid))
    if stripped.lower().startswith(("/bind ", "/unbind ")):
        max_ui._set(uid)
    return uid, result

async def send_reminders(client: httpx.AsyncClient, *, expected=None) -> None:
    """Send at most one reminder per user and local day, with bounded retries."""
    if not daily.reminders_due():
        return
    snapshot = daily.summary()
    for uid, missing in daily.pending_users(snapshot).items():
        # Another assigned employee may have submitted since the initial snapshot.
        current = daily.summary()
        missing_now = {room["id"] for room in current["rooms"]
                       if room["daily_status"] == "missing"}
        still_missing = [room for room in missing if room["id"] in missing_now]
        if not still_missing or current["local_date"] != snapshot["local_date"]:
            continue
        with db.atomic() as conn:
            if expected:
                max_connection.require_current(conn, expected)
            claimed = daily.claim_reminder(snapshot["local_date"], uid)
        if not claimed:
            continue
        try:
            response = await client.post(
                "/messages", params={"user_id": uid},
                json={"text": daily.reminder_text(still_missing), "attachments": max_ui.keyboard(uid)},
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            daily.mark_reminder(snapshot["local_date"], uid, False)
            logger.warning("Cannot send reminder to MAX user %s: %s", uid, exc)
        else:
            daily.mark_reminder(snapshot["local_date"], uid, True)
        await asyncio.sleep(0.15)  # under MAX's global request-rate limit


async def _identified(client: httpx.AsyncClient, active: max_connection.Connection):
    if active.bot_id:
        return active
    response = await client.get("/me", timeout=8)
    response.raise_for_status()
    return max_connection.identify(active, response.json())


async def poll(token: str):
    headers = {"Authorization": token}
    async with httpx.AsyncClient(base_url=API, headers=headers,
                                 timeout=httpx.Timeout(45, connect=12)) as client:
        backoff = 2
        while True:
            try:
                with write_gate.activity():
                    active = max_connection.current()
                    if active.token != token or active.mode != "polling":
                        return
                    _heartbeat("polling")
                    try:
                        active = await _identified(client, active)
                        await send_reminders(client, expected=active)
                        max_delivery.queue_temperature_alerts(expected=active)
                        await max_delivery.drain(client, expected=active)
                        marker = max_connection.marker(active)
                        params = {"timeout": 30, "limit": 100}
                        if marker:
                            params["marker"] = marker
                        response = await client.get("/updates", params=params)
                        response.raise_for_status()
                        payload = response.json()
                        for update in payload.get("updates", []):
                            max_delivery.enqueue(update, expected=active)
                        max_connection.poll_result(active, cursor=payload.get("marker"))
                        await max_delivery.drain(client, expected=active)
                    except (httpx.HTTPError, ValueError, KeyError, max_delivery.ProcessingPending) as exc:
                        max_connection.poll_result(active, error=_poll_failure_reason(exc))
                        logger.warning("MAX polling cycle failed: %s", exc)
                    else:
                        backoff = 2
                        continue
            except write_gate.Busy:
                await asyncio.sleep(1)
                continue
            except max_connection.Changed:
                return
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 90)

def main():
    logging.basicConfig(level=logging.INFO)
    with write_gate.startup():
        db.initialize()
        settings.bootstrap()
    if notify_socket := os.getenv("NOTIFY_SOCKET"):
        address = "\0" + notify_socket[1:] if notify_socket.startswith("@") else notify_socket
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as connection:
            connection.sendto(b"READY=1", address)

    async def worker():
        next_webhook_sync = 0.0
        webhook_generation = ""
        while True:
            try:
                with write_gate.activity():
                    active = max_connection.current()
                    if not active.token:
                        _heartbeat("waiting_for_token")
                    elif active.mode == "webhook":
                        _heartbeat("webhook")
                        async with httpx.AsyncClient(base_url=API, headers={"Authorization": active.token},
                                                     timeout=httpx.Timeout(30, connect=12)) as client:
                            try:
                                active = await _identified(client, active)
                                now = asyncio.get_running_loop().time()
                                if active.generation != webhook_generation or now >= next_webhook_sync:
                                    verified = await max_delivery.ensure_webhook_subscription(client, expected=active)
                                    webhook_generation = active.generation
                                    next_webhook_sync = now + (3600 if verified else 120)
                                max_delivery.queue_temperature_alerts(expected=active)
                                await max_delivery.drain(client, expected=active)
                                await send_reminders(client, expected=active)
                            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                                logger.warning("MAX webhook cycle failed: %s", exc)
            except (write_gate.Busy, max_connection.Changed):
                await asyncio.sleep(1)
                continue
            if active.token and active.mode == "polling":
                await poll(active.token)
            else:
                await asyncio.sleep(3 if active.token else 10)

    asyncio.run(worker())

if __name__ == "__main__":
    main()
