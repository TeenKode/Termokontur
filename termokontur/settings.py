"""Persist end-user settings and integration credentials in SQLite.

Only the internal session signing secret is an infrastructure secret and
remains outside the database. The web API never returns stored credentials.
"""
from __future__ import annotations

import os
import re
import secrets
import sqlite3
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import db

ENV_DEFAULTS = {
    "organization_name": ("TERMOKONTUR_ORGANIZATION", "Термоконтур"),
    "timezone": ("TERMOKONTUR_TZ", "UTC"),
    "stale_hours": ("TERMOKONTUR_STALE_HOURS", "26"),
    "reminders_enabled": ("TERMOKONTUR_REMINDERS_ENABLED", "0"),
    "reminder_time": ("TERMOKONTUR_REMINDER_TIME", "09:00"),
    "reminder_days": ("TERMOKONTUR_REMINDER_DAYS", "0,1,2,3,4"),
    "reminder_window_minutes": ("TERMOKONTUR_REMINDER_WINDOW_MINUTES", "120"),
}

def _valid_secret(value: str) -> bool:
    return bool(value and len(value) >= 20 and not value.startswith("REPLACE_WITH_"))

def bootstrap() -> None:
    """One-time import from older .env deployments without overriding UI changes."""
    with db.connect() as conn:
        for key, (env, default) in ENV_DEFAULTS.items():
            value = os.environ.get(env, default)
            conn.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
                (key, value),
            )
        for key, env in (("max_token", "MAX_BOT_TOKEN"), ("ingest_key", "TERMOKONTUR_INGEST_KEY")):
            value = os.environ.get(env, "")
            if _valid_secret(value):
                conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (key, value))
        # A generated token is not automatically provisioned when env was empty:
        # the administrator creates it from the panel only when required.

def get(key: str, default: str | None = None) -> str | None:
    with db.connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        if row is None:
            if key in ENV_DEFAULTS:
                env, fallback = ENV_DEFAULTS[key]
                return os.environ.get(env, fallback)
            return default
        return row["value"]

def update(values: dict[str, str]) -> None:
    allowed = set(ENV_DEFAULTS)
    if not set(values).issubset(allowed):
        raise ValueError("Неизвестная настройка")
    with db.connect() as conn:
        for key, value in values.items():
            conn.execute(
                """INSERT INTO settings(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                (key, value),
            )

def set_secret(key: str, value: str | None) -> None:
    if key not in ("max_token", "ingest_key"):
        raise ValueError("Unknown secret")
    with db.connect() as conn:
        if value is None:
            conn.execute("DELETE FROM settings WHERE key=?", (key,))
        elif _valid_secret(value):
            conn.execute(
                """INSERT INTO settings(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                (key, value),
            )
        else:
            raise ValueError("Ключ должен содержать минимум 20 символов")

def public() -> dict:
    data = {key: get(key) for key in ENV_DEFAULTS}
    data["stale_hours"] = int(data["stale_hours"])
    data["reminders_enabled"] = data["reminders_enabled"] == "1"
    data["reminder_days"] = [int(x) for x in data["reminder_days"].split(",") if x.strip()]
    data["reminder_window_minutes"] = int(data["reminder_window_minutes"])
    data["max_token_configured"] = bool(get("max_token"))
    data["ingest_key_configured"] = bool(get("ingest_key"))
    return data

def validated(data: dict) -> dict[str, str]:
    try:
        name = data["organization_name"].strip()
        timezone = data["timezone"].strip()
        ZoneInfo(timezone)
        if not 1 <= len(name) <= 100 or not 1 <= len(timezone) <= 64:
            raise ValueError()
        stale = int(data["stale_hours"])
        if not 1 <= stale <= 168:
            raise ValueError()
        enabled = data["reminders_enabled"]
        if not isinstance(enabled, bool):
            raise ValueError()
        at = data["reminder_time"]
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", at):
            raise ValueError()
        days = data["reminder_days"]
        if not isinstance(days, list) or not days or any(
            type(x) is not int or x not in range(7) for x in days
        ):
            raise ValueError()
        window = int(data["reminder_window_minutes"])
        if not 1 <= window <= 240:
            raise ValueError()
    except (KeyError, TypeError, ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError("Проверьте название, часовой пояс и параметры расписания") from exc
    return {
        "organization_name": name,
        "timezone": timezone,
        "stale_hours": str(stale),
        "reminders_enabled": "1" if enabled else "0",
        "reminder_time": at,
        "reminder_days": ",".join(map(str, sorted(set(days)))),
        "reminder_window_minutes": str(window),
    }
