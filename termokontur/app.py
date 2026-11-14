"""Authenticated web dashboard and machine-ingest API."""
from __future__ import annotations

import csv
import asyncio
import tempfile
import hashlib
import json
import hmac
import io
import os
import secrets
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit
import ipaddress
import httpx
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import auth, db, daily, settings, updates, max_delivery, maintenance

ROOT = Path(__file__).resolve().parent
from . import __version__

# A content fingerprint changes whenever JavaScript OR CSS changes, even between
# deployments using the same semantic version number.
_ASSET_FINGERPRINT = hashlib.sha256(
    b"".join((ROOT / "static" / name).read_bytes() for name in ("app.js", "style.css"))
).hexdigest()[:16]

app = FastAPI(title="Termokontur", version=__version__, docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
FAILED_LOGINS: dict[str, list[float]] = {}

async def scheduled_maintenance():
    while True:
        await asyncio.sleep(30)
        try:
            await asyncio.to_thread(maintenance.schedule)
        except Exception:
            import logging
            logging.getLogger("termokontur.maintenance").exception("Scheduled maintenance failed")
        await asyncio.sleep(3600)


@app.on_event("startup")
async def startup():
    if len(os.environ.get("TERMOKONTUR_SECRET", "")) < 32 or os.environ["TERMOKONTUR_SECRET"].startswith("REPLACE_WITH_"):
        raise RuntimeError("Set a unique TERMOKONTUR_SECRET (at least 32 characters)")
    db.initialize()
    auth.bootstrap()
    settings.bootstrap()
    app.state.maintenance_task = asyncio.create_task(scheduled_maintenance())


@app.on_event("shutdown")
async def stop_maintenance():
    task = getattr(app.state, "maintenance_task", None)
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
    )
    if request.url.path.startswith("/api/") or request.url.path.startswith("/static/"):
        # Static assets changed during a native update must never be mixed with
        # a newer index.html through stale browser/intermediary caches.
        response.headers["Cache-Control"] = "no-store"
    return response

def signature(message: str) -> str:
    return hmac.new(os.environ["TERMOKONTUR_SECRET"].encode(),
                    message.encode(), "sha256").hexdigest()

def session_cookie() -> str:
    payload = f"{int(time.time()) + 8 * 3600}:{secrets.token_urlsafe(18)}"
    return f"{payload}:{signature(payload + ':' + str(auth.session_epoch()))}"

def authenticated(request: Request) -> bool:
    cookie = request.cookies.get("tk_session", "")
    try:
        expires, nonce, mac = cookie.split(":", 2)
        if int(expires) <= time.time() or len(nonce) < 12:
            return False
        return hmac.compare_digest(
            mac, signature(f"{expires}:{nonce}:{auth.session_epoch()}")
        )
    except (ValueError, KeyError):
        return False

def require_admin(request: Request):
    if not authenticated(request):
        raise HTTPException(401, "Необходимо войти в систему")
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if origin:
            host = request.headers.get("host", "")
            if origin not in (f"http://{host}", f"https://{host}"):
                raise HTTPException(403, "Недопустимый источник запроса")

class Login(BaseModel):
    password: str

class RoomIn(BaseModel):
    building: str = Field(min_length=1, max_length=100)
    floor: str = Field(default="", max_length=30)
    name: str = Field(min_length=1, max_length=100)
    kind: str = Field(default="Кабинет", min_length=1, max_length=80)
    min_temp: float = Field(default=18, ge=-40, le=60, allow_inf_nan=False)
    max_temp: float = Field(default=24, ge=-40, le=60, allow_inf_nan=False)

class RoomPatch(BaseModel):
    building: str | None = Field(default=None, min_length=1, max_length=100)
    floor: str | None = Field(default=None, max_length=30)
    name: str | None = Field(default=None, min_length=1, max_length=100)
    kind: str | None = Field(default=None, min_length=1, max_length=80)
    min_temp: float | None = Field(default=None, ge=-40, le=60, allow_inf_nan=False)
    max_temp: float | None = Field(default=None, ge=-40, le=60, allow_inf_nan=False)
    active: bool | None = None

