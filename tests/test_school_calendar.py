from datetime import datetime, timezone

from termokontur import daily, db, reports, settings


def test_holiday_and_makeup_day_share_calendar_with_reminders(admin, monkeypatch):
    settings.update({"timezone": "Asia/Vladivostok", "reminders_enabled": "1"})
    monkeypatch.setattr(db, "utc_now", lambda: "2026-11-19T01:00:00+00:00")
    senior = db.create_room("Старшая школа", "", "101", "Кабинет", 18, 24)
    junior = db.create_room("Начальная школа", "", "201", "Кабинет", 18, 24)
    for room in (senior, junior):
        assert db.bind_code(str(room["id"]), db.issue_code(room["id"]))
    rules = [
        {"building": "", "start": "2026-11-20", "end": "2026-11-22", "expected": False},
        {"building": "Старшая школа", "start": "2026-11-21", "end": "2026-11-21", "expected": True},
    ]
    assert admin.put("/api/school-calendar", json={"exceptions": rules}).status_code == 200
    assert admin.put("/api/school-calendar", json={"exceptions": rules + rules}).status_code == 422
    saturday = datetime(2026, 11, 20, 23, 10, tzinfo=timezone.utc)
    assert daily.reminders_due(saturday)
    snapshot = daily.summary(saturday)
    assert set(daily.pending_users(snapshot)) == {str(senior["id"])}
    report = reports.build("2026-11-20", "2026-11-21", now=saturday)
    assert report["summary"]["rooms_total"] == 1
    assert [(row["date"], row["room_id"]) for row in report["missing"]] == [("2026-11-21", senior["id"])]


def test_room_lifetime_preserves_history(admin, monkeypatch):
    monkeypatch.setattr(db, "utc_now", lambda: "2026-11-16T00:00:00+00:00")
    old = db.create_room("Школа", "", "101", "Кабинет", 18, 24)
    db.add_reading(old["id"], 22, "manual")
    monkeypatch.setattr(db, "utc_now", lambda: "2026-11-23T00:00:00+00:00")
    db.update_room(old["id"], {"active": False})
    db.create_room("Школа", "", "102", "Кабинет", 18, 24)
    now = datetime(2026, 11, 23, 13, tzinfo=timezone.utc)
    previous = reports.build("2026-11-16", "2026-11-16", now=now)
    assert previous["summary"]["rooms_measured"] == 1
    assert previous["rooms"][0]["room"] == "101"
    assert previous["missing"] == []
    current = reports.build("2026-11-23", "2026-11-23", now=now)
    assert [row["room"] for row in current["missing"]] == ["102"]
