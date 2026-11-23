"""Room catalog and manual or automated readings."""
from __future__ import annotations

import csv
import hashlib
import hmac
import io
import secrets
import sqlite3

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from pydantic import BaseModel, Field

from .. import db, settings
from .access import require_admin
from .csv_export import clean_cell

router = APIRouter(prefix="/api", dependencies=[Depends(require_admin)])
ingest_router = APIRouter(prefix="/api")


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


class ReadingIn(BaseModel):
    room_id: int = Field(gt=0)
    value: float = Field(ge=-40, le=60, allow_inf_nan=False)


class IngestIn(ReadingIn):
    device: str = Field(default="", max_length=128)
    event_id: str | None = Field(default=None, max_length=128)


@router.get("/rooms")
def rooms(include_inactive: bool = False):
    return db.room_list(active_only=not include_inactive)


@router.get("/rooms/export.csv")
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


@router.post("/rooms", status_code=201)
def create_room(payload: RoomIn):
    data = payload.model_dump()
    data = {k: v.strip() if isinstance(v, str) else v for k, v in data.items()}
    if not data["building"] or not data["name"] or not data["kind"]:
        raise HTTPException(422, "Заполните название, корпус и тип помещения")
    if data["min_temp"] >= data["max_temp"]:
        raise HTTPException(422, "Минимум должен быть ниже максимума")
    try:
        return db.create_room(data["building"], data["floor"], data["name"], data["kind"],
                              data["min_temp"], data["max_temp"])
    except sqlite3.IntegrityError:
        raise HTTPException(409, "Кабинет с таким названием уже есть в этом корпусе")


@router.post("/rooms/bulk")
def bulk_rooms(payload: RoomsBulkIn):
    data = payload.model_dump()
    names = [name.strip() for name in data.pop("names")]
    if any(len(name) > 100 or not name for name in names):
        raise HTTPException(422, "Проверьте названия помещений")
    if data["min_temp"] >= data["max_temp"]:
        raise HTTPException(422, "Минимум должен быть ниже максимума")
    try:
        return db.create_rooms_bulk(data["building"], data["floor"], data["kind"],
                                    data["min_temp"], data["max_temp"], names)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.patch("/rooms/{room_id}")
def patch_room(room_id: int, payload: RoomPatch):
    room = db.room_get(room_id)
    if not room:
        raise HTTPException(404, "Кабинет не найден")
    patch = payload.model_dump(exclude_unset=True)
    if any(value is None for value in patch.values()):
        raise HTTPException(422, "Заполните обязательные поля")
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


@router.post("/readings", status_code=201)
def manual_reading(payload: ReadingIn):
    try:
        reading, _ = db.add_reading(payload.room_id, payload.value, "manual", "Администратор")
    except ValueError as e:
        raise HTTPException(422, str(e))
    return reading


@router.get("/readings")
def readings(room_id: int | None = None, limit: int = Query(200, ge=1, le=1000)):
    return db.recent_readings(room_id=room_id, limit=limit)


@router.get("/readings/{reading_id}/corrections")
def reading_corrections(reading_id: int):
    """Original reading and MAX corrections are visible only to administrators."""
    history = db.reading_corrections(reading_id)
    if history is None:
        raise HTTPException(404, "Измерение не найдено")
    return history


@router.get("/export.csv")
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


@ingest_router.post("/ingest")
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


@router.post("/settings/ingest-key")
def rotate_ingest_key():
    key = secrets.token_urlsafe(36)
    settings.set_secret("ingest_key", key)
    return {"key": key, "warning": "Старый ключ недействителен. Обновите настройки Home Assistant."}