class RoomsBulkIn(BaseModel):
    building: str = Field(min_length=1, max_length=100)
    floor: str = Field(default="", max_length=30)
    kind: str = Field(default="Кабинет", min_length=1, max_length=80)
    min_temp: float = Field(default=18, ge=-40, le=60, allow_inf_nan=False)
    max_temp: float = Field(default=24, ge=-40, le=60, allow_inf_nan=False)
    names: list[str] = Field(min_length=1, max_length=250)


class BulkPairingCodesIn(BaseModel):
    room_ids: list[int] = Field(min_length=1, max_length=250)
    validity_minutes: Literal[30, 120, 1440, 10080] = 1440


class SettingsIn(BaseModel):
    organization_name: str = Field(min_length=1, max_length=100)
    timezone: str = Field(min_length=1, max_length=64)
    stale_hours: int = Field(ge=1, le=168)
    reminders_enabled: bool
    reminder_time: str
    reminder_days: list[int] = Field(min_length=1, max_length=7)
    reminder_window_minutes: int = Field(ge=1, le=240)
    backup_interval_days: int = Field(default=1, ge=0, le=30)
    backup_keep: int = Field(default=30, ge=1, le=100)
    retention_days: int = Field(default=0, ge=0, le=3650)
    alerts_enabled: bool = False
    alert_cooldown_minutes: int = Field(default=60, ge=5, le=1440)


class RestoreBackupIn(BaseModel):
    name: str
    password: str


class MaxWebhookIn(BaseModel):
    url: str = Field(min_length=20, max_length=255)


class PasswordIn(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8, max_length=256)


class SecretIn(BaseModel):
    value: str | None = None


class ReadingIn(BaseModel):
    room_id: int = Field(gt=0)
    value: float = Field(ge=-40, le=60, allow_inf_nan=False)

class IngestIn(ReadingIn):
    device: str = Field(default="", max_length=128)
    event_id: str | None = Field(default=None, max_length=128)

@app.get("/")
def index():
    # Never cache the HTML shell: each request must fetch the correct assets.
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    html = html.replace("__ASSET_FINGERPRINT__", _ASSET_FINGERPRINT)
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})

@app.get("/health")
def health():
    return {"ok": True, "service": "termokontur"}

@app.get("/api/session")
def session(request: Request):
    return {"authenticated": authenticated(request)}

@app.post("/api/login")
def login(payload: Login, request: Request, response: Response):
    ip = request.client.host if request.client else "unknown"
    now = time.monotonic()
    failures = [t for t in FAILED_LOGINS.get(ip, []) if now - t < 300]
    if len(failures) >= 5:
        raise HTTPException(429, "Слишком много попыток входа. Повторите позже.")
    if not auth.check_password(payload.password):
        failures.append(now)
        FAILED_LOGINS[ip] = failures
        raise HTTPException(401, "Неверный пароль")
    FAILED_LOGINS.pop(ip, None)
    response.set_cookie("tk_session", session_cookie(), httponly=True, samesite="strict",
                        secure=os.getenv("TERMOKONTUR_SECURE_COOKIE") == "1",
                        max_age=8 * 3600, path="/")
    return {"ok": True}

@app.post("/api/logout", dependencies=[Depends(require_admin)])
def logout(response: Response):
    response.delete_cookie("tk_session", path="/", samesite="strict")
    return {"ok": True}

@app.get("/api/settings", dependencies=[Depends(require_admin)])
def get_settings():
    return settings.public()


@app.put("/api/settings", dependencies=[Depends(require_admin)])
def save_settings(payload: SettingsIn):
    try:
        data = settings.validated(payload.model_dump())
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    settings.update(data)
    return settings.public()


@app.put("/api/settings/max-token", dependencies=[Depends(require_admin)])
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


