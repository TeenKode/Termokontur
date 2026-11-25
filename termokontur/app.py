"""Compose the panel's FastAPI application."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Lock

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, auth, audit, db, maintenance, sensor_firmware, sensor_mqtt, sensor_onboarding, sensors, settings
from .web import access, max_api, operations, reporting, rooms

ROOT = Path(__file__).resolve().parent
logger = logging.getLogger("termokontur.maintenance")

# A content fingerprint changes whenever JavaScript OR CSS changes, even between
# deployments using the same semantic version number.
_ASSET_FINGERPRINT = hashlib.sha256(
    b"".join((ROOT / "static" / name).read_bytes() for name in (
        "app.js", "charts.js", "reports.js", "notifications.js", "updates.js", "school-calendar.js", "building-accounts.js", "sensors.js", "sensor-onboarding.js",
        "sensor-usb.mjs", "firmware-debug.mjs", "sensor-log.mjs",
        "vendor/esptool.bundle.js", "style.css", "theme.js", "icon-32.png", "icon-192.png",
    ))
).hexdigest()[:16]


def bootstrap() -> None:
    secret = os.getenv("TERMOKONTUR_SECRET", "")
    if len(secret) < 32 or secret.startswith("REPLACE_WITH_"):
        raise RuntimeError("Set a unique TERMOKONTUR_SECRET (at least 32 characters)")
    db.initialize()
    auth.bootstrap()
    settings.bootstrap()


async def scheduled_maintenance() -> None:
    while True:
        await asyncio.sleep(30)
        try:
            await asyncio.to_thread(maintenance.schedule)
        except Exception:
            logger.exception("Scheduled maintenance failed")
        await asyncio.sleep(3600)


@asynccontextmanager
async def lifespan(application: FastAPI):
    await asyncio.to_thread(bootstrap)
    download_lock = asyncio.Lock()
    application.state.firmware_download = download_lock
    async with asyncio.TaskGroup() as tasks:
        maintenance_task = tasks.create_task(scheduled_maintenance())
        mqtt_task = tasks.create_task(sensor_mqtt.worker())
        firmware_task = None
        if os.getenv("TERMOKONTUR_FETCH_FIRMWARE") == "1":
            firmware_task = tasks.create_task(sensor_firmware.retry_fetch(download_lock))
        try:
            yield
        finally:
            maintenance_task.cancel()
            mqtt_task.cancel()
            if firmware_task is not None:
                firmware_task.cancel()


async def security_headers(request: Request, call_next):
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
    finally:
        if request.method in ("POST", "PUT", "PATCH", "DELETE") and getattr(request.state, "audit_admin", False):
            try:
                route = getattr(request.scope.get("route"), "path", "/api")
                # Only known object identifiers, not arbitrary form data,
                # query parameters, credentials, certificates or tokens.
                params = request.path_params
                target = ", ".join(f"{key}={params[key]}" for key in (
                    "room_id", "reading_id", "device", "firmware_id", "subscription_id", "user_id", "username", "name")
                    if key in params)
                await asyncio.to_thread(audit.record, request.method, route, target, status)
            except Exception:
                logger.exception("Admin audit write failed")
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


def html_page(path: Path) -> HTMLResponse:
    # Never cache the HTML shell: each request must fetch the correct assets.
    html = path.read_text(encoding="utf-8")
    html = html.replace("__ASSET_FINGERPRINT__", _ASSET_FINGERPRINT)
    html = html.replace("__APP_VERSION__", __version__)
    html = html.replace("__FIRMWARE_RELEASE_URL__", sensor_firmware.RELEASE_URL)
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


def index():
    return html_page(ROOT / "static" / "index.html")


def firmware_debug():
    return html_page(ROOT / "firmware-debug.html")


def health():
    return {"ok": True, "service": "termokontur"}


def create_app() -> FastAPI:
    application = FastAPI(
        title="Termokontur", version=__version__, docs_url=None, redoc_url=None,
        lifespan=lifespan,
    )
    application.state.failed_logins = {}
    application.state.login_lock = Lock()
    application.middleware("http")(security_headers)
    application.add_api_route("/", index, methods=["GET"])
    application.add_api_route("/sensors/firmware-debug", firmware_debug, methods=["GET"],
                              dependencies=[Depends(access.require_admin)])
    application.add_api_route("/health", health, methods=["GET"])
    application.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
    application.include_router(access.router)
    application.include_router(operations.router)
    application.include_router(reporting.router)
    application.include_router(rooms.router)
    application.include_router(rooms.ingest_router)
    application.include_router(max_api.router)
    application.include_router(max_api.webhook_router)
    application.include_router(sensors.router(access.require_admin))
    application.include_router(sensor_onboarding.router(access.require_admin))
    return application


app = create_app()
