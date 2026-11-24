"""Autonomous sensors: versioned SQLite schema, transactional ingest and admin API."""
from __future__ import annotations

import csv
import io
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import time
from datetime import datetime, timezone
from ipaddress import IPv4Address
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from . import __version__, db, sensor_firmware, sensor_monitor, settings

MODEL = "c3-sht40-v1"
SensorModel = Literal["c3-sht40-v1"]
SensorType = Literal["sht4x", "sht3x", "aht20", "bme280", "bmp280", "dht11", "dht22", "ds18b20"]
# Zero denotes a missing battery reading during USB diagnostics.
BatteryMV = Annotated[int, Field(ge=0, le=0)] | Annotated[int, Field(ge=2000, le=5000)]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Config(Strict):
    version: int = Field(default=1, ge=1, le=2147483647)
    measure_s: int = Field(default=1800, ge=1, le=604800)
    send_s: int = Field(default=86400, ge=1, le=604800)
    cool_s: int = Field(default=1800, ge=0, le=86400)
    wifi_budget_s: int = Field(default=45, ge=1, le=600)
    wifi_min_mv: int = Field(default=0, ge=0, le=4200)
    cutoff_mv: int = Field(default=3300, ge=3000, le=4200)
    ota_min_mv: int = Field(default=3600, ge=0, le=4200)
    tx_qdbm: int = Field(default=34, ge=8, le=78)
    adc_scale_ppm: int = Field(default=2000000, ge=1600000, le=2400000)
    enabled: bool = True

    @model_validator(mode="after")
    def coherent(self):
        # Store effective values understood by both current and older firmware.
        if self.wifi_min_mv:
            self.wifi_min_mv = max(self.wifi_min_mv, self.cutoff_mv)
        self.ota_min_mv = max(self.ota_min_mv, self.cutoff_mv, 3300)
        return self


DEFAULT_SETUP_KEY = "sensor_setup_defaults"


class SetupDefaults(Strict):
    sensor_type: SensorType = "sht4x"
    ssid: str = ""
    password: str | None = Field(default=None, repr=False)
    dns_primary: str = ""
    dns_secondary: str = ""
    config: Config = Field(default_factory=Config)

    @field_validator("ssid")
    @classmethod
    def valid_ssid(cls, value: str) -> str:
        if "\0" in value or len(value.encode("utf-8")) > 32:
            raise ValueError("Название Wi-Fi должно занимать не более 32 байт")
        return value

    @field_validator("dns_primary", "dns_secondary")
    @classmethod
    def valid_dns(cls, value: str) -> str:
        value = value.strip()
        if not value:
            return value
        try:
            address = IPv4Address(value)
            if address.is_unspecified or address.is_loopback or address.packed[0] >= 224:
                raise ValueError()
        except ValueError:
            raise ValueError("DNS должен быть корректным IPv4-адресом") from None
        return str(address)


def setup_defaults(conn: sqlite3.Connection) -> SetupDefaults:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (DEFAULT_SETUP_KEY,)).fetchone()
    return SetupDefaults.model_validate_json(row["value"]) if row else SetupDefaults()


class DeviceIn(Strict):
    name: str = Field(min_length=1, max_length=100)
    room_id: int = Field(gt=0)
    model: SensorModel = MODEL


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


class Record(Strict):
    seq: int = Field(ge=1, le=9007199254740991)
    boot_id: str = Field(pattern=r"^[a-f0-9]{16}$")
    uptime_s: int = Field(ge=0, le=9007199254740991)
    measured_at: int | None = Field(default=None, ge=1700000000, le=4102444800)
    time_quality: Literal["unknown", "sntp", "server", "rtc"]
    temperature: float | None = Field(default=None, ge=-55, le=125, allow_inf_nan=False)
    humidity: float | None = Field(default=None, ge=0, le=100, allow_inf_nan=False)
    battery_mv: BatteryMV
    interval_s: int = Field(ge=1, le=604800)
    error: Literal["", "sensor_crc", "sensor_io"] = ""

    @model_validator(mode="after")
    def coherent(self):
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
    battery_mv: BatteryMV
    config_version: int = Field(ge=0)
    boots: int = Field(ge=0)
    ota_failures: int = Field(default=0, ge=0)
    ota_generation: int = Field(default=0, ge=0)
    error: str = Field(default="", max_length=128)
    device_ack: int | None = Field(default=None, ge=1, le=9007199254740991)
    device_lost_base: int | None = Field(default=None, ge=0, le=9007199254740991)
    replay: bool = False
    journal_end: bool = False

    @model_validator(mode="after")
    def sequence(self):
        if self.device_ack is None:
            if self.replay or self.device_lost_base is not None:
                raise ValueError("Не указан подтверждённый номер журнала")
        elif self.device_ack > self.next_seq or self.device_lost_base is None or self.device_lost_base > self.lost:
            raise ValueError("Некорректные счётчики журнала для восстановления")
        seqs = [r.seq for r in self.records]
        if seqs != sorted(set(seqs)) or any(s >= self.next_seq for s in seqs):
            raise ValueError("Неверная последовательность записей")
        for record in self.records:
            if not record.error:
                if self.sensor_type in ("ds18b20", "bmp280"):
                    if record.humidity is not None:
                        raise ValueError("Этот датчик не измеряет влажность")
                elif record.humidity is None:
                    raise ValueError("Нет показания влажности")
        return self


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


