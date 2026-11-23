"""MAX integration, staff assignments and notifications."""
from __future__ import annotations

import csv
import hmac
import io
import ipaddress
import json
import secrets
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .. import db, max_delivery, notifications, settings
from .access import require_admin
from .csv_export import clean_cell

router = APIRouter(prefix="/api", dependencies=[Depends(require_admin)])
webhook_router = APIRouter(prefix="/api")


class BulkPairingCodesIn(BaseModel):
    room_ids: list[int] = Field(min_length=1, max_length=250)
    validity_minutes: Literal[30, 120, 1440, 10080] = 1440


class MaxUserRoomsIn(BaseModel):
    room_ids: list[int] = Field(default_factory=list, max_length=250)
    notify: bool = False


class MaxWebhookIn(BaseModel):
    url: str = Field(min_length=20, max_length=255)


class NotificationSendIn(BaseModel):
    request_id: str = Field(min_length=12, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    text: str = Field(min_length=1, max_length=2000)
    user_ids: list[str] = Field(min_length=1, max_length=250)
    silent: bool = False


class SecretIn(BaseModel):
    value: str | None = None


@router.put("/settings/max-token")
def set_max_token(payload: SecretIn):
    if settings.get("max_mode", "polling") == "webhook":
        raise HTTPException(409, "Сначала отключите webhook MAX, чтобы изменить токен")
    old_token = settings.get("max_token", "") or ""
    if payload.value is not None and payload.value.strip():
        try:
            settings.set_secret("max_token", payload.value.strip())
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    else:
        settings.set_secret("max_token", None)
    configured_token = settings.get("max_token", "") or ""
    if configured_token != old_token:
        # MAX markers belong to an individual bot. An old bot's marker can
        # silently skip events or make polling fail after a token replacement.
        db.set_state("max_marker", "")
        db.set_state("max_poll_last_success_at", "")
        db.set_state("max_poll_last_error", "")
        # The saved menu belongs to a specific bot, not just a MAX user.
        # A newly configured bot must publish its own first menu message.
        with db.connect() as conn:
            conn.execute("DELETE FROM max_menu_messages")
    return {"configured": bool(configured_token)}


def _validate_max_webhook_url(value: str) -> str:
    """Require an externally reachable trusted HTTPS endpoint on port 443."""
    try:
        url = urlsplit(value.strip())
        if (url.scheme != "https" or not url.hostname or url.port not in (None, 443)
                or url.username is not None or url.password is not None
                or url.query or url.fragment or url.path != "/api/max/webhook"):
            raise ValueError()
        hostname = url.hostname
        if hostname in ("localhost",) or hostname.endswith((".local", ".internal")):
            raise ValueError()
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            pass
        else:
            if not address.is_global:
                raise ValueError()
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, "Укажите публичный HTTPS-адрес вида https://domain.example/api/max/webhook (порт 443)") from exc
    return value.strip()


@router.post("/settings/max-webhook")
def enable_max_webhook(payload: MaxWebhookIn):
    url = _validate_max_webhook_url(payload.url)
    token = settings.get("max_token", "")
    if not token:
        raise HTTPException(409, "Сначала сохраните токен MAX")
    secret = secrets.token_urlsafe(32)
    try:
        response = httpx.post(
            "https://platform-api2.max.ru/subscriptions",
            headers={"Authorization": token},
            json={"url": url, "update_types": ["message_created", "bot_started", "message_callback"],
                  "secret": secret},
            timeout=20,
        )
        response.raise_for_status()
        result = response.json()
        if result.get("success") is not True:
            raise ValueError(result.get("message", "MAX отклонил подписку"))
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(502, "Не удалось зарегистрировать webhook в MAX: " + str(exc)[:150]) from exc
    settings.set_max_webhook(url, secret)
    return {"ok": True, "max_mode": "webhook", "url": url}


@router.post("/settings/max-polling")
def switch_max_to_polling():
    url = settings.get("max_webhook_url", "")
    token = settings.get("max_token", "")
    if url:
        if not token:
            raise HTTPException(409, "Отсутствует токен для отмены webhook-подписки")
        try:
            response = httpx.delete(
                "https://platform-api2.max.ru/subscriptions",
                params={"url": url},
                headers={"Authorization": token},
                timeout=20,
            )
            response.raise_for_status()
            if response.json().get("success") is not True:
                raise ValueError("MAX не подтвердил отмену подписки")
        except (httpx.HTTPError, ValueError) as exc:
            raise HTTPException(502, "Не удалось отключить подписку MAX: " + str(exc)[:150]) from exc
    settings.clear_max_webhook()
    return {"ok": True, "max_mode": "polling"}


