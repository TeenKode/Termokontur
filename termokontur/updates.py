"""Native Debian LXC update status and version metadata.

The unprivileged web worker only writes a request marker; the privileged
fixed-action systemd updater applies the hardcoded upstream main branch.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import re
import time
from pathlib import Path

import httpx

from . import __version__, db

SYSTEMD_PATH = Path("/etc/systemd/system/termokontur-update.path")
GITHUB_HEAD = "https://api.github.com/repos/TeenKode/Termokontur/commits/main"
GITHUB_VERSION = "https://raw.githubusercontent.com/TeenKode/Termokontur/{sha}/termokontur/__init__.py"
SHA_RE = re.compile(r"^[a-f0-9]{40}$")
VERSION_RE = re.compile(r'^__version__\s*=\s*["\x27](\d+\.\d+\.\d+(?:[-+][a-zA-Z0-9.-]+)?)["\x27]$', re.M)
STALLED_SECONDS = 2100
QUEUED_STALLED_SECONDS = 180


def directory() -> Path:
    return db.db_path().parent


def installed_revision() -> str | None:
    try:
        revision = (directory() / "installed-revision").read_text(encoding="ascii").strip()
        return revision if SHA_RE.fullmatch(revision) else None
    except (OSError, UnicodeError):
        return None


def supported() -> bool:
    return bool(SYSTEMD_PATH.is_file() and installed_revision())


def status() -> dict:
    revision = installed_revision()
    is_supported = bool(SYSTEMD_PATH.is_file() and revision)
    result = {
        "supported": is_supported,
        "version": __version__,
        "installed_version": __version__,
        "installed_revision": revision,
        "state": "idle",
        "phase": None,
        "message": "",
        "updated_at": None,
        "started_at": None,
        "duration_seconds": None,
        "history": [],
        "target_revision": None,
    }
    if not is_supported:
        result["message"] = (
            "Автообновление доступно после установки Debian LXC со службой обновления. "
            "Для Docker обновляйте контейнеры вручную."
        )
        return result

    path = directory() / "update-status.json"
    try:
        stat = path.stat()
        event = json.loads(path.read_text(encoding="utf-8")[:12000])
        if isinstance(event, dict) and event.get("state") in ("success", "failed", "installing", "queued"):
            result["state"] = event["state"]
            result["message"] = str(event.get("message", ""))[:300]
            phase = event.get("phase")
            if phase in ("preparing", "downloading", "dependencies", "backup",
                         "switching", "verifying", "finishing", "success", "failed"):
                result["phase"] = phase
            result["updated_at"] = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
            started = event.get("started_at")
            if isinstance(started, str):
                try:
                    start_time = datetime.fromisoformat(started.replace("Z", "+00:00"))
                    if start_time.tzinfo is not None:
                        result["started_at"] = started
                        end_time = (
                            datetime.now(timezone.utc)
                            if result["state"] in ("installing", "queued")
                            else datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
                        )
                        result["duration_seconds"] = max(0, int(
                            (end_time - start_time).total_seconds()
                        ))
                except ValueError:
                    pass
            history = event.get("history")
            if isinstance(history, list):
                result["history"] = [
                    {"phase": item["phase"], "message": str(item.get("message", ""))[:200],
                     "at": str(item.get("at", ""))[:40]}
                    for item in history[-12:]
                    if isinstance(item, dict)
                    and item.get("phase") in (
                        "preparing", "downloading", "dependencies", "backup",
                        "switching", "verifying", "finishing", "success", "failed"
                    )
                ]
            target = event.get("target_revision")
            if isinstance(target, str) and SHA_RE.fullmatch(target):
                result["target_revision"] = target
            if result["state"] == "installing" and time.time() - stat.st_mtime > STALLED_SECONDS:
                result["state"] = "failed"
                result["phase"] = "failed"
                result["message"] = (
                    "Обновление давно не сообщает о ходе работы. "
                    "Проверьте журнал termokontur-update.service перед повторной попыткой."
                )
    except (OSError, ValueError, TypeError, OverflowError):
        pass

    # A new marker always takes priority over results from a previous attempt.
    marker = directory() / "update-request"
    try:
        age = time.time() - marker.stat().st_mtime
    except OSError:
        age = None
    if age is not None:
        if age > QUEUED_STALLED_SECONDS:
            result["state"] = "failed"
            result["phase"] = "failed"
            result["message"] = (
                "Системная служба не забрала запрос за 3 минуты. "
                "Проверьте termokontur-update.path и termokontur-update.service. "
                "Файл запроса можно удалить только после проверки, что обновление не выполняется."
            )
        else:
            result["state"] = "queued"
            result["phase"] = "preparing"
            result["message"] = "Запрос принят. Ожидаем запуска системной службы."
            result["updated_at"] = datetime.fromtimestamp(
                time.time() - age, tz=timezone.utc).isoformat()
    if not result["message"]:
        result["message"] = "Можно проверить наличие новой версии."
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
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        result["available"] = None
        if result["state"] not in ("queued", "installing"):
            result["message"] = "Не удалось проверить GitHub. Проверьте интернет-соединение сервера."
        return result

    result["available"] = sha != result["installed_revision"]
    result["latest_revision"] = sha
    result["latest_version"] = None
    # Fetch version from the *same* pinned commit, not a moving main reference.
    try:
        version_response = httpx.get(GITHUB_VERSION.format(sha=sha), timeout=6.0, headers={
            "User-Agent": "Termokontur-update-checker",
        })
        version_response.raise_for_status()
        match = VERSION_RE.search(getattr(version_response, "text", "")[:300])
        if match:
            result["latest_version"] = match.group(1)
    except (httpx.HTTPError, ValueError):
        pass

    if result["state"] not in ("queued", "installing"):
        result["message"] = (
            "Доступна новая сборка. Можно установить обновление."
            if result["available"] else "Установлена актуальная сборка."
        )
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
    return {"ok": True, "state": "queued",
            "message": "Запрос принят. Ожидаем запуска системной службы."}
