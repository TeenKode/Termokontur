"""Daily reporting, timezones and reminder idempotency tests."""
import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

from termokontur import daily, db, settings
from termokontur.bot import send_reminders


def utc(value):
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


def test_local_day_boundaries_and_dst(client, monkeypatch):
    settings.update({"timezone": "Asia/Vladivostok"})
    day, start, end = daily.daily_bounds(utc("2026-11-12T01:00:00"))
    assert str(day) == "2026-11-12"
    assert start == "2026-11-11T14:00:00+00:00"
    assert end == "2026-11-12T14:00:00+00:00"
    settings.update({"timezone": "Europe/Berlin"})
    day, start, end = daily.daily_bounds(utc("2026-03-29T13:00:00"))
    assert str(day) == "2026-03-29"
    assert start == "2026-03-28T23:00:00+00:00"
    assert end == "2026-03-29T22:00:00+00:00"  # 23-hour DST day


def test_daily_coverage_ignores_previous_day(client, monkeypatch):
    settings.update({"timezone": "UTC"})
    room = db.create_room("Корпус", "2", "201", "Кабинет", 18, 24)
    instant = utc("2026-11-12T10:00:00")
    with db.connect() as conn:
        conn.execute("""INSERT INTO readings
            (room_id, value, source, reporter, observed_at)
            VALUES (?, 22, 'manual', 'test', ?)""", (room["id"], "2026-11-11T23:59:59+00:00"))
    assert daily.summary(instant)["counts"]["missing"] == 1
    with db.connect() as conn:
        conn.execute("""INSERT INTO readings
            (room_id, value, source, reporter, observed_at)
            VALUES (?, 16, 'ha', 'test', ?)""", (room["id"], "2026-11-12T08:00:00+00:00"))
    result = daily.summary(instant)
    assert result["counts"] == {"measured": 1, "missing": 0, "out_of_range": 1}
    assert result["rooms"][0]["measured_source"] == "ha"


def test_reminder_time_weekday_window(client, monkeypatch):
    settings.update({"timezone": "Asia/Vladivostok"})
    settings.update({"reminders_enabled": "1"})
    settings.update({"reminder_time": "09:00"})
    settings.update({"reminder_days": "0,1,2,3,4"})
    settings.update({"reminder_window_minutes": "120"})
    # Mon Nov 16, 2026; time in Vladivostok is UTC+10
    assert not daily.reminders_due(utc("2026-11-15T22:59:00"))
    assert daily.reminders_due(utc("2026-11-15T23:15:00"))
    assert not daily.reminders_due(utc("2026-11-16T01:00:00"))  # window ends at 11
    assert not daily.reminders_due(utc("2026-11-14T23:15:00"))  # Sunday
    settings.update({"reminders_enabled": "0"})
    assert not daily.reminders_due(utc("2026-11-15T23:15:00"))


def test_one_claim_per_day_and_retry(client):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    assert daily.claim_reminder("2026-11-12", "123", now)
    assert not daily.claim_reminder("2026-11-12", "123", now)
    daily.mark_reminder("2026-11-12", "123", False)
    assert not daily.claim_reminder("2026-11-12", "123", now + timedelta(minutes=4))
    assert daily.claim_reminder("2026-11-12", "123", now + timedelta(minutes=6))
    daily.mark_reminder("2026-11-12", "123", True)
    assert not daily.claim_reminder("2026-11-12", "123", now + timedelta(hours=1))
    assert daily.claim_reminder("2026-11-13", "123", now + timedelta(days=1))


def test_pending_groups_and_bot_sends_only_missing(client, monkeypatch):
    settings.update({"timezone": "UTC"})
    r1 = db.create_room("Старшая", "1", "101", "Кабинет", 18, 24)
    r2 = db.create_room("Старшая", "1", "102", "Кабинет", 18, 24)
    for room in (r1, r2):
        assert db.bind_code("777", db.issue_code(room["id"]))
    db.add_reading(r1["id"], 21.5, "manual")
    snapshot = daily.summary()
    missing = daily.pending_users(snapshot)
    assert list(missing) == ["777"]
    assert [x["id"] for x in missing["777"]] == [r2["id"]]

    monkeypatch.setattr(daily, "reminders_due", lambda: True)

    class FakeResponse:
        def raise_for_status(self):
            pass

    class FakeClient:
        def __init__(self):
            self.calls = []

        async def post(self, path, params, json):
            self.calls.append((path, params, json))
            return FakeResponse()

    fake = FakeClient()
    asyncio.run(send_reminders(fake))
    asyncio.run(send_reminders(fake))
    assert len(fake.calls) == 1
    assert fake.calls[0][1] == {"user_id": "777"}
    assert "102" in fake.calls[0][2]["text"]
    assert "101" not in fake.calls[0][2]["text"]


def test_two_colleagues_receive_reminder_only_until_one_reports(client, monkeypatch):
    room = db.create_room("Корпус", "1", "102", "Кабинет", 18, 24)
    for user in ("7", "8"):
        assert db.bind_code(user, db.issue_code(room["id"]))
    snapshot = daily.summary()
    assert sorted(daily.pending_users(snapshot)) == ["7", "8"]

    monkeypatch.setattr(daily, "reminders_due", lambda: True)
    class Response:
        def raise_for_status(self): pass
    class FakeClient:
        def __init__(self):
            self.posts = []
        async def post(self, route, params, json):
            self.posts.append((params["user_id"], json["text"]))
            return Response()

    sender = FakeClient()
    asyncio.run(send_reminders(sender))
    assert sorted(user for user, _ in sender.posts) == ["7", "8"]

    # An accepted reading from either colleague completes the room/day.
    from termokontur import max_ui
    assert "Сохранено" in max_ui._write("7", room["id"], "s", "21", "max:message:7")
    assert daily.pending_users(daily.summary()) == {}
    before = len(sender.posts)
    asyncio.run(send_reminders(sender))
    assert len(sender.posts) == before


def test_reminder_rechecks_snapshot_when_colleague_submits_before_send(client, monkeypatch):
    room = db.create_room("Корпус", "1", "103", "Кабинет", 18, 24)
    for user in ("7", "8"):
        assert db.bind_code(user, db.issue_code(room["id"]))
    monkeypatch.setattr(daily, "reminders_due", lambda: True)
    original = daily.summary
    calls = {"count": 0}
    def summary_with_submit():
        calls["count"] += 1
        if calls["count"] == 2:
            db.add_reading(room["id"], 21, "manual", "Администратор")
        return original()
    monkeypatch.setattr(daily, "summary", summary_with_submit)

    class FakeClient:
        async def post(self, *args, **kwargs):
            raise AssertionError("Reminder for a room that is now measured")
    asyncio.run(send_reminders(FakeClient()))
    assert calls["count"] >= 2
    assert original()["counts"]["missing"] == 0
