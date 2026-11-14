"""MAX bot with durable long polling; no public IP or incoming ports required."""
from __future__ import annotations

import asyncio
import logging
import os
import re

import httpx

from . import db, daily, settings, max_delivery, max_ui

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

HELP = ("Термоконтур — сбор температуры.\n"
        "/bind КОД — привязать кабинет (код выдаёт администратор)\n"
        "/rooms — мои кабинеты\n"
        "/today — статус замеров за сегодня\n"
        "/temp 21,5 — отправить температуру для единственного кабинета\n"
        "/temp ID 21,5 — отправить для кабинета с указанным ID\n"
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
            return "Введите /bind КОД — код получите у администратора."
        room = db.bind_code(user_id, parts[1], event_id)
        if room is None:
            return "Код недействителен или истёк. Запросите новый."
        return f"Кабинет привязан: {room['building']} / {room['name']} (ID {room['room_id']})."
    if action == "/rooms":
        items = db.bound_rooms(user_id)
        if not items:
            return "Нет привязанных кабинетов. Введите /bind КОД."
        return "Ваши кабинеты:\n" + "\n".join(
            f"ID {r['id']}: {r['building']} / {r['name']}" for r in items)
    if action == "/today":
        snapshot = daily.summary()
        selected_ids = {room["id"] for room in db.bound_rooms(user_id)}
        if not selected_ids:
            return "Нет привязанных кабинетов. Введите /bind КОД."
        selected = [room for room in snapshot["rooms"] if room["id"] in selected_ids]
        lines = ["Показания за " + snapshot["local_date"] + ":"]
        for room in selected:
            name = room["building"] + " / " + room["name"]
            if room["daily_status"] == "missing":
                lines.append("Нет показаний — " + name)
            else:
                lines.append(f"{room['measured_temp']:g} °C — " + name)
        return "\n".join(lines) if len(lines) > 1 else "Все привязанные кабинеты отключены."
    if action == "/unbind":
        if len(parts) != 2 or not parts[1].isdigit():
            return "Формат: /unbind ID"
        if db.unbind(user_id, int(parts[1])):
            return "Кабинет отвязан."
        return "Этот кабинет не привязан."
    if action == "/temp" or re.fullmatch(r"[-+]?\d{1,2}(?:[.,]\d)?", text):
        values = parts[1:] if action == "/temp" else parts
        allowed = db.bound_rooms(user_id)
        if not allowed:
            return "Сначала привяжите кабинет: /bind КОД"
        if len(values) == 1:
            if len(allowed) != 1:
                return "У вас несколько кабинетов. Укажите /temp ID 21,5"
            room = allowed[0]
            raw = values[0]
        elif len(values) == 2 and values[0].isdigit():
            room = next((r for r in allowed if r["id"] == int(values[0])), None)
            if not room:
                return "Этот кабинет не привязан к вашему аккаунту."
            raw = values[1]
        else:
            return "Формат: /temp 21,5 или /temp ID 21,5"
        try:
            value = float(raw.replace(",", "."))
            entry, created = db.add_reading(room["id"], value, "max",
                                            f"MAX:{user_id}", f"max:{event_id}")
        except ValueError:
            return "Введите корректную температуру, например /temp 21,5"
        if not created:
            return "Это измерение уже записано."
        return f"Сохранено: {room['building']} / {room['name']} — {value:g} °C."
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

async def send_reminders(client: httpx.AsyncClient) -> None:
    """Send at most one reminder per user and local day, with bounded retries."""
    if not daily.reminders_due():
        return
    snapshot = daily.summary()
    for uid, missing in daily.pending_users(snapshot).items():
        if not daily.claim_reminder(snapshot["local_date"], uid):
            continue
        try:
            response = await client.post(
                "/messages", params={"user_id": uid},
                json={"text": daily.reminder_text(missing), "attachments": max_ui.keyboard(uid)},
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            daily.mark_reminder(snapshot["local_date"], uid, False)
            logger.warning("Cannot send reminder to MAX user %s: %s", uid, exc)
        else:
            daily.mark_reminder(snapshot["local_date"], uid, True)
        await asyncio.sleep(0.15)  # under MAX's global request-rate limit


async def poll(token: str):
    headers = {"Authorization": token}
    async with httpx.AsyncClient(base_url=API, headers=headers,
                                 timeout=httpx.Timeout(45, connect=12)) as client:
        backoff = 2
        while True:
            _heartbeat("polling")
            if token != settings.get("max_token", "") or settings.get("max_mode", "polling") != "polling":
                logger.info("MAX mode or token updated; reconnecting")
                return
            try:
                await send_reminders(client)
                max_delivery.queue_temperature_alerts()
                await max_delivery.drain(client)
                marker = db.get_state("max_marker")
                params = {"timeout": 30, "limit": 100}
                if marker:
                    params["marker"] = marker
                response = await client.get("/updates", params=params)
                response.raise_for_status()
                payload = response.json()
                for update in payload.get("updates", []):
                    max_delivery.enqueue(update)
                next_marker = payload.get("marker")
                if next_marker is not None:
                    db.set_state("max_marker", str(next_marker))
                await max_delivery.drain(client)
                db.set_state("max_poll_last_success_at", db.utc_now())
                db.set_state("max_poll_last_error", "")
                backoff = 2
            except (httpx.HTTPError, ValueError, KeyError, max_delivery.ProcessingPending) as exc:
                db.set_state("max_poll_last_error", _poll_failure_reason(exc))
                logger.warning("MAX polling cycle failed: %s", exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 90)

def main():
    logging.basicConfig(level=logging.INFO)
    db.initialize()
    settings.bootstrap()

    async def worker():
        next_webhook_sync = 0.0
        while True:
            token = settings.get("max_token", "") or ""
            if not token:
                _heartbeat("waiting_for_token")
                await asyncio.sleep(10)
                continue
            if settings.get("max_mode", "polling") == "webhook":
                _heartbeat("webhook")
                async with httpx.AsyncClient(base_url=API, headers={"Authorization": token},
                                             timeout=httpx.Timeout(30, connect=12)) as client:
                    now = asyncio.get_running_loop().time()
                    if now >= next_webhook_sync:
                        try:
                            verified = await max_delivery.ensure_webhook_subscription(client)
                        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                            logger.warning("MAX webhook subscription check failed: %s", exc)
                            verified = False
                        next_webhook_sync = now + (3600 if verified else 120)
                    max_delivery.queue_temperature_alerts()
                    await max_delivery.drain(client)
                    await send_reminders(client)
                await asyncio.sleep(3)
            else:
                await poll(token)

    asyncio.run(worker())

if __name__ == "__main__":
    main()