@webhook_router.post("/max/webhook")
async def max_webhook(request: Request, x_max_bot_api_secret: str | None = Header(default=None)):
    secret = settings.get("max_webhook_secret", "")
    if (settings.get("max_mode", "polling") != "webhook"
            or not secret or not x_max_bot_api_secret
            or not hmac.compare_digest(secret, x_max_bot_api_secret)):
        raise HTTPException(403, "Webhook не настроен или неверный секрет")
    # Check size while streaming: request.body() would buffer an unbounded
    # request before the size limit was applied.
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > 65536:
            raise HTTPException(413, "Слишком большой webhook-запрос")
        raw.extend(chunk)
    try:
        update = json.loads(raw)
        if not isinstance(update, dict):
            raise ValueError()
    except (ValueError, UnicodeError) as exc:
        raise HTTPException(422, "Некорректное событие MAX") from exc
    try:
        max_delivery.enqueue(update)
    except max_delivery.ProcessingPending as exc:
        # MAX retries deliveries with non-200 responses. This preserves
        # unacknowledged events after an interrupted processing attempt.
        raise HTTPException(503, "Событие ещё обрабатывается") from exc
    return {"ok": True}


@router.get("/notifications/recipients")
def notification_recipients():
    return notifications.recipients()


@router.get("/notifications/history")
def notification_history():
    return notifications.history()


@router.post("/notifications/send")
def notification_send(payload: NotificationSendIn):
    try:
        return notifications.queue_batch(
            payload.request_id, payload.text, payload.user_ids, payload.silent,
        )
    except ValueError as exc:
        message = str(exc)
        code = 409 if message.startswith("Сначала настройте") else 422
        raise HTTPException(code, message) from exc


@router.get("/max/diagnostics")
def max_diagnostics():
    """Live MAX checks plus local worker status; no token or message content returned."""
    result = max_delivery.diagnostics()
    mode = settings.get("max_mode", "polling")
    token = settings.get("max_token", "") or ""
    result.update({
        "mode": mode,
        "token_configured": bool(token),
        "token_valid": None,
        "api_check_error": False,
        "bot_username": None,
        "worker_running": False,
        "worker_mode": None,
        "worker_last_seen": None,
        "webhook_registered": None,
        "webhook_check_error": False,
        "polling_blocked_by_webhook": False,
        "subscription_count": None,
        "poll_last_success_at": db.get_state("max_poll_last_success_at") or None,
        "poll_last_error": db.get_state("max_poll_last_error") or None,
    })
    heartbeat = db.get_state("max_worker_heartbeat") or ""
    try:
        timestamp, worker_mode = heartbeat.split("|", 1)
        last = datetime.fromisoformat(timestamp)
        seconds = (datetime.now(timezone.utc) - last).total_seconds()
        if last.tzinfo is not None and -30 <= seconds < 120:
            result["worker_running"] = True
        result["worker_last_seen"] = timestamp
        result["worker_mode"] = worker_mode
    except (ValueError, TypeError):
        pass
    if not token:
        return result

    headers = {"Authorization": token}
    try:
        response = httpx.get("https://platform-api2.max.ru/me", headers=headers, timeout=8.0)
        response.raise_for_status()
        identity = response.json()
        if not isinstance(identity, dict) or not identity.get("is_bot"):
            raise ValueError("Invalid MAX bot identity")
        result["token_valid"] = True
        username = identity.get("username")
        if isinstance(username, str):
            result["bot_username"] = username[:128]
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 401:
            result["token_valid"] = False
        else:
            result["api_check_error"] = True
        return result
    except (httpx.HTTPError, ValueError, TypeError):
        result["api_check_error"] = True
        return result

    try:
        response = httpx.get("https://platform-api2.max.ru/subscriptions",
                             headers=headers, timeout=8.0)
        response.raise_for_status()
        subscriptions = response.json()["subscriptions"]
        if not isinstance(subscriptions, list):
            raise ValueError("Invalid subscriptions response")
        result["subscription_count"] = len(subscriptions)
        if mode == "polling":
            # MAX does not deliver updates via long polling while any webhook
            # subscription is registered for this bot, even on another server.
            result["polling_blocked_by_webhook"] = bool(subscriptions)
        else:
            url = settings.get("max_webhook_url", "")
            result["webhook_registered"] = any(
                isinstance(item, dict) and item.get("url") == url
                for item in subscriptions
            )
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        result["webhook_check_error"] = True
    return result


@router.get("/max/users")
def max_users():
    return JSONResponse(db.list_max_users(), headers={"Cache-Control": "no-store"})


