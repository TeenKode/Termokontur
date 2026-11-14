"""Daily averages must be room-weighted and use local calendar days."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from termokontur import db, settings


def _room(admin, building, name):
    response = admin.post("/api/rooms", json={
        "building": building, "floor": "1", "name": name,
        "kind": "Кабинет", "min_temp": 18, "max_temp": 24,
    })
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _reading(room_id, value, observed):
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO readings(room_id,value,source,reporter,observed_at) "
            "VALUES(?,?,'manual','Тест',?)",
            (room_id, value, observed.astimezone(timezone.utc).isoformat(timespec="seconds")),
        )


def test_history_requires_login_and_checks_period(client):
    assert client.get("/api/dashboard/history").status_code == 401
    client.post("/api/login", json={"password": "this-is-a-test-admin-password"})
    assert client.get("/api/dashboard/history?days=8").status_code == 422
    assert client.get("/api/dashboard/history?days=90").status_code == 200
    data = client.get("/api/dashboard/history?days=7").json()
    assert len(data["series"]) == 7
    assert all(day["average"] is None for day in data["series"])
    assert data["rooms_total"] == 0


def test_room_day_average_does_not_overweight_sensor_readings(admin):
    a = _room(admin, "Корпус А", "101")
    b = _room(admin, "Корпус Б", "201")
    now = datetime(2026, 11, 14, 12, tzinfo=timezone.utc)
    _reading(a, 21, now - timedelta(days=2))
    _reading(a, 20, now - timedelta(days=1, hours=1))
    _reading(a, 24, now - timedelta(days=1))
    _reading(b, 18, now - timedelta(days=1))

    data = db.dashboard_history(days=7, timezone_name="UTC", now=now)
    yesterday = data["series"][-2]
    assert yesterday["average"] == 20  # (mean(20,24) + 18) / 2
    assert yesterday["minimum"] == 18
    assert yesterday["maximum"] == 22
    assert yesterday["rooms_measured"] == 2
    assert yesterday["coverage"] == 100
    assert yesterday["readings_count"] == 3
    assert data["series"][-3]["average"] == 21
    assert data["series"][-3]["rooms_measured"] == 1
    assert data["series"][-3]["coverage"] == 50
    assert data["series"][-1]["average"] is None
    assert data["series"][-1]["minimum"] is None
    assert data["series"][-1]["coverage"] == 0

    room_a = next(room for room in data["rooms"] if room["id"] == a)
    assert room_a["average"] == 21.5  # equal weight per reported day
    assert room_a["days_reported"] == 2
    assert room_a["daily"]["2026-11-13"] == {"average": 22, "count": 2}

    filtered = db.dashboard_history(days=7, timezone_name="UTC", building="Корпус Б", now=now)
    assert filtered["rooms_total"] == 1
    assert filtered["series"][-2]["average"] == 18
    assert filtered["series"][-2]["rooms_measured"] == 1

    # Disabled rooms and their historical readings are not included in current monitoring.
    db.update_room(b, {"active": False})
    active_only = db.dashboard_history(days=7, timezone_name="UTC", now=now)
    assert active_only["rooms_total"] == 1
    assert active_only["series"][-2]["average"] == 22


def test_local_days_follow_timezone_and_dst(admin):
    room = _room(admin, "Школа", "2")
    # On 2026-10-25 clocks in Berlin fall back: the two UTC instants
    # below both occur at 02:30 local, but they are separate measurements.
    _reading(room, 18, datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc))
    _reading(room, 22, datetime(2026, 10, 25, 1, 30, tzinfo=timezone.utc))
    _reading(room, 26, datetime(2026, 10, 25, 23, 30, tzinfo=timezone.utc))
    data = db.dashboard_history(
        days=7, timezone_name="Europe/Berlin",
        now=datetime(2026, 10, 26, 12, tzinfo=timezone.utc),
    )
    assert data["series"][-2]["date"] == "2026-10-25"
    assert data["series"][-2]["average"] == 20
    assert data["series"][-2]["readings_count"] == 2
    assert data["series"][-1]["date"] == "2026-10-26"
    assert data["series"][-1]["average"] == 26
    assert data["series"][-1]["out_of_range"] == 1


def test_history_api_uses_configured_timezone_and_building(admin):
    settings.update({"timezone": "Asia/Vladivostok"})
    a = _room(admin, "Школа", "101")
    _room(admin, "Детский сад", "1")
    now = datetime.now(timezone.utc)
    local_day = now.astimezone(ZoneInfo("Asia/Vladivostok")).date()
    # 12:00 local today is in the requested day independently of UTC date.
    local_noon = datetime.combine(
        local_day, datetime.min.time(), tzinfo=ZoneInfo("Asia/Vladivostok")
    ) + timedelta(hours=12)
    _reading(a, 23, local_noon)
    result = admin.get("/api/dashboard/history?days=14&building=Школа")
    assert result.status_code == 200, result.text
    data = result.json()
    assert data["timezone"] == "Asia/Vladivostok"
    assert data["rooms_total"] == 1
    assert data["series"][-1]["date"] == local_day.isoformat()
    assert data["series"][-1]["average"] == 23
    assert data["series"][-1]["coverage"] == 100


def test_building_daily_averages_are_separate_and_room_weighted(admin):
    """Buildings are averaged from distinct rooms, not raw sensor samples."""
    a1 = _room(admin, "Старшая школа", "101")
    a2 = _room(admin, "Старшая школа", "102")
    b1 = _room(admin, "Начальная школа", "201")
    now = datetime(2026, 11, 14, 15, tzinfo=timezone.utc)
    today = datetime(2026, 11, 14, 9, tzinfo=timezone.utc)
    _reading(a1, 19, today)
    _reading(a1, 23, today + timedelta(minutes=5))  # room 101 = 21 °C
    _reading(a2, 23, today)  # room 102 = 23 °C
    _reading(b1, 18, today)  # school mean = (21 + 23 + 18)/3

    result = db.dashboard_history(days=14, timezone_name="UTC", now=now)
    assert result["series"][-1]["average"] == 20.67
    assert result["series"][-1]["rooms_measured"] == 3
    assert result["series"][-1]["readings_count"] == 4
    assert [item["name"] for item in result["buildings"]] == [
        "Начальная школа", "Старшая школа"
    ]
    school_a = next(b for b in result["buildings"] if b["name"] == "Старшая школа")
    school_b = next(b for b in result["buildings"] if b["name"] == "Начальная школа")
    assert school_a["rooms_total"] == 2
    assert school_a["series"][-1]["average"] == 22
    assert school_a["series"][-1]["coverage"] == 100
    assert school_a["series"][-1]["readings_count"] == 3
    assert school_b["series"][-1]["average"] == 18
    assert school_b["series"][-2]["average"] is None
    assert school_b["series"][-2]["coverage"] == 0

    selected = db.dashboard_history(days=7, timezone_name="UTC",
                                    building="Старшая школа", now=now)
    assert len(selected["buildings"]) == 1
    assert selected["series"][-1]["average"] == 22
    assert selected["series"][-1]["rooms_total"] == 2

    # The second building has not reported the following day.
    tomorrow = db.dashboard_history(days=7, timezone_name="UTC",
                                    now=now + timedelta(days=1))
    assert tomorrow["series"][-1]["average"] is None
    assert all(building["series"][-1]["average"] is None for building in tomorrow["buildings"])


def test_building_api_returns_local_daily_series(admin):
    settings.update({"timezone": "UTC"})
    a = _room(admin, "Корпус А", "1")
    _room(admin, "Корпус Б", "2")
    _reading(a, 21, datetime.now(timezone.utc))
    response = admin.get("/api/dashboard/history?days=7")
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["buildings"]) == 2
    assert len(body["buildings"][0]["series"]) == 7
    assert sum(b["series"][-1]["rooms_measured"] for b in body["buildings"]) == 1
    assert sum(b["series"][-1]["readings_count"] for b in body["buildings"]) == 1
