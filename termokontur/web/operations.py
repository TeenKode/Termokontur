"""Administrator routes for panel settings, HTTPS, updates and backups."""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .. import auth, maintenance, settings, tls, updates
from .access import require_admin

router = APIRouter(prefix="/api", dependencies=[Depends(require_admin)])
logger = logging.getLogger("termokontur.backups")


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


@router.get("/settings")
def get_settings():
    return settings.public()


@router.put("/settings")
def save_settings(payload: SettingsIn):
    try:
        data = settings.validated(payload.model_dump())
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    settings.update(data)
    return settings.public()


@router.get("/tls/status")
def get_tls_status():
    return tls.state()


async def _limited_tls_file(file: UploadFile | None) -> bytes | None:
    if file is None or not file.filename:
        return None
    raw = await file.read(tls.MAX_UPLOAD + 1)
    if len(raw) > tls.MAX_UPLOAD:
        raise HTTPException(413, "Максимум 256 КБ на файл.")
    return raw


@router.post("/tls/certificate")
async def upload_tls_certificate(
    certificate: UploadFile | None = File(default=None),
    key: UploadFile | None = File(default=None),
    password: str = Form(default=""),
):
    try:
        return tls.upload(
            await _limited_tls_file(certificate),
            await _limited_tls_file(key),
            password,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.delete("/tls/certificate")
def remove_tls_certificate():
    return tls.remove()


@router.post("/tls/apply")
async def apply_tls_settings():
    state = tls.state()
    if not state["managed"]:
        raise HTTPException(409, "Запустите панель через python -m termokontur.server.")
    if state["error"]:
        raise HTTPException(422, state["error"])
    if not state["restart_pending"]:
        return {"ok": True, "restarting": False}
    # The HTTP response is flushed before replacing this managed process.
    # No user-controlled command or argument reaches execv.
    loop = asyncio.get_running_loop()
    loop.call_later(1.0, os.execv, sys.executable, [sys.executable, "-m", "termokontur.server"])
    return {"ok": True, "restarting": True, "https": state["configured"]}


@router.get("/update/status")
def update_status():
    return updates.status()


@router.get("/update/check")
def check_update():
    return updates.check()


@router.post("/update/request")
def request_update():
    try:
        return updates.request()
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/backups")
def backups():
    return maintenance.list_backups()


@router.post("/backups")
def backup_now():
    return maintenance.create_backup()


@router.get("/backups/{name}")
def download_backup(name: str):
    try:
        path = maintenance.backup_path(name)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    return FileResponse(path, filename=name, media_type="application/octet-stream",
                        headers={"Cache-Control":"no-store"})


@router.delete("/backups/{name}")
def remove_backup(name: str):
    try:
        maintenance.delete_backup(name)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"ok": True}


@router.post("/backups/restore")
def restore_backup(payload: RestoreBackupIn, response: Response):
    if not auth.check_password(payload.password):
        raise HTTPException(403, "Неверный пароль администратора")
    try:
        result = maintenance.restore(payload.name)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        logger.exception("Restore failed")
        raise HTTPException(500, "Не удалось восстановить резервную копию") from exc
    response.delete_cookie("tk_session", path="/", samesite="strict")
    return result


@router.post("/backups/import")
async def import_backup(request: Request):
    length = request.headers.get("content-length")
    limit = maintenance.MAX_IMPORT_BYTES
    too_large = f"Максимальный размер загружаемой копии — {limit:,} байт".replace(",", " ")
    if length is not None and (not length.isdigit() or len(length) > 20 or int(length) > limit):
        raise HTTPException(413, too_large)
    destination = maintenance.backup_dir()
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", prefix=".incoming-", dir=destination,
                                         delete=False) as tmp:
            temp_path = Path(tmp.name)
            size = 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > limit:
                    raise HTTPException(413, too_large)
                tmp.write(chunk)
        temp_path.chmod(0o600)
        await asyncio.to_thread(maintenance.inspect, temp_path)
        name = maintenance._filename("import")
        temp_path.rename(destination / name)
        return {"name": name, "size": size}
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    finally:
        if temp_path and temp_path.exists():
            temp_path.unlink()


@router.post("/maintenance/prune")
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