@router.post("/max/users/{user_id}/refresh")
def refresh_max_user_profile(user_id: str):
    """Refresh an existing user's MAX profile using their verified dialog."""
    if not user_id.isdecimal() or len(user_id) > 64:
        raise HTTPException(404, "Пользователь MAX не найден")
    users = db.list_max_users()
    if not any(user["user_id"] == user_id for user in users):
        raise HTTPException(404, "Пользователь MAX не найден")
    chat_id = db.max_user_dialog_chat_id(user_id)
    if chat_id is None:
        raise HTTPException(
            409, "Нет ID диалога. Попросите сотрудника отправить боту /start, затем обновите список."
        )
    token = settings.get("max_token", "") or ""
    if not token:
        raise HTTPException(409, "Сначала настройте токен MAX")
    try:
        response = httpx.get(
            f"https://platform-api2.max.ru/chats/{chat_id}",
            headers={"Authorization": token}, timeout=8,
        )
        response.raise_for_status()
        chat = response.json()
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        raise HTTPException(502, "Не удалось получить профиль из MAX. Повторите позже.") from exc
    if not isinstance(chat, dict) or chat.get("type") != "dialog":
        raise HTTPException(502, "MAX не вернул данные личного диалога")
    profile = chat.get("dialog_with_user")
    if (not isinstance(profile, dict) or profile.get("is_bot")
            or str(profile.get("user_id")) != user_id):
        raise HTTPException(502, "MAX не подтвердил профиль выбранного пользователя")
    if not any(isinstance(profile.get(k), str) and profile[k].strip()
               for k in ("first_name", "last_name", "name", "username")):
        raise HTTPException(409, "MAX не передал имя пользователя. Попросите его проверить профиль.")
    db.remember_max_user(profile)
    return JSONResponse(
        {"ok": True, "profile": next(
            user for user in db.list_max_users() if user["user_id"] == user_id
        )},
        headers={"Cache-Control": "no-store"},
    )


@router.put("/max/users/{user_id}/rooms")
def assign_max_user_rooms(user_id: str, payload: MaxUserRoomsIn):
    if not user_id.isdecimal() or len(user_id) > 64:
        raise HTTPException(404, "Пользователь MAX не найден")
    if payload.notify and not settings.get("max_token", ""):
        raise HTTPException(409, "Для уведомления сначала настройте токен MAX")
    try:
        response = db.set_max_user_rooms(user_id, payload.room_ids, notify=payload.notify)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return JSONResponse(response, headers={"Cache-Control": "no-store"})


@router.get("/max/pairing-codes")
def active_max_codes():
    return JSONResponse(
        db.list_pairing_codes(),
        headers={"Cache-Control": "no-store, no-cache, max-age=0"},
    )


@router.post("/max/pairing-codes")
def issue_max_codes(payload: BulkPairingCodesIn):
    try:
        codes = db.issue_codes_bulk(payload.room_ids, payload.validity_minutes)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return JSONResponse(
        {"codes": codes},
        headers={"Cache-Control": "no-store, no-cache, max-age=0"},
    )


@router.post("/max/pairing-codes.csv")
def export_bulk_max_codes(payload: BulkPairingCodesIn):
    try:
        codes = db.issue_codes_bulk(payload.room_ids, payload.validity_minutes)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    output = io.StringIO(newline="")
    writer = csv.writer(output, delimiter=";")
    writer.writerow(("Корпус", "Этаж", "Кабинет", "ID кабинета",
                     "Код MAX", "Команда боту", "Действует до (UTC)",
                     "Ответственный (для заполнения)"))
    for room in codes:
        writer.writerow((
            clean_cell(room["building"]), clean_cell(room["floor"]),
            clean_cell(room["name"]), room["id"], room["code"],
            "/bind " + room["code"], room["expires_at"], "",
        ))
    return Response(
        content="\ufeff" + output.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="termokontur-max-codes.csv"',
            "Cache-Control": "no-store, no-cache, max-age=0",
            "Pragma": "no-cache",
        },
    )


@router.delete("/rooms/{room_id}/pairing")
def revoke_room_pairing(room_id: int):
    if not db.room_get(room_id):
        raise HTTPException(404, "Кабинет не найден")
    return {"revoked": db.revoke_pairing_code(room_id)}


@router.delete("/rooms/{room_id}/max-bindings/{user_id}")
def unbind_room_max_user(room_id: int, user_id: str):
    if not db.room_get(room_id):
        raise HTTPException(404, "Кабинет не найден")
    if not db.unbind(user_id, room_id):
        raise HTTPException(404, "Привязка MAX не найдена")
    return {"removed": True}


@router.post("/rooms/{room_id}/pairing")
def pairing(room_id: int):
    try:
        code = db.issue_code(room_id)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return {"code": code, "expires_in_minutes": 30}