@app.post("/api/settings/max-webhook", dependencies=[Depends(require_admin)])
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


@app.post("/api/settings/max-polling", dependencies=[Depends(require_admin)])
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


@app.post("/api/max/webhook")
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


@app.get("/api/max/diagnostics", dependencies=[Depends(require_admin)])
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


@app.post("/api/settings/ingest-key", dependencies=[Depends(require_admin)])
def rotate_ingest_key():
    key = secrets.token_urlsafe(36)
    settings.set_secret("ingest_key", key)
    return {"key": key, "warning": "Старый ключ недействителен. Обновите настройки Home Assistant."}


@app.post("/api/settings/password", dependencies=[Depends(require_admin)])
def change_password(payload: PasswordIn, response: Response):
    try:
        changed = auth.change_password(payload.current_password, payload.new_password)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if not changed:
        raise HTTPException(403, "Текущий пароль указан неверно")
    response.delete_cookie("tk_session", path="/", samesite="strict")
    return {"ok": True, "relogin_required": True}


@app.get("/api/update/status", dependencies=[Depends(require_admin)])
def update_status():
    return updates.status()


@app.get("/api/update/check", dependencies=[Depends(require_admin)])
def check_update():
    return updates.check()


@app.post("/api/update/request", dependencies=[Depends(require_admin)])
def request_update():
    try:
        return updates.request()
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc



@app.get("/api/backups", dependencies=[Depends(require_admin)])
def backups():
    return maintenance.list_backups()


@app.post("/api/backups", dependencies=[Depends(require_admin)])
def backup_now():
    return maintenance.create_backup()


@app.get("/api/backups/{name}", dependencies=[Depends(require_admin)])
def download_backup(name: str):
    try:
        path = maintenance.backup_path(name)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    return FileResponse(path, filename=name, media_type="application/octet-stream",
                        headers={"Cache-Control":"no-store"})


@app.delete("/api/backups/{name}", dependencies=[Depends(require_admin)])
def remove_backup(name: str):
    try:
        maintenance.delete_backup(name)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"ok": True}


@app.post("/api/backups/restore", dependencies=[Depends(require_admin)])
def restore_backup(payload: RestoreBackupIn, response: Response):
    if not auth.check_password(payload.password):
        raise HTTPException(403, "Неверный пароль администратора")
    try:
        result = maintenance.restore(payload.name)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        import logging
        logging.getLogger("termokontur.backups").exception("Restore failed")
        raise HTTPException(500, "Не удалось восстановить резервную копию") from exc
    response.delete_cookie("tk_session", path="/", samesite="strict")
    return result


@app.post("/api/backups/import", dependencies=[Depends(require_admin)])
async def import_backup(request: Request):
    length = request.headers.get("content-length")
    if length is not None and (not length.isdigit() or int(length) > maintenance.MAX_IMPORT_BYTES):
        raise HTTPException(413, "Размер файла превышает 100 МБ")
    destination = maintenance.backup_dir()
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", prefix=".incoming-", dir=destination,
                                         delete=False) as tmp:
            temp_path = Path(tmp.name)
            size = 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > maintenance.MAX_IMPORT_BYTES:
                    raise HTTPException(413, "Размер файла превышает 100 МБ")
                tmp.write(chunk)
        temp_path.chmod(0o600)
        maintenance.inspect(temp_path)
        name = maintenance._filename("import")
        temp_path.rename(destination / name)
        return {"name": name, "size": size}
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    finally:
        if temp_path and temp_path.exists():
            temp_path.unlink()


@app.post("/api/maintenance/prune", dependencies=[Depends(require_admin)])
def prune_readings(payload: RestoreBackupIn):
    if not auth.check_password(payload.password):
        raise HTTPException(403, "Неверный пароль администратора")
    interval = int(settings.get("retention_days", "0"))
    if not interval:
        raise HTTPException(422, "В настройках отключено удаление старых измерений")
    # Create a safety copy before irreversible cleanup.
    snapshot = maintenance.create_backup("manual")
    result = maintenance.prune_data(interval)
    return {"deleted": result["readings"], "backup": snapshot["name"]}


