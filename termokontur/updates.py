"""Native installation update control.

The web worker never runs commands or obtains root access. It can only request
a fixed operation using a marker file. A separate root-owned systemd unit
always fetches the hardcoded upstream main branch and restarts services.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import httpx

from . import db

SYSTEMD_PATH = Path("/etc/systemd/system/termokontur-update.path")
GITHUB_HEAD = "https://api.github.com/repos/TeenKode/Termokontur/commits/main"
SHA_RE = re.compile(r"^[a-f0-9]{40}$")

def directory() -> Path:
    return db.db_path().parent

def installed_revision() -> str | None:
    try:
        rev = (directory() / "installed-revision").read_text(encoding="ascii").strip()
        return rev if SHA_RE.fullmatch(rev) else None
    except (OSError, UnicodeError):
        return None

def supported() -> bool:
    return bool(SYSTEMD_PATH.is_file() and installed_revision())

def status() -> dict:
    rev = installed_revision()
    result = {"supported": supported(), "installed_revision": rev,
              "state": "idle", "message": ""}
    if not result["supported"]:
        result["message"] = "Автообновление доступно после установки Debian LXC со службой обновления. В Docker обновляйте контейнеры вручную."
        return result
    try:
        raw = (directory() / "update-status.json").read_text(encoding="utf-8")[:2000]
        event = json.loads(raw)
        if isinstance(event, dict) and event.get("state") in ("success", "failed", "installing"):
            result["state"] = event["state"]
            result["message"] = str(event.get("message", ""))[:300]
    except (OSError, ValueError, TypeError):
        pass
    if (directory() / "update-request").exists():
        result["state"] = "queued"
        result["message"] = "Запрос передан системной службе обновления."
    if not result["message"]:
        result["message"] = "Установка готова к проверке новой версии."
    return result

def check() -> dict:
    result = status()
    if not result["supported"]:
        result["available"] = False
        return result
    try:
        response = httpx.get(GITHUB_HEAD, timeout=8.0, headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "Termokontur-update-checker",
        })
        response.raise_for_status()
        sha = response.json()["sha"]
        if not isinstance(sha, str) or not SHA_RE.fullmatch(sha):
            raise ValueError("Invalid GitHub commit hash")
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        result["available"] = None
        result["message"] = "Не удалось проверить GitHub. Убедитесь, что сервер имеет доступ к github.com."
        return result
    result["available"] = sha != result["installed_revision"]
    result["latest_revision"] = sha
    result["message"] = ("Доступно обновление из основного репозитория." if result["available"]
                         else "Установлена последняя версия.")
    return result

def request() -> dict:
    if not supported():
        raise RuntimeError("На сервере не настроена служба автообновления")
    if status()["state"] in ("queued", "installing"):
        raise RuntimeError("Обновление уже запущено")
    marker = directory() / "update-request"
    try:
        with marker.open("x", encoding="ascii") as fp:
            fp.write("main\n")
        marker.chmod(0o600)
    except FileExistsError as exc:
        raise RuntimeError("Обновление уже ожидает выполнения") from exc
    return {"ok": True, "message": "Обновление запрошено. Проверьте статус через несколько секунд."}
