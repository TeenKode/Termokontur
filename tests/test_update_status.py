"""Regression checks for native in-panel upgrade progress reporting."""
import json
import os
import time
from datetime import datetime, timedelta, timezone

from termokontur import db, updates


def enable_updater(monkeypatch, tmp_path):
    unit = tmp_path / "termokontur-update.path"
    unit.write_text("[Path]\nPathExists=/var/lib/termokontur/update-request\n")
    monkeypatch.setattr(updates, "SYSTEMD_PATH", unit)
    (db.db_path().parent / "installed-revision").write_text("a" * 40 + "\n")
    return db.db_path().parent


def test_history_and_duration_survive_api_reload(admin, monkeypatch, tmp_path):
    data = enable_updater(monkeypatch, tmp_path)
    started = datetime.now(timezone.utc) - timedelta(seconds=93)
    history = [
        {"phase": "preparing", "message": "Проверка прав", "at": started.isoformat()},
        {"phase": "downloading", "message": "Скачивание GitHub", "at": started.isoformat()},
        {"phase": "dependencies", "message": "Установка зависимостей", "at": started.isoformat()},
    ]
    event = data / "update-status.json"
    event.write_text(json.dumps({
        "state": "installing", "phase": "dependencies",
        "message": "Зависимости", "started_at": started.isoformat(),
        "target_revision": "b" * 40, "history": history,
    }), encoding="utf-8")
    result = admin.get("/api/update/status").json()
    assert result["state"] == "installing"
    assert result["target_revision"] == "b" * 40
    assert result["started_at"] == started.isoformat()
    assert 90 <= result["duration_seconds"] <= 120
    assert [entry["phase"] for entry in result["history"]] == [
        "preparing", "downloading", "dependencies",
    ]
    assert result["history"][-1]["message"] == "Установка зависимостей"

    event.write_text(json.dumps({
        "state": "success", "phase": "success", "message": "Готово",
        "started_at": started.isoformat(), "history": history,
    }), encoding="utf-8")
    result = admin.get("/api/update/status").json()
    assert result["state"] == "success"
    assert 90 <= result["duration_seconds"] <= 120


def test_stale_queue_is_reported_as_failure_not_endless_spinner(admin, monkeypatch, tmp_path):
    data = enable_updater(monkeypatch, tmp_path)
    marker = data / "update-request"
    marker.write_text("main\n")
    stale = time.time() - updates.QUEUED_STALLED_SECONDS - 10
    os.utime(marker, (stale, stale))
    result = admin.get("/api/update/status").json()
    assert result["state"] == "failed"
    assert result["phase"] == "failed"
    assert "termokontur-update.path" in result["message"]
    # Never override/remove a pending request from an unprivileged API call.
    assert marker.exists()
    assert admin.post("/api/update/request", json={}).status_code == 409


def test_untrusted_history_is_filtered(admin, monkeypatch, tmp_path):
    data = enable_updater(monkeypatch, tmp_path)
    event = data / "update-status.json"
    event.write_text(json.dumps({
        "state": "failed", "phase": "failed", "message": "Сбой",
        "target_revision": "../../root",
        "history": [None, {"phase": "other", "message": "unknown"},
                    {"phase": "failed", "message": "Test", "at": "now"}],
    }), encoding="utf-8")
    result = admin.get("/api/update/status").json()
    assert result["history"] == [{"phase": "failed", "message": "Test", "at": "now"}]
    assert result["target_revision"] is None
