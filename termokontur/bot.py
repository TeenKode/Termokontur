"""MAX bot: development mode long polling, no public IP required.

Production deployments should switch to MAX webhooks behind trusted HTTPS.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re

import httpx

from . import db, daily, settings, max_delivery

API = "https://platform-api2.max.ru"
logger = logging.getLogger("termokontur.max")
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
    kind = update.get("update_type")
    if kind == "bot_started":
        user = update.get("user") or {}
        uid = user.get("user_id")
        return (str(uid), HELP) if uid is not None else None
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
    return str(uid), interpret(str(uid), text, str(mid))

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
                json={"text": daily.reminder_text(missing)},
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
            if token != settings.get("max_token", "") or settings.get("max_mode", "polling") != "polling":
                logger.info("MAX mode or token updated; reconnecting")
                return
            try:
                await send_reminders(client)
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
                backoff = 2
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                logger.warning("MAX API temporarily unavailable: %s", exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 90)

def main():
    logging.basicConfig(level=logging.INFO)
    db.initialize()
    settings.bootstrap()

    async def worker():
        while True:
            token = settings.get("max_token", "") or ""
            if not token:
                await asyncio.sleep(10)
                continue
            if settings.get("max_mode", "polling") == "webhook":
                async with httpx.AsyncClient(base_url=API, headers={"Authorization": token},
                                             timeout=httpx.Timeout(30, connect=12)) as client:
                    await max_delivery.drain(client)
                    await send_reminders(client)
                await asyncio.sleep(3)
            else:
                await poll(token)

    asyncio.run(worker())

if __name__ == "__main__":
    main()
