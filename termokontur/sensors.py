"""Autonomous sensors: versioned SQLite schema, transactional ingest and admin API."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import struct
import time
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, File, Header, HTTPException, Query, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import db

MODEL = "c3-sht40-v1"
SensorModel = Literal["c3-sht40-v1", "esp01-sht40-v1"]
SensorType = Literal["sht4x", "sht3x", "aht20", "bme280", "dht11", "dht22", "ds18b20"]
MAX_FIRMWARE = 0x1C0000


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Config(Strict):
    version: int = Field(default=1, ge=1, le=2147483647)
    measure_s: int = Field(default=1800, ge=60, le=86400)
    send_s: int = Field(default=86400, ge=600, le=604800)
    cool_s: int = Field(default=1800, ge=300, le=86400)
    wifi_budget_s: int = Field(default=45, ge=15, le=120)
    wifi_min_mv: int = Field(default=3600, ge=3300, le=4100)
    cutoff_mv: int = Field(default=3300, ge=3100, le=3800)
    ota_min_mv: int = Field(default=3900, ge=3600, le=4200)
    tx_qdbm: int = Field(default=34, ge=8, le=78)
    adc_scale_ppm: int = Field(default=2000000, ge=1600000, le=2400000)
    enabled: bool = True

    @model_validator(mode="after")
    def coherent(self):
        if not self.cutoff_mv <= self.wifi_min_mv <= self.ota_min_mv:
            raise ValueError("Пороги напряжения: отключение ≤ Wi-Fi ≤ обновление")
        if self.send_s < self.measure_s:
            raise ValueError("Интервал передачи должен быть не меньше интервала измерений")
        return self


class DeviceIn(Strict):
    name: str = Field(min_length=1, max_length=100)
    room_id: int = Field(gt=0)


class DevicePatch(Strict):
    name: str = Field(min_length=1, max_length=100)
    room_id: int = Field(gt=0)
    enabled: bool


class Enrollment(Strict):
    device_id: str = Field(pattern=r"^tk-[a-f0-9]{16}$")
    code: str = Field(min_length=32, max_length=100)
    hardware_id: str = Field(pattern=r"^[a-f0-9]{12}$")
    model: SensorModel
    sensor_type: SensorType = "sht4x"


def valid_battery_mv(mv: int) -> bool:
    """0 is an explicit 'not measurable' value for ESP-01 without an ADC."""
    return mv == 0 or 2000 <= mv <= 5000


class Record(Strict):
    seq: int = Field(ge=1, le=9007199254740991)
    boot_id: str = Field(pattern=r"^[a-f0-9]{16}$")
    uptime_s: int = Field(ge=0, le=9007199254740991)
    measured_at: int | None = Field(default=None, ge=1700000000, le=4102444800)
    time_quality: Literal["unknown", "sntp", "server", "rtc"]
    temperature: float | None = Field(default=None, ge=-55, le=125, allow_inf_nan=False)
    humidity: float | None = Field(default=None, ge=0, le=100, allow_inf_nan=False)
    battery_mv: int = Field(ge=0, le=5000)
    interval_s: int = Field(ge=60, le=86400)
    error: Literal["", "sensor_crc", "sensor_io"] = ""

    @model_validator(mode="after")
    def coherent(self):
        if not valid_battery_mv(self.battery_mv):
            raise ValueError("Недопустимое напряжение аккумулятора")
        if (self.time_quality == "unknown") != (self.measured_at is None):
            raise ValueError("Качество времени не соответствует временной метке")
        if not self.error and self.temperature is None:
            raise ValueError("Нет измерения или описания ошибки")
        if self.error and (self.temperature is not None or self.humidity is not None):
            raise ValueError("Ошибочное измерение не должно содержать значения")
        return self


class Sync(Strict):
    model: SensorModel
    sensor_type: SensorType = "sht4x"
    firmware: str = Field(pattern=r"^\d{1,3}\.\d{1,3}\.\d{1,3}$")
    records: list[Record] = Field(max_length=100)
    next_seq: int = Field(ge=1, le=9007199254740991)
    lost: int = Field(ge=0, le=9007199254740991)
    rssi: int = Field(ge=-127, le=0)
    battery_mv: int = Field(ge=0, le=5000)
    config_version: int = Field(ge=0)
    boots: int = Field(ge=0)
    ota_failures: int = Field(default=0, ge=0)
    ota_generation: int = Field(default=0, ge=0)
    error: str = Field(default="", max_length=128)

    @model_validator(mode="after")
    def sequence(self):
        if not valid_battery_mv(self.battery_mv):
            raise ValueError("Недопустимое напряжение аккумулятора")
        seqs = [r.seq for r in self.records]
        if seqs != sorted(set(seqs)) or any(s >= self.next_seq for s in seqs):
            raise ValueError("Неверная последовательность записей")
        for record in self.records:
            if not record.error:
                if self.sensor_type == "ds18b20":
                    if record.humidity is not None:
                        raise ValueError("DS18B20 не измеряет влажность")
                elif record.humidity is None:
                    raise ValueError("Нет показания влажности")
        return self


def initialize(conn):
    # Additive migration: no rebuild of readings or changes to MAX/manual data.
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS sensor_migrations(version INTEGER PRIMARY KEY);
    CREATE TABLE IF NOT EXISTS sensor_firmware(
      id INTEGER PRIMARY KEY, version TEXT NOT NULL UNIQUE, model TEXT NOT NULL,
      sha256 TEXT NOT NULL, image BLOB NOT NULL, created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS sensor_devices(
      id TEXT PRIMARY KEY, name TEXT NOT NULL, room_id INTEGER NOT NULL REFERENCES rooms(id),
      enabled INTEGER NOT NULL DEFAULT 1, enrollment_hash TEXT, enrollment_until INTEGER NOT NULL,
      hardware_id TEXT UNIQUE, token_hash TEXT, config TEXT NOT NULL,
      created_at TEXT NOT NULL, last_seen INTEGER, firmware TEXT NOT NULL DEFAULT '',
      battery_mv INTEGER, rssi INTEGER, boots INTEGER NOT NULL DEFAULT 0,
      lost INTEGER NOT NULL DEFAULT 0, ack_seq INTEGER NOT NULL DEFAULT 1,
      config_version INTEGER NOT NULL DEFAULT 0, acknowledged_lost INTEGER NOT NULL DEFAULT 0, ota_failures INTEGER NOT NULL DEFAULT 0,
      target_firmware INTEGER REFERENCES sensor_firmware(id), ota_generation INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '');
    CREATE TABLE IF NOT EXISTS sensor_records(
      device_id TEXT NOT NULL REFERENCES sensor_devices(id), seq INTEGER NOT NULL,
      room_id INTEGER NOT NULL REFERENCES rooms(id), measured_at INTEGER, received_at INTEGER NOT NULL,
      boot_id TEXT NOT NULL, uptime_s INTEGER NOT NULL, time_quality TEXT NOT NULL,
      temperature REAL, humidity REAL, battery_mv INTEGER NOT NULL, interval_s INTEGER NOT NULL,
      error TEXT NOT NULL, fingerprint TEXT NOT NULL,
      PRIMARY KEY(device_id,seq));
    CREATE INDEX IF NOT EXISTS sensor_records_time ON sensor_records(device_id,measured_at);
    CREATE TABLE IF NOT EXISTS sensor_events(
      id INTEGER PRIMARY KEY, device_id TEXT NOT NULL REFERENCES sensor_devices(id),
      at INTEGER NOT NULL, kind TEXT NOT NULL, detail TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS sensor_mqtt_outbox(
      device_id TEXT PRIMARY KEY REFERENCES sensor_devices(id), generation INTEGER NOT NULL DEFAULT 1,
      attempts INTEGER NOT NULL DEFAULT 0, next_retry INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '');
    CREATE TABLE IF NOT EXISTS sensor_mqtt_settings(id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL);
    INSERT OR IGNORE INTO sensor_migrations VALUES(1);
    """)
    # Web and MAX services can start together; serialize the additive migration.
    conn.execute("BEGIN IMMEDIATE")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(sensor_devices)")}
    if "sensor_type" not in columns:
        conn.execute("ALTER TABLE sensor_devices ADD COLUMN sensor_type TEXT NOT NULL DEFAULT 'sht4x'")
    if "model" not in columns:
        conn.execute("ALTER TABLE sensor_devices ADD COLUMN model TEXT NOT NULL DEFAULT 'c3-sht40-v1'")
    conn.execute("INSERT OR IGNORE INTO sensor_migrations VALUES(2)")
    from . import sensor_onboarding
    sensor_onboarding.initialize(conn)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def event(conn, device, kind, detail):
    conn.execute("INSERT INTO sensor_events(device_id,at,kind,detail) VALUES(?,?,?,?)",
                 (device, int(time.time()), kind, detail[:512]))
    # Bound diagnostics per device; measurements have their own retention policy.
    conn.execute("DELETE FROM sensor_events WHERE device_id=? AND id NOT IN "
                 "(SELECT id FROM sensor_events WHERE device_id=? ORDER BY id DESC LIMIT 200)", (device, device))