def get_supported_device(conn, device):
    d = get_device(conn, device)
    if d["model"] != MODEL:
        raise HTTPException(410, "Поддержка модели завершена. История показаний доступна в карточке.")
    return d


def authenticate(conn, device, authorization):
    d = get_device(conn, device)
    token = authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else ""
    if not d["token_hash"] or not hmac.compare_digest(d["token_hash"], digest(token)):
        raise HTTPException(401, "Неверный ключ датчика")
    if not d["enabled"] or d["model"] != MODEL:
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
    for key in ("token_hash", "enrollment_hash", "restore_pending"):
        d.pop(key, None)
    d["config"] = json.loads(d["config"])
    if d["model"] != MODEL:
        return {**d, "enabled": 0, "next_sync_at": None, "status": "retired"}
    if d.get("identity_state") in ("pending", "rejected"):
        return {**d, "next_sync_at": None, "status": "rejected" if d["identity_state"] == "rejected" else "authorization_pending"}
    scheduled, deadline = sensor_monitor.upload_times(d)
    d["next_sync_at"] = scheduled if d["enabled"] and d["config"]["enabled"] and d["hardware_id"] else None
    d["status"] = ("disabled" if not d["enabled"] or not d["config"]["enabled"]
                   else "not_enrolled" if not d["hardware_id"]
                   else "stale" if time.time() > deadline else "waiting" if not d["last_seen"]
                   else "low_battery" if sensor_monitor.battery_warning(d) else "recent")
    return d


