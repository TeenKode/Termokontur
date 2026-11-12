"""Authenticated web dashboard and machine-ingest API."""
from __future__ import annotations

import csv
import hmac
import io
import os
import secrets
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import auth, db, daily, settings, updates

ROOT = Path(__file__).resolve().parent
app = FastAPI(title="Termokontur", version="0.1.0", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
FAILED_LOGINS: dict[str, list[float]] = {}

@app.on_event("startup")
def startup():
    if len(os.environ.get("TERMOKONTUR_SECRET", "")) < 32 or os.environ["TERMOKONTUR_SECRET"].startswith("REPLACE_WITH_"):
        raise RuntimeError("Set a unique TERMOKONTUR_SECRET (at least 32 characters)")
    db.initialize()
    auth.bootstrap()
    settings.bootstrap()

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
    if request.url.path.startswith("/api/"):
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


class SettingsIn(BaseModel):
    organization_name: str = Field(min_length=1, max_length=100)
    timezone: str = Field(min_length=1, max_length=64)
    stale_hours: int = Field(ge=1, le=168)
    reminders_enabled: bool
    reminder_time: str
    reminder_days: list[int] = Field(min_length=1, max_length=7)
    reminder_window_minutes: int = Field(ge=1, le=240)


class PasswordIn(BaseModel):
    current_password: str
    new_password: str = Field(min_length=12, max_length=256)


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
    return FileResponse(ROOT / "static" / "index.html")

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
    if payload.value is not None and payload.value.strip():
        try:
            settings.set_secret("max_token", payload.value.strip())
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    else:
        settings.set_secret("max_token", None)
    return {"configured": bool(settings.get("max_token"))}


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


@app.get("/api/dashboard", dependencies=[Depends(require_admin)])
def dashboard():
    return db.dashboard(int(settings.get("stale_hours", "26")))

@app.get("/api/daily", dependencies=[Depends(require_admin)])
def daily_report():
    return daily.summary()

@app.get("/api/rooms", dependencies=[Depends(require_admin)])
def rooms(include_inactive: bool = False):
    return db.room_list(active_only=not include_inactive)

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
    identifier = "ha:" + payload.event_id if payload.event_id else None
    try:
        reading, inserted = db.add_reading(payload.room_id, payload.value,
                                           "ha", payload.device, identifier)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return {"reading": reading, "inserted": inserted}
