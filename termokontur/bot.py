"""MAX bot: development mode long polling, no public IP required.

Production deployments should switch to MAX webhooks behind trusted HTTPS.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re

import httpx

from . import db

API = "https://platform-api2.max.ru"
logger = logging.getLogger("termokontur.max")
HELP = ("Термоконтур — сбор температуры.\n"
        "/bind КОД — привязать кабинет (код выдаёт администратор)\n"
        "/rooms — мои кабинеты\n"
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
        room = db.bind_code(user_id, parts[1])
        if room is None:
            return "Код недействителен или истёк. Запросите новый."
        return f"Кабинет привязан: {room['building']} / {room['name']} (ID {room['room_id']})."
    if action == "/rooms":
        items = db.bound_rooms(user_id)
        if not items:
            return "Нет привязанных кабинетов. Введите /bind КОД."
        return "Ваши кабинеты:\n" + "\n".join(
            f"ID {r['id']}: {r['building']} / {r['name']}" for r in items)
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

async def poll(token: str):
    headers = {"Authorization": token}
    async with httpx.AsyncClient(base_url=API, headers=headers,
                                 timeout=httpx.Timeout(45, connect=12)) as client:
        backoff = 2
        while True:
            try:
                marker = db.get_state("max_marker")
                params = {"timeout": 30, "limit": 100}
                if marker:
                    params["marker"] = marker
                response = await client.get("/updates", params=params)
                response.raise_for_status()
                payload = response.json()
                for update in payload.get("updates", []):
                    processed = process_update(update)
                    if processed is None:
                        continue
                    uid, reply = processed
                    sent = await client.post("/messages", params={"user_id": uid},
                                             json={"text": reply})
                    sent.raise_for_status()
                next_marker = payload.get("marker")
                if next_marker is not None:
                    db.set_state("max_marker", str(next_marker))
                backoff = 2
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                logger.warning("MAX API temporarily unavailable: %s", exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 90)

def main():
    logging.basicConfig(level=logging.INFO)
    token = os.getenv("MAX_BOT_TOKEN", "").strip()
    if not token:
        logger.info("MAX_BOT_TOKEN is unset; bot service disabled")
        return
    db.initialize()
    asyncio.run(poll(token))

if __name__ == "__main__":
    main()