def get_device(conn, device):
    row = conn.execute("SELECT * FROM sensor_devices WHERE id=?", (device,)).fetchone()
    if row is None:
        raise HTTPException(404, "Датчик не найден")
    return dict(row)


def authenticate(conn, device, authorization):
    d = get_device(conn, device)
    token = authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else ""
    if not d["token_hash"] or not hmac.compare_digest(d["token_hash"], digest(token)):
        raise HTTPException(401, "Неверный ключ датчика")
    if not d["enabled"]:
        raise HTTPException(403, "Датчик отключён администратором")
    room = conn.execute("SELECT active FROM rooms WHERE id=?", (d["room_id"],)).fetchone()
    if not room or not room["active"]:
        raise HTTPException(403, "Помещение отключено")
    return d


def check_room(conn, room):
    if not conn.execute("SELECT 1 FROM rooms WHERE id=? AND active=1", (room,)).fetchone():
        raise HTTPException(422, "Выберите активное помещение")


def version_tuple(v):
    return tuple(map(int, v.split(".")))


def public_device(d):
    d = dict(d)
    for key in ("token_hash", "enrollment_hash"):
        d.pop(key, None)
    d["config"] = json.loads(d["config"])
    # Daily reporting cannot establish that a sleeping device is currently online.
    stale = max(3600, d["config"]["send_s"] * 2 + d["config"]["cool_s"])
    d["status"] = ("disabled" if not d["enabled"] else "not_enrolled" if not d["hardware_id"]
                   else "waiting" if not d["last_seen"] else "stale" if time.time() - d["last_seen"] > stale
                   else "low_battery" if d["battery_mv"] not in (None, 0) and d["battery_mv"] < d["config"]["wifi_min_mv"] else "recent")
    return d


