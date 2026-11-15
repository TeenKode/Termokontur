"""USB-assisted approval, signed device authentication, and common factory images."""
from __future__ import annotations

import hashlib
import io
import json
import secrets
import re
import sqlite3
import struct
import time
import zipfile
from pathlib import PurePosixPath

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import APIRouter, Depends, File, Header, HTTPException, Request, UploadFile
from fastapi.responses import Response
from pydantic import Field, model_validator

from . import db, sensors

MAX_KIT = 16 * 1024 * 1024
MAX_FACTORY = 0x20000 + sensors.MAX_FIRMWARE


class Identity(sensors.Strict):
    public_key: str = Field(pattern=r"^04[a-f0-9]{128}$")
    hardware_id: str = Field(pattern=r"^[a-f0-9]{12}$")
    sensor_type: sensors.SensorType = "sht4x"
    firmware: str = Field(pattern=r"^\d{1,3}\.\d{1,3}\.\d{1,3}$")

    @model_validator(mode="after")
    def valid_point(self):
        try:
            public_key(self.public_key)
        except ValueError:
            raise ValueError("Неверный открытый ключ P-256") from None
        return self


class Proof(Identity):
    challenge: str = Field(pattern=r"^[a-f0-9]{64}$")
    signature: str = Field(pattern=r"^[a-f0-9]{16,160}$")


class Onboard(Proof):
    name: str = Field(min_length=1, max_length=100)
    room_id: int = Field(gt=0)
    config: sensors.Config = Field(default_factory=sensors.Config)


class Approval(sensors.Strict):
    name: str = Field(min_length=1, max_length=100)
    room_id: int = Field(gt=0)


def public_key(value):
    return ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), bytes.fromhex(value))


def fingerprint(value):
    return hashlib.sha256(bytes.fromhex(value)).hexdigest()


def message(identity, nonce):
    return "\n".join(("TK-AUTH-1", nonce, fingerprint(identity.public_key),
                      identity.hardware_id, identity.sensor_type, identity.firmware))