@app.get("/api/dashboard", dependencies=[Depends(require_admin)])
def dashboard():
    return db.dashboard(int(settings.get("stale_hours", "26")))

@app.get("/api/daily", dependencies=[Depends(require_admin)])
def daily_report():
    return daily.summary()

@app.get("/api/rooms", dependencies=[Depends(require_admin)])
def rooms(include_inactive: bool = False):
    return db.room_list(active_only=not include_inactive)

@app.get("/api/rooms/export.csv", dependencies=[Depends(require_admin)])
def export_rooms(include_inactive: bool = True):
    output = io.StringIO()
    writer = csv.writer(output, delimiter=";")
    writer.writerow(("ID", "Корпус", "Этаж", "Кабинет", "Тип помещения",
                     "Мин. °C", "Макс. °C", "Состояние", "Привязка MAX",
                     "Пользователи MAX"))
    for room in db.room_list(active_only=not include_inactive):
        writer.writerow([room["id"], clean_cell(room["building"]),
                         clean_cell(room["floor"]), clean_cell(room["name"]),
                         clean_cell(room["kind"]), room["min_temp"],
                         room["max_temp"], "Активен" if room["active"] else "Отключён",
                         "Есть" if room["max_users"] else "Нет",
                         clean_cell("; ".join(
                             " ".join(filter(None, (user["first_name"], user["last_name"])))
                             or ("MAX ID " + user["user_id"])
                             for user in room["max_users"]))])
    return Response(
        content="\ufeff" + output.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="termokontur-rooms.csv"'},
    )


@app.post("/api/rooms", status_code=201, dependencies=[Depends(require_admin)])
def create_room(payload: RoomIn):
    data = payload.model_dump()
    data = {k: v.strip() if isinstance(v, str) else v for k, v in data.items()}
    if not data["building"] or not data["name"] or not data["kind"]:
        raise HTTPException(422, "Заполните название, корпус и тип помещения")
    if data["min_temp"] >= data["max_temp"]:
        raise HTTPException(422, "Минимальная температура должна быть ниже максимальной")
    try:
        return db.create_room(data["building"], data["floor"], data["name"], data["kind"],
                              data["min_temp"], data["max_temp"])
    except sqlite3.IntegrityError:
        raise HTTPException(409, "В этом корпусе кабинет с таким названием уже существует")

@app.post("/api/rooms/bulk", dependencies=[Depends(require_admin)])
def bulk_rooms(payload: RoomsBulkIn):
    data = payload.model_dump()
    names = [name.strip() for name in data.pop("names")]
    if any(len(name) > 100 or not name for name in names):
        raise HTTPException(422, "Неверные названия помещений")
    if data["min_temp"] >= data["max_temp"]:
        raise HTTPException(422, "Минимум должен быть ниже максимума")
    try:
        return db.create_rooms_bulk(data["building"], data["floor"], data["kind"],
                                    data["min_temp"], data["max_temp"], names)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.patch("/api/rooms/{room_id}", dependencies=[Depends(require_admin)])
def patch_room(room_id: int, payload: RoomPatch):
    room = db.room_get(room_id)
    if not room:
        raise HTTPException(404, "Кабинет не найден")
    patch = payload.model_dump(exclude_unset=True)
    if any(value is None for value in patch.values()):
        raise HTTPException(422, "Поля нельзя обнулять")
    for key, value in patch.items():
        if isinstance(value, str):
            patch[key] = value.strip()
    if any(patch.get(key) == "" for key in ("name", "building", "kind") if key in patch):
        raise HTTPException(422, "Поле не может быть пустым")
    merged = {**room, **patch}
    if merged["min_temp"] >= merged["max_temp"]:
        raise HTTPException(422, "Неверный диапазон температуры")
    try:
        return db.update_room(room_id, patch)
    except sqlite3.IntegrityError:
        raise HTTPException(409, "Кабинет с таким названием уже существует")