def router(require_admin):
    r = APIRouter(prefix="/api/sensors", tags=["sensors"])
    admin = [Depends(require_admin)]

    @r.get("/devices", dependencies=admin)
    def devices():
        with db.connect() as conn:
            return [public_device(x) for x in conn.execute("SELECT d.*,r.name room_name,r.building,i.fingerprint identity_fingerprint,i.state identity_state "
                    "FROM sensor_devices d JOIN rooms r ON r.id=d.room_id LEFT JOIN sensor_identities i ON i.device_id=d.id ORDER BY r.building,r.name,d.name")]

    @r.post("/devices", dependencies=admin)
    def create(payload: DeviceIn):
        code = secrets.token_urlsafe(32)
        device = "tk-" + secrets.token_hex(8)
        with db.connect() as conn:
            check_room(conn, payload.room_id)
            defaults = setup_defaults(conn)
            conn.execute("""INSERT INTO sensor_devices(id,name,room_id,enrollment_hash,enrollment_until,config,created_at,model,sensor_type)
                            VALUES(?,?,?,?,?,?,?,?,?)""", (device, payload.name.strip(), payload.room_id, digest(code),
                            int(time.time()) + 7 * 86400, defaults.config.model_dump_json(), db.utc_now(), payload.model, defaults.sensor_type))
            event(conn, device, "created", "Регистрация разрешена администратором")
        return {"device_id": device, "code": code, "expires_in": 7 * 86400}

    @r.patch("/devices/{device}", dependencies=admin)
    def update_device(device: str, payload: DevicePatch):
        with db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = get_supported_device(conn, device)
            check_room(conn, payload.room_id)
            if current["room_id"] != payload.room_id:
                conn.execute("INSERT OR IGNORE INTO sensor_room_history VALUES(?,0,?)", (device, current["room_id"]))
                conn.execute("""INSERT INTO sensor_room_history VALUES(?,?,?)
                    ON CONFLICT(device_id,starts_at) DO UPDATE SET room_id=excluded.room_id""",
                    (device, int(time.time()), payload.room_id))
            conn.execute("UPDATE sensor_devices SET name=?,room_id=?,enabled=? WHERE id=?",
                         (payload.name.strip(), payload.room_id, payload.enabled, device))
            event(conn, device, "updated", "Изменены имя, помещение или доступ")
        return {"ok": True}

    @r.post("/devices/{device}/rekey", dependencies=admin)
    def rekey(device: str):
        code = secrets.token_urlsafe(32)
        with db.connect() as conn:
            get_supported_device(conn, device)
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
            d = get_supported_device(conn, device)
            payload.version = json.loads(d["config"])["version"] + 1
            conn.execute("UPDATE sensor_devices SET config=? WHERE id=?", (payload.model_dump_json(), device))
            event(conn, device, "config", f"Ожидает доставки: версия {payload.version}")
        return payload.model_dump()

    @r.post("/enroll")
    def enroll(payload: Enrollment):
        with db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            d = get_supported_device(conn, payload.device_id)
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
                conn.execute("UPDATE sensor_devices SET hardware_id=?,token_hash=?,sensor_type=? WHERE id=?",
                             (payload.hardware_id, digest(token), payload.sensor_type, payload.device_id))
            except sqlite3.IntegrityError:
                raise HTTPException(409, "Плата уже зарегистрирована") from None
            event(conn, payload.device_id, "enrolled", "Ключ выдан. Код будет использован при первой передаче данных.")
        return {"token": token}

    @r.post("/devices/{device}/sync")
    def sync(device: str, payload: Sync, authorization: str = Header(default="")):
        now = int(time.time())
        retention = int(settings.get("retention_days", "0"))
        cutoff = now - retention * 86400 if retention else 0
        with db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            d = authenticate(conn, device, authorization)
            if payload.sensor_type != d["sensor_type"] or payload.model != d["model"]:
                raise HTTPException(409, "Модель или тип датчика изменился. Зарегистрируйте его заново.")
            if payload.next_seq < d["ack_seq"] or payload.lost < d["lost"]:
                raise HTTPException(409, "Счётчики устройства уменьшились; восстановите журнал")
            ack = d["ack_seq"]
            if payload.device_ack is not None and ack < payload.device_ack and not payload.replay:
                return {"ack_seq": ack, "replay_from": ack, "time": now}
            replay_until = payload.device_ack if payload.replay else None
            lost_confirmed = d["acknowledged_lost"]
            legacy_restore = bool(d["restore_pending"] and payload.device_ack is None)
            initial_ack = ack
            for rec in payload.records:
                if rec.measured_at and rec.measured_at > now + 300:
                    raise HTTPException(422, "Измерение датировано будущим")
                fingerprint = digest(rec.model_dump_json())
                old = conn.execute("""SELECT fingerprint FROM sensor_records WHERE device_id=? AND seq=?
                    UNION ALL SELECT fingerprint FROM sensor_record_receipts WHERE device_id=? AND seq=?""",
                    (device, rec.seq, device, rec.seq)).fetchone()
                if old:
                    if old["fingerprint"] != fingerprint:
                        raise HTTPException(409, "Содержимое существующего seq изменилось")
                    continue
                if rec.seq < ack:
                    raise HTTPException(409, "Неизвестная запись ниже подтверждённого seq")
                gap_from = ack
                if replay_until is not None:
                    if ack < replay_until and rec.seq > ack:
                        gap_from = min(rec.seq, replay_until)
                        event(conn, device, "restore_gap", f"После восстановления в журнале нет записей [{ack},{gap_from})")
                    if rec.seq >= replay_until:
                        lost_confirmed = max(lost_confirmed, payload.device_lost_base)
                if rec.seq > gap_from:
                    gap = rec.seq - gap_from
                    if legacy_restore and ack == initial_ack and gap > payload.lost - lost_confirmed:
                        event(conn, device, "restore_gap", f"Старая прошивка продолжила с {rec.seq}; отсутствуют [{ack},{rec.seq})")
                        lost_confirmed = payload.lost
                    elif gap > payload.lost - lost_confirmed:
                        raise HTTPException(409, "Разрыв seq не подтверждён счётчиком потерь")
                    else:
                        event(conn, device, "gap", f"Нет записей [{gap_from},{rec.seq})")
                        lost_confirmed += gap
                if rec.measured_at is not None and rec.measured_at < cutoff:
                    conn.execute("INSERT INTO sensor_record_receipts VALUES(?,?,?)", (device, rec.seq, fingerprint))
                    ack = rec.seq + 1
                    continue
                room_id = d["room_id"]
                if rec.measured_at is not None:
                    assigned = conn.execute("""SELECT room_id FROM sensor_room_history
                        WHERE device_id=? AND starts_at<=? ORDER BY starts_at DESC LIMIT 1""",
                        (device, rec.measured_at)).fetchone()
                    if assigned:
                        room_id = assigned["room_id"]
                conn.execute("""INSERT INTO sensor_records VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                             (device, rec.seq, room_id, rec.measured_at, now, rec.boot_id, rec.uptime_s,
                              rec.time_quality, rec.temperature, rec.humidity, rec.battery_mv, rec.interval_s,
                              rec.error, fingerprint))
                if rec.measured_at and not rec.error and -40 <= rec.temperature <= 60:
                    observed = datetime.fromtimestamp(rec.measured_at, timezone.utc).isoformat(timespec="seconds")
                    conn.execute("INSERT INTO readings(room_id,value,source,reporter,observed_at,external_id) "
                                 "VALUES(?,?,'ha',?,?,?)", (room_id, rec.temperature, "Датчик "+device,
                                                        observed, f"sensor:{device}:{rec.seq}"))
                ack = rec.seq + 1
            if replay_until is not None and not payload.records and ack < replay_until:
                event(conn, device, "restore_gap", f"После восстановления в журнале нет записей [{ack},{replay_until})")
                ack = replay_until
            if payload.journal_end and replay_until is not None and ack < replay_until:
                event(conn, device, "restore_gap", f"После восстановления в журнале нет записей [{ack},{replay_until})")
                ack = replay_until
            if replay_until is not None and ack >= replay_until:
                lost_confirmed = max(lost_confirmed, payload.device_lost_base)
            if payload.journal_end and ack < payload.next_seq:
                gap = payload.next_seq - ack
                if gap > payload.lost - lost_confirmed:
                    raise HTTPException(409, "Конец журнала не подтверждён счётчиком потерь")
                event(conn, device, "gap", f"Нет записей [{ack},{payload.next_seq})")
                lost_confirmed += gap
                ack = payload.next_seq
            restore_pending = int(bool(d["restore_pending"] and ack == initial_ack))
            conn.execute("""UPDATE sensor_devices SET last_seen=?,firmware=?,battery_mv=?,rssi=?,boots=?,lost=?,
                         ack_seq=?,config_version=?,acknowledged_lost=?,ota_failures=?,error=?,
                         restore_pending=?,last_complete_sync=CASE WHEN ? THEN ? ELSE last_complete_sync END,
                         enrollment_hash=NULL WHERE id=?""",
                         (now, payload.firmware, payload.battery_mv, payload.rssi, payload.boots, payload.lost,
                          ack, payload.config_version, lost_confirmed, payload.ota_failures, payload.error,
                          restore_pending, ack == payload.next_seq, now, device))
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
    def records(device: str, limit: int = Query(500, ge=1, le=5000),
                before_seq: int | None = Query(default=None, ge=1, le=9007199254740991)):
        with db.connect() as conn:
            get_device(conn, device)
            return [dict(x) for x in conn.execute("""SELECT * FROM sensor_records
                WHERE device_id=? AND (? IS NULL OR seq<?) ORDER BY seq DESC LIMIT ?""",
                (device, before_seq, before_seq, limit))]

    @r.get("/devices/{device}/records.csv", dependencies=admin)
    def export_records(device: str):
        with db.connect() as conn:
            get_device(conn, device)
        fields = ("seq", "measured_at", "received_at", "time_quality", "temperature",
                  "humidity", "battery_mv", "error")
        def rows():
            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow(fields)
            yield "\ufeff" + output.getvalue()
            output.seek(0)
            output.truncate()
            with db.connect() as conn:
                cursor = conn.execute("""SELECT seq,measured_at,received_at,time_quality,
                    temperature,humidity,battery_mv,error FROM sensor_records
                    WHERE device_id=? ORDER BY seq DESC""", (device,))
                while batch := cursor.fetchmany(500):
                    writer.writerows(batch)
                    yield output.getvalue()
                    output.seek(0)
                    output.truncate()
        return StreamingResponse(rows(), media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{device}.csv"',
                     "Cache-Control": "no-store"})

    @r.get("/devices/{device}/events", dependencies=admin)
    def events(device: str):
        with db.connect() as conn:
            get_device(conn, device)
            return [dict(x) for x in conn.execute("SELECT * FROM sensor_events WHERE device_id=? ORDER BY id DESC LIMIT 100", (device,))]

    @r.get("/firmware", dependencies=admin)
    def firmwares():
        with db.connect() as conn:
            return [dict(x) for x in conn.execute("SELECT id,version,model,sha256,length(image) size,created_at FROM sensor_firmware ORDER BY id DESC")]

    @r.get("/firmware/bundled", dependencies=admin)
    def bundled_firmware():
        bundle = sensor_firmware.current()
        if bundle is None:
            return None
        return {
            "version": bundle.factory.version,
            "factory": {"sha256": bundle.factory.sha256, "size": len(bundle.factory.image)},
            "esp32": {"sha256": bundle.esp32_sha256, "size": len(bundle.esp32)},
        }

    @r.post("/firmware/bundled/retry", dependencies=admin)
    async def retry_bundle(request: Request):
        lock = request.app.state.firmware_download
        if lock.locked():
            return {"status": "loading"}
        async with lock:
            available = await sensor_firmware.fetch_current()
        if not available:
            raise HTTPException(502, "Сервер не получил комплект. Скачайте ZIP с GitHub и загрузите его в панель.")
        return {"status": "ready"}

    @r.get("/firmware/bundled/{version}/{target}.bin", dependencies=admin)
    def bundled_image(version: str, target: Literal["esp32-c3", "esp32-c3-factory"]):
        bundle = sensor_firmware.current()
        if bundle is None or version != bundle.factory.version:
            raise HTTPException(404, "Прошивка этой версии недоступна")
        image = bundle.factory.image if target == "esp32-c3-factory" else bundle.esp32
        return Response(image, media_type="application/octet-stream", headers={
            "Content-Disposition": f'attachment; filename="termokontur-{target}-{version}.bin"',
        })

    @r.post("/firmware", dependencies=admin)
    async def upload(file: UploadFile = File(...)):
        image = await file.read(sensor_firmware.MAX_FIRMWARE + 1)
        try:
            version = sensor_firmware.app_version(image)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        sha = hashlib.sha256(image).hexdigest()
        if version == __version__:
            bundle = sensor_firmware.current()
            if bundle and bundle.esp32_sha256 != sha:
                raise HTTPException(409, "Этот номер уже использует прошивка из комплекта панели")
        with db.connect() as conn:
            try:
                cur = conn.execute("INSERT INTO sensor_firmware(version,model,sha256,image,created_at) VALUES(?,?,?,?,?)",
                                   (version, MODEL, sha, image, db.utc_now()))
            except sqlite3.IntegrityError:
                raise HTTPException(409, "Эта версия уже загружена") from None
        return {"id": cur.lastrowid, "version": version}

    @r.put("/devices/{device}/ota", dependencies=admin)
    def target(device: str, firmware_id: int | None = Query(default=None, ge=1),
               bundled_version: str | None = Query(default=None, max_length=11)):
        if firmware_id is not None and bundled_version is not None:
            raise HTTPException(422, "Выберите один образ прошивки")
        with db.connect() as conn:
            d = get_supported_device(conn, device)
            if firmware_id is not None or bundled_version is not None:
                if bundled_version is not None:
                    bundle = sensor_firmware.current()
                    if bundle is None or bundle.factory.version != bundled_version:
                        raise HTTPException(404, "Прошивка этой версии недоступна")
                    conn.execute("""
                        INSERT INTO sensor_firmware(version,model,sha256,image,created_at) VALUES(?,?,?,?,?)
                        ON CONFLICT(version) DO NOTHING
                    """, (bundled_version, MODEL, bundle.esp32_sha256, bundle.esp32, db.utc_now()))
                    fw = conn.execute("SELECT id,version,model,sha256 FROM sensor_firmware WHERE version=?",
                                      (bundled_version,)).fetchone()
                    if fw["sha256"] != bundle.esp32_sha256:
                        raise HTTPException(409, "Загружен другой образ с тем же номером версии")
                    firmware_id = fw["id"]
                else:
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