def initialize(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS sensor_identities(
      fingerprint TEXT PRIMARY KEY, public_key TEXT NOT NULL UNIQUE,
      hardware_id TEXT NOT NULL, sensor_type TEXT NOT NULL, firmware TEXT NOT NULL,
      state TEXT NOT NULL CHECK(state IN ('pending','approved','rejected')),
      device_id TEXT UNIQUE REFERENCES sensor_devices(id),
      created_at INTEGER NOT NULL, last_seen INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS sensor_auth_challenges(
      nonce TEXT PRIMARY KEY, message TEXT NOT NULL, fingerprint TEXT NOT NULL, expires INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS sensor_auth_limits(
      bucket TEXT PRIMARY KEY, count INTEGER NOT NULL, since INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS sensor_installers(
      id INTEGER PRIMARY KEY, version TEXT NOT NULL UNIQUE, sha256 TEXT NOT NULL,
      source_commit TEXT NOT NULL, image BLOB NOT NULL, created_at TEXT NOT NULL);
    """)


def throttle(conn, request, fp, proof=False):
    now = int(time.time())
    conn.execute("DELETE FROM sensor_auth_limits WHERE since<?", (now-60,))
    ip = request.client.host if request.client else "unknown"
    scope="proof:" if proof else "challenge:"
    for key, maximum in ((scope+"ip:"+ip, 60 if proof else 30), (scope+"key:"+fp, 32 if proof else 8)):
        row = conn.execute("SELECT count FROM sensor_auth_limits WHERE bucket=?", (key,)).fetchone()
        if row and row[0] >= maximum:
            raise HTTPException(429, "Слишком много запросов авторизации; повторите через минуту")
        if not row and conn.execute("SELECT count(*) FROM sensor_auth_limits").fetchone()[0] >= 512:
            raise HTTPException(503, "Очередь авторизации занята")
        conn.execute("INSERT INTO sensor_auth_limits VALUES(?,1,?) ON CONFLICT(bucket) DO UPDATE SET count=count+1", (key, now))


def verify(conn, payload):
    row = conn.execute("SELECT * FROM sensor_auth_challenges WHERE nonce=?", (payload.challenge,)).fetchone()
    if not row or row['expires'] < time.time() or row['message'] != message(payload, payload.challenge):
        raise HTTPException(401, "Запрос авторизации истёк или не соответствует устройству")
    try:
        public_key(payload.public_key).verify(bytes.fromhex(payload.signature), row['message'].encode(), ec.ECDSA(hashes.SHA256()))
    except (InvalidSignature, ValueError):
        raise HTTPException(401, "Неверная подпись устройства") from None
    conn.execute("DELETE FROM sensor_auth_challenges WHERE nonce=?", (payload.challenge,))


def remember(conn, identity, usb=False):
    fp = fingerprint(identity.public_key)
    existing = conn.execute("SELECT * FROM sensor_identities WHERE fingerprint=?", (fp,)).fetchone()
    if existing and existing['hardware_id'] != identity.hardware_id:
        raise HTTPException(409, "Ключ уже связан с другой аппаратной платой")
    if not existing:
        if not usb and conn.execute("SELECT count(*) FROM sensor_identities WHERE state='pending'").fetchone()[0] >= 64:
            raise HTTPException(503, "Список ожидающих устройств заполнен")
        now = int(time.time())
        conn.execute("INSERT INTO sensor_identities VALUES(?,?,?,?,?,'pending',NULL,?,?)",
                     (fp, identity.public_key, identity.hardware_id, identity.sensor_type, identity.firmware, now, now))
    else:
        conn.execute("UPDATE sensor_identities SET last_seen=?,firmware=?,sensor_type=CASE WHEN state='pending' THEN ? ELSE sensor_type END WHERE fingerprint=?",
                     (int(time.time()), identity.firmware, identity.sensor_type, fp))
    return fp


def approve(conn, fp, name, room_id, config=None, sensor_type=None):
    sensors.check_room(conn, room_id)
    identity = conn.execute("SELECT * FROM sensor_identities WHERE fingerprint=?", (fp,)).fetchone()
    if not identity:
        raise HTTPException(404, "Заявка не найдена")
    d = sensors.get_device(conn, identity['device_id']) if identity['device_id'] else None
    if d and d['room_id'] != room_id:
        raise HTTPException(409, "Для переноса зарегистрированного датчика используйте его карточку")
    old = conn.execute("SELECT id,enabled FROM sensor_devices WHERE hardware_id=? AND id!=?", (identity['hardware_id'], d['id'] if d else '')).fetchone()
    if old:
        if old['enabled']:
            raise HTTPException(409, "Эта плата уже зарегистрирована. Для обновления используйте OTA; при замене сначала отключите прежнюю запись")
        conn.execute("UPDATE sensor_devices SET hardware_id=NULL,token_hash=NULL,enrollment_hash=NULL WHERE id=?", (old['id'],))
    if not d:
        device = "tk-" + secrets.token_hex(8)
        conf = config or sensors.Config()
        conf.version = 1
        conn.execute("""INSERT INTO sensor_devices(id,name,room_id,hardware_id,enrollment_until,config,created_at,sensor_type)
                     VALUES(?,?,?,?,0,?,?,?)""", (device, name.strip(), room_id, identity['hardware_id'],
                     conf.model_dump_json(), db.utc_now(), sensor_type or identity['sensor_type']))
    else:
        device = d['id']
        conf = sensors.Config(**json.loads(d['config']))
        if config:
            wanted = config.model_dump(exclude={'version'})
            if wanted != conf.model_dump(exclude={'version'}):
                config.version = conf.version + 1
                conf = config
        conn.execute("UPDATE sensor_devices SET enabled=1,name=?,config=?,sensor_type=?,hardware_id=? WHERE id=?",
                     (name.strip(), conf.model_dump_json(), sensor_type or d['sensor_type'], identity['hardware_id'], device))
    conn.execute("UPDATE sensor_identities SET state='approved',device_id=?,sensor_type=? WHERE fingerprint=?",
                 (device, sensor_type or identity['sensor_type'], fp))
    sensors.event(conn, device, "approved", "Авторизован администратором по открытому ключу " + fp[:16])
    return {'device_id': device, 'fingerprint': fp, 'config': conf.model_dump(), 'status': 'approved'}


def inspect_kit(data):
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = z.namelist()
            if len(names) != len(set(names)) or any(PurePosixPath(n).is_absolute() or '..' in PurePosixPath(n).parts for n in names):
                raise ValueError("Неверные пути или повторяющиеся файлы ZIP")
            if z.getinfo('manifest.json').file_size > 65536 or z.getinfo('factory.bin').file_size > MAX_FACTORY:
                raise ValueError("Слишком большой образ или manifest")
            manifest = json.loads(z.read('manifest.json'))
            image = z.read('factory.bin')
        entry = manifest['files']['factory.bin']
        sha = hashlib.sha256(image).hexdigest()
        if entry['sha256'] != sha or entry['size'] != len(image):
            raise ValueError("SHA-256 или размер factory.bin не совпадает с manifest")
        if not 0x20100 <= len(image) <= MAX_FACTORY or image[0] != 0xe9 or struct.unpack_from('<H', image, 12)[0] != 5:
            raise ValueError("Нужен заводской образ ESP32-C3")
        app = image[0x20000:]
        if app[0] != 0xe9 or struct.unpack_from('<H', app, 12)[0] != 5 or struct.unpack_from('<I', app, 32)[0] != 0xabcd5432:
            raise ValueError("Неверное приложение ESP32-C3")
        version = app[48:80].split(b'\0', 1)[0].decode('ascii')
        if not re.fullmatch(r'\d{1,3}\.\d{1,3}\.\d{1,3}',version) or app[80:112].split(b'\0', 1)[0] != b'termokontur-sensor' or version != manifest['version']:
            raise ValueError("Неверное имя приложения или версия")
        if sensors.version_tuple(version) < (0, 1, 4):
            raise ValueError("Нужна прошивка 0.1.4 или новее с USB-настройкой")
        if image[0x9000:0xf000] != b'\xff' * 0x6000:
            raise ValueError("Общий образ не должен содержать индивидуальные реквизиты NVS")
        actual = {}
        for pos in range(0x8000, 0x9000-31, 32):
            magic, kind, subtype, offset, size, name, flags = struct.unpack_from('<HBBII16sI', image, pos)
            if magic != 0x50aa:
                break
            actual[name.split(b'\0', 1)[0].decode()] = (kind, subtype, offset, size)
        if actual != {'nvs': (1,2,0x9000,0x6000), 'otadata': (1,0,0xf000,0x2000),
                       'phy_init': (1,1,0x11000,0x1000), 'ota_0': (0,16,0x20000,0x1c0000),
                       'ota_1': (0,17,0x1e0000,0x1c0000), 'datalog': (1,64,0x3a0000,0x60000)}:
            raise ValueError("Неизвестная таблица разделов")
        commit = manifest['source_commit']
        if len(commit) != 40 or any(c not in '0123456789abcdef' for c in commit):
            raise ValueError("Неверная исходная ревизия")
        return version, sha, commit, image
    except (ValueError, KeyError, TypeError, IndexError, struct.error, UnicodeError, zipfile.BadZipFile) as exc:
        raise HTTPException(422, "Неверный комплект: " + str(exc)) from None


def router(require_admin):
    r = APIRouter(prefix='/api/sensors')
    admin = [Depends(require_admin)]

    @r.post('/auth/challenge')
    def challenge(payload: Identity, request: Request):
        fp = fingerprint(payload.public_key)
        with db.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            throttle(conn, request, fp)
            conn.execute('DELETE FROM sensor_auth_challenges WHERE expires<?', (int(time.time()),))
            if conn.execute('SELECT count(*) FROM sensor_auth_challenges').fetchone()[0] >= 256:
                raise HTTPException(503, 'Очередь авторизации занята')
            nonce = secrets.token_hex(32)
            text = message(payload, nonce)
            conn.execute('INSERT INTO sensor_auth_challenges VALUES(?,?,?,?)', (nonce,text,fp,int(time.time())+60))
        return {'challenge':nonce, 'message':text, 'expires_in':60}

    @r.post('/auth/prove')
    def prove(payload: Proof, request: Request):
        with db.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            throttle(conn, request, fingerprint(payload.public_key), proof=True)
            verify(conn, payload)
            fp = remember(conn, payload)
            identity = conn.execute('SELECT * FROM sensor_identities WHERE fingerprint=?', (fp,)).fetchone()
            status = identity['state']
            if status != 'approved':
                return {'status':status, 'fingerprint':fp}
            d = sensors.get_device(conn, identity['device_id'])
            if not d['enabled'] or not conn.execute('SELECT 1 FROM rooms WHERE id=? AND active=1',(d['room_id'],)).fetchone():
                raise HTTPException(403, 'Доступ датчика или помещения отключён')
            if payload.sensor_type != d['sensor_type']:
                raise HTTPException(409, 'Тип датчика изменился; настройте его через USB из панели')
            token = secrets.token_hex(32)
            conn.execute('UPDATE sensor_devices SET token_hash=? WHERE id=?', (sensors.digest(token), d['id']))
        return {'status':'approved', 'device_id':d['id'], 'token':token, 'fingerprint':fp}

    @r.post('/onboard', dependencies=admin)
    def onboard(payload: Onboard):
        with db.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            verify(conn, payload)
            fp = remember(conn, payload, usb=True)
            return approve(conn, fp, payload.name, payload.room_id, payload.config, payload.sensor_type)

    @r.get('/claims', dependencies=admin)
    def claims():
        with db.connect() as conn:
            return [dict(row) for row in conn.execute("SELECT i.fingerprint,i.hardware_id,i.sensor_type,i.firmware,i.state,i.created_at,i.last_seen,d.name,d.room_id FROM sensor_identities i LEFT JOIN sensor_devices d ON d.id=i.device_id WHERE i.state!='approved' ORDER BY CASE i.state WHEN 'pending' THEN 0 ELSE 1 END,i.last_seen DESC LIMIT 64")]

    @r.post('/claims/{fp}/approve', dependencies=admin)
    def authorize(fp: str, payload: Approval):
        with db.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            return approve(conn, fp, payload.name, payload.room_id)

    @r.post('/claims/{fp}/reject', dependencies=admin)
    def reject(fp: str):
        with db.connect() as conn:
            updated = conn.execute("UPDATE sensor_identities SET state='rejected' WHERE fingerprint=? AND state!='approved'", (fp,))
            if not updated.rowcount:
                raise HTTPException(409, "Заявка не найдена или датчик уже авторизован")
        return {'status':'rejected'}

    @r.get('/usb/options', dependencies=admin)
    def options():
        return {'config':sensors.Config().model_dump()}

    @r.get('/usb/check/{hardware_id}', dependencies=admin)
    def check_board(hardware_id: str):
        with db.connect() as conn:
            existing = conn.execute('SELECT id FROM sensor_devices WHERE hardware_id=? AND enabled=1', (hardware_id,)).fetchone()
        if existing:
            raise HTTPException(409, 'Эта плата уже зарегистрирована: используйте OTA. Для замены сначала отключите прежнюю запись')
        return {'new_board':True}

    @r.post('/installer', dependencies=admin)
    async def upload(file: UploadFile = File(...)):
        data = await file.read(MAX_KIT+1)
        if len(data)>MAX_KIT:
            raise HTTPException(413, 'Комплект превышает 16 МиБ')
        version, sha, commit, image = inspect_kit(data)
        with db.connect() as conn:
            old = conn.execute('SELECT id,sha256 FROM sensor_installers WHERE version=?', (version,)).fetchone()
            if old:
                if old['sha256'] != sha:
                    raise HTTPException(409, 'Эта версия уже загружена с другим содержимым')
                return {'id':old['id'], 'version':version}
            cur = conn.execute('INSERT INTO sensor_installers(version,sha256,source_commit,image,created_at) VALUES(?,?,?,?,?)',
                               (version,sha,commit,image,db.utc_now()))
        return {'id':cur.lastrowid, 'version':version}

    @r.get('/installer', dependencies=admin)
    def installer():
        with db.connect() as conn:
            row = conn.execute('SELECT id,version,sha256,source_commit,length(image) size FROM sensor_installers ORDER BY id DESC LIMIT 1').fetchone()
        return dict(row) if row else None

    @r.get('/installer/{installer_id}/factory.bin', dependencies=admin)
    def image(installer_id: int):
        with db.connect() as conn:
            row = conn.execute('SELECT image FROM sensor_installers WHERE id=?', (installer_id,)).fetchone()
        if not row:
            raise HTTPException(404, 'Комплект не найден')
        return Response(bytes(row[0]), media_type='application/octet-stream', headers={'Cache-Control':'no-store'})

    return r