@app.get("/api/max/pairing-codes", dependencies=[Depends(require_admin)])
def active_max_codes():
    return JSONResponse(
        db.list_pairing_codes(),
        headers={"Cache-Control": "no-store, no-cache, max-age=0"},
    )


@app.post("/api/max/pairing-codes", dependencies=[Depends(require_admin)])
def issue_max_codes(payload: BulkPairingCodesIn):
    try:
        codes = db.issue_codes_bulk(payload.room_ids, payload.validity_minutes)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return JSONResponse(
        {"codes": codes},
        headers={"Cache-Control": "no-store, no-cache, max-age=0"},
    )


@app.post("/api/max/pairing-codes.csv", dependencies=[Depends(require_admin)])
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


@app.delete("/api/rooms/{room_id}/pairing", dependencies=[Depends(require_admin)])
def revoke_room_pairing(room_id: int):
    if not db.room_get(room_id):
        raise HTTPException(404, "Кабинет не найден")
    return {"revoked": db.revoke_pairing_code(room_id)}


@app.delete("/api/rooms/{room_id}/max-bindings/{user_id}", dependencies=[Depends(require_admin)])
def unbind_room_max_user(room_id: int, user_id: str):
    if not db.room_get(room_id):
        raise HTTPException(404, "Кабинет не найден")
    if not db.unbind(user_id, room_id):
        raise HTTPException(404, "Привязка MAX не найдена")
    return {"removed": True}


@app.post("/api/rooms/{room_id}/pairing", dependencies=[Depends(require_admin)])
def pairing(room_id: int):
    try:
        code = db.issue_code(room_id)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return {"code": code, "expires_in_minutes": 30}

@app.post("/api/readings", status_code=201, dependencies=[Depends(require_admin)])
def manual_reading(payload: ReadingIn):
    try:
        reading, _ = db.add_reading(payload.room_id, payload.value, "manual", "Администратор")
    except ValueError as e:
        raise HTTPException(422, str(e))
    return reading

@app.get("/api/readings", dependencies=[Depends(require_admin)])
def readings(room_id: int | None = None, limit: int = Query(200, ge=1, le=1000)):
    return db.recent_readings(room_id=room_id, limit=limit)

def clean_cell(value) -> str:
    value = "" if value is None else str(value)
    if value.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + value
    return value

@app.get("/api/export.csv", dependencies=[Depends(require_admin)])
def export_csv(room_id: int | None = None):
    output = io.StringIO()
    writer = csv.writer(output, delimiter=";")
    writer.writerow(("Дата UTC", "Корпус", "Помещение", "Температура °C",
                     "Источник", "Отправитель"))
    for row in db.recent_readings(room_id=room_id, limit=50000):
        writer.writerow([clean_cell(row["observed_at"]), clean_cell(row["building"]),
                         clean_cell(row["room_name"]), row["value"], row["source"],
                         clean_cell(row["reporter"])])
    return Response(content="\ufeff" + output.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="termokontur.csv"'})

@app.post("/api/ingest")
def ingest(payload: IngestIn, x_api_key: str | None = Header(default=None)):
    expected = settings.get("ingest_key", "") or ""
    if len(expected) < 20 or expected.startswith("REPLACE_WITH_"):
        raise HTTPException(503, "Приём от датчиков не настроен")
    if not x_api_key or not hmac.compare_digest(x_api_key, expected):
        raise HTTPException(401, "Неверный ключ")
    identifier = None
    if payload.event_id:
        # Event IDs are only unique within one sensor; namespace by room+device.
        identity = f"{payload.room_id}\\0{payload.device}\\0{payload.event_id}"
        identifier = "ha:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()
    try:
        reading, inserted = db.add_reading(payload.room_id, payload.value,
                                           "ha", payload.device, identifier)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return {"reading": reading, "inserted": inserted}