def router(require_admin):
    r = APIRouter(prefix="/api/sensors", tags=["sensors"])
    admin = [Depends(require_admin)]

    @r.get("/devices", dependencies=admin)
    def devices():
        with db.connect() as conn:
            return [public_device(x) for x in conn.execute("SELECT d.*,r.name room_name,r.building,i.fingerprint identity_fingerprint "
                    "FROM sensor_devices d JOIN rooms r ON r.id=d.room_id LEFT JOIN sensor_identities i ON i.device_id=d.id ORDER BY r.building,r.name,d.name")]

    @r.post("/devices", dependencies=admin)
    def create(payload: DeviceIn):
        code = secrets.token_urlsafe(32)
        device = "tk-" + secrets.token_hex(8)
        with db.connect() as conn:
            check_room(conn, payload.room_id)
            conn.execute("""INSERT INTO sensor_devices(id,name,room_id,enrollment_hash,enrollment_until,config,created_at)
                            VALUES(?,?,?,?,?,?,?)""", (device, payload.name.strip(), payload.room_id, digest(code),
                            int(time.time()) + 7 * 86400, Config().model_dump_json(), db.utc_now()))
            event(conn, device, "created", "Регистрация разрешена администратором")
        return {"device_id": device, "code": code, "expires_in": 7 * 86400}

    @r.patch("/devices/{device}", dependencies=admin)
    def update_device(device: str, payload: DevicePatch):
        with db.connect() as conn:
            get_device(conn, device)
            check_room(conn, payload.room_id)
            conn.execute("UPDATE sensor_devices SET name=?,room_id=?,enabled=? WHERE id=?",
                         (payload.name.strip(), payload.room_id, payload.enabled, device))
            event(conn, device, "updated", "Изменены имя, помещение или доступ")
        return {"ok": True}

    @r.post("/devices/{device}/rekey", dependencies=admin)
    def rekey(device: str):
        code = secrets.token_urlsafe(32)
        with db.connect() as conn:
            get_device(conn, device)
            identity = conn.execute("SELECT fingerprint FROM sensor_identities WHERE device_id=?", (device,)).fetchone()
            if identity:
                conn.execute("UPDATE sensor_devices SET token_hash=NULL,enrollment_hash=NULL,enrollment_until=0 WHERE id=?", (device,))
                conn.execute("UPDATE sensor_identities SET state='pending' WHERE device_id=?", (device,))
                event(conn, device, "rekey", "Доступ отозван. Требуется новое разрешение.")
                return {"device_id":device,"requires_authorization":True}
            conn.execute("UPDATE sensor_devices SET enrollment_hash=?,enrollment_until=?,token_hash=NULL WHERE id=?",
                         (digest(code), int(time.time())+7*86400, device))
            conn.execute("UPDATE sensor_identities SET state='pending' WHERE device_id=?", (device,))
            event(conn, device, "rekey", "Ключ отозван. Настройте датчик заново.")
        return {"device_id": device, "code": code, "expires_in": 7 * 86400}

    @r.put("/devices/{device}/config", dependencies=admin)
    def configure(device: str, payload: Config):
        with db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            d = get_device(conn, device)
            payload.version = json.loads(d["config"])["version"] + 1
            conn.execute("UPDATE sensor_devices SET config=? WHERE id=?", (payload.model_dump_json(), device))
            event(conn, device, "config", f"Ожидает доставки: версия {payload.version}")
        return payload.model_dump()

    @r.post("/enroll")
    def enroll(payload: Enrollment):
        with db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            d = get_device(conn, payload.device_id)
            if (not d["enabled"] or not d["enrollment_hash"] or d["enrollment_until"] < time.time()
                    or not hmac.compare_digest(d["enrollment_hash"], digest(payload.code))):
                raise HTTPException(403, "Код недействителен; обратитесь к администратору")
            if not conn.execute("SELECT 1 FROM rooms WHERE id=? AND active=1", (d["room_id"],)).fetchone():
                raise HTTPException(403, "Помещение отключено")
            if d["hardware_id"] and d["hardware_id"] != payload.hardware_id:
                raise HTTPException(409, "Код уже привязан к другой плате")
            # Deterministic per-code credential lets a lost enrollment response be retried.
            token = hmac.new(payload.code.encode(), (payload.device_id+":"+payload.hardware_id).encode(), "sha256").hexdigest()
            previous = conn.execute("SELECT id,enabled FROM sensor_devices WHERE hardware_id=? AND id<>?",
                                    (payload.hardware_id, payload.device_id)).fetchone()
            if previous and not previous["enabled"]:
                conn.execute("UPDATE sensor_devices SET hardware_id=NULL,token_hash=NULL,enrollment_hash=NULL WHERE id=?",
                             (previous["id"],))
                event(conn, previous["id"], "retired", "Плата перенесена в новую регистрацию; архив сохранён")
            try:
                conn.execute("UPDATE sensor_devices SET hardware_id=?,token_hash=?,sensor_type=?,model=?,target_firmware=NULL WHERE id=?",
                             (payload.hardware_id, digest(token), payload.sensor_type, payload.model, payload.device_id))
            except sqlite3.IntegrityError:
                raise HTTPException(409, "Плата уже зарегистрирована") from None
            event(conn, payload.device_id, "enrolled", "Ключ выдан. Код будет использован при первой передаче данных.")
        return {"token": token}

    @r.post("/devices/{device}/sync")
    def sync(device: str, payload: Sync, authorization: str = Header(default="")):
        now = int(time.time())
        with db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            d = authenticate(conn, device, authorization)
            if payload.sensor_type != d["sensor_type"] or payload.model != d["model"]:
                raise HTTPException(409, "Модель или тип датчика изменился. Зарегистрируйте его заново.")
            if payload.next_seq < d["ack_seq"] or payload.lost < d["lost"]:
                raise HTTPException(409, "Счётчики устройства уменьшились; восстановите журнал")
            ack = d["ack_seq"]
            for rec in payload.records:
                if rec.measured_at and rec.measured_at > now + 300:
                    raise HTTPException(422, "Измерение датировано будущим")
                fingerprint = digest(rec.model_dump_json())
                old = conn.execute("SELECT fingerprint FROM sensor_records WHERE device_id=? AND seq=?",
                                   (device, rec.seq)).fetchone()
                if old:
                    if old["fingerprint"] != fingerprint:
                        raise HTTPException(409, "Содержимое существующего seq изменилось")
                    continue
                if rec.seq < ack:
                    raise HTTPException(409, "Неизвестная запись ниже подтверждённого seq")
                if rec.seq > ack:
                    gap = rec.seq - ack
                    if gap > payload.lost - d["acknowledged_lost"]:
                        raise HTTPException(409, "Разрыв seq не подтверждён счётчиком потерь")
                    event(conn, device, "gap", f"Нет записей [{ack},{rec.seq})")
                    d["acknowledged_lost"] += gap
                conn.execute("""INSERT INTO sensor_records VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                             (device, rec.seq, d["room_id"], rec.measured_at, now, rec.boot_id, rec.uptime_s,
                              rec.time_quality, rec.temperature, rec.humidity, rec.battery_mv, rec.interval_s,
                              rec.error, fingerprint))
                if rec.measured_at and not rec.error and -40 <= rec.temperature <= 60:
                    observed = datetime.fromtimestamp(rec.measured_at, timezone.utc).isoformat(timespec="seconds")
                    conn.execute("INSERT INTO readings(room_id,value,source,reporter,observed_at,external_id) "
                                 "VALUES(?,?,'ha',?,?,?)", (d["room_id"], rec.temperature, "Датчик "+device,
                                                        observed, f"sensor:{device}:{rec.seq}"))
                ack = rec.seq + 1
            # No records is not permission to acknowledge data that was never received.
            conn.execute("""UPDATE sensor_devices SET last_seen=?,firmware=?,battery_mv=?,rssi=?,boots=?,lost=?,
                         ack_seq=?,config_version=?,acknowledged_lost=?,ota_failures=?,error=?,enrollment_hash=NULL WHERE id=?""",
                         (now, payload.firmware, payload.battery_mv, payload.rssi, payload.boots, payload.lost,
                          ack, payload.config_version, d["acknowledged_lost"], payload.ota_failures, payload.error, device))
            if payload.error:
                event(conn, device, "device_error", payload.error)
            conn.execute("""INSERT INTO sensor_mqtt_outbox(device_id) VALUES(?) ON CONFLICT(device_id)
                         DO UPDATE SET generation=generation+1,next_retry=0""", (device,))
            firmware = conn.execute("SELECT id,version,sha256,length(image) size FROM sensor_firmware WHERE id=? AND model=?",
                                    (d["target_firmware"], d["model"])).fetchone()
            ota = (dict(firmware) if firmware and (payload.ota_generation != d["ota_generation"] or payload.ota_failures < 3)
                   and version_tuple(firmware["version"]) > version_tuple(payload.firmware) else None)
            if ota:
                ota["generation"] = d["ota_generation"]
            config = json.loads(d["config"])
        # The context manager has committed before an ACK leaves the server.
        return {"ack_seq": ack, "time": now, "config": config, "ota": ota}

    @r.get("/devices/{device}/records", dependencies=admin)
    def records(device: str, limit: int = Query(500, ge=1, le=5000)):
        with db.connect() as conn:
            get_device(conn, device)
            return [dict(x) for x in conn.execute("SELECT * FROM sensor_records WHERE device_id=? ORDER BY seq DESC LIMIT ?",
                                                  (device, limit))]

    @r.get("/devices/{device}/events", dependencies=admin)
    def events(device: str):
        with db.connect() as conn:
            get_device(conn, device)
            return [dict(x) for x in conn.execute("SELECT * FROM sensor_events WHERE device_id=? ORDER BY id DESC LIMIT 100", (device,))]

    @r.get("/firmware", dependencies=admin)
    def firmwares():
        with db.connect() as conn:
            return [dict(x) for x in conn.execute("SELECT id,version,model,sha256,length(image) size,created_at FROM sensor_firmware ORDER BY id DESC")]

    @r.post("/firmware", dependencies=admin)
    async def upload(file: UploadFile = File(...)):
        image = await file.read(MAX_FIRMWARE + 1)
        if len(image) < 256 or len(image) > MAX_FIRMWARE or image[0] != 0xE9:
            raise HTTPException(422, "Неверный формат или размер прошивки")
        if struct.unpack_from("<H", image, 12)[0] != 5 or struct.unpack_from("<I", image, 32)[0] != 0xABCD5432:
            raise HTTPException(422, "Выберите прошивку для ESP32-C3")
        version = image[48:80].split(b"\0", 1)[0].decode("ascii", errors="replace")
        project = image[80:112].split(b"\0", 1)[0]
        if not re.fullmatch(r"\d{1,3}\.\d{1,3}\.\d{1,3}", version) or project != b"termokontur-sensor":
            raise HTTPException(422, "Прошивка не подходит: проверьте проект и версию")
        with db.connect() as conn:
            try:
                cur = conn.execute("INSERT INTO sensor_firmware(version,model,sha256,image,created_at) VALUES(?,?,?,?,?)",
                                   (version, MODEL, hashlib.sha256(image).hexdigest(), image, db.utc_now()))
            except sqlite3.IntegrityError:
                raise HTTPException(409, "Эта версия уже загружена") from None
        return {"id": cur.lastrowid, "version": version}

    @r.put("/devices/{device}/ota", dependencies=admin)
    def target(device: str, firmware_id: int | None = Query(default=None, ge=1)):
        with db.connect() as conn:
            d = get_device(conn, device)
            if firmware_id is not None:
                if d["model"] == "esp01-sht40-v1":
                    raise HTTPException(422, "OTA ESP-01S 1 МБ пока не поддерживается. Используйте USB–UART.")
                fw = conn.execute("SELECT version,model FROM sensor_firmware WHERE id=?", (firmware_id,)).fetchone()
                if not fw or fw["model"] != d["model"] or (d["firmware"] and version_tuple(fw["version"]) <= version_tuple(d["firmware"])):
                    raise HTTPException(422, "Выберите более новую версию")
            conn.execute("UPDATE sensor_devices SET target_firmware=?,ota_failures=0,ota_generation=ota_generation+1 WHERE id=?", (firmware_id, device))
            event(conn, device, "ota", f"Назначен образ {firmware_id}")
        return {"ok": True}

    @r.get("/devices/{device}/firmware/{firmware_id}")
    def download(device: str, firmware_id: int, authorization: str = Header(default="")):
        with db.connect() as conn:
            d = authenticate(conn, device, authorization)
            if d["target_firmware"] != firmware_id:
                raise HTTPException(403, "Образ не назначен устройству")
            fw = conn.execute("SELECT image,model FROM sensor_firmware WHERE id=?", (firmware_id,)).fetchone()
            if not fw or fw["model"] != d["model"]:
                raise HTTPException(404, "Образ не найден")
            return Response(bytes(fw["image"]), media_type="application/octet-stream")

    @r.get("/mqtt", dependencies=admin)
    def mqtt_settings():
        return sensor_mqtt.public()

    @r.put("/mqtt", dependencies=admin)
    def mqtt_configure(payload: sensor_mqtt.MQTTConfig):
        return sensor_mqtt.configure(payload)

    return r


from . import sensor_mqtt
