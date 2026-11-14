"""Manual notices, audience selection and MAX push delivery."""
import asyncio

from termokontur import db, max_delivery, notifications, settings


def _bound_user(uid: str, room: dict):
    with db.connect() as conn:
        conn.execute("INSERT INTO max_bindings(user_id,room_id) VALUES(?,?)", (uid, room["id"]))
        conn.execute("INSERT INTO max_users(user_id,first_name,last_name,username,seen_at) VALUES(?,?,?,?,?)",
                     (uid, "Имя", uid, "", db.utc_now()))


def test_notifications_require_admin(client):
    assert client.get("/api/notifications/recipients").status_code == 401
    assert client.get("/api/notifications/history").status_code == 401
    assert client.post("/api/notifications/send", json={
        "request_id": "test-notification-000001",
        "text": "Тест", "user_ids": ["123"], "silent": False,
    }).status_code == 401


def test_notification_audience_and_idempotency(admin):
    room = db.create_room("Школа", "1", "101", "Кабинет", 18, 24)
    inactive = db.create_room("Школа", "1", "102", "Кабинет", 18, 24)
    _bound_user("123", room)
    _bound_user("321", inactive)
    db.update_room(inactive["id"], {"active": False})
    seen = admin.get("/api/notifications/recipients")
    assert seen.status_code == 200
    assert len(seen.json()) == 1
    assert seen.json()[0]["missing"] is True
    data = {"request_id": "test-notification-000001", "text": "  Проверьте термометр  ",
            "user_ids": ["123"], "silent": False}
    assert admin.post("/api/notifications/send", json=data).status_code == 409
    settings.set_secret("max_token", "some-valid-test-token")
    assert admin.post("/api/notifications/send", json={**data, "user_ids": ["321"]}).status_code == 422
    first = admin.post("/api/notifications/send", json=data)
    assert first.status_code == 200, first.text
    assert first.json() == {"id": data["request_id"], "queued": 1, "already_queued": False}
    again = admin.post("/api/notifications/send", json=data)
    assert again.status_code == 200 and again.json()["already_queued"]
    assert admin.post("/api/notifications/send", json={**data, "text": "Другой текст"}).status_code == 422
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM notification_batches").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM max_outbox").fetchone()[0] == 1
    history = admin.get("/api/notifications/history").json()
    assert history[0]["recipients"] == 1 and history[0]["delivered"] == 0


def test_notification_is_new_message_not_silent_menu(client):
    room = db.create_room("Корпус", "2", "201", "Кабинет", 18, 24)
    _bound_user("123", room)
    settings.set_secret("max_token", "some-valid-test-token")
    max_delivery.set_menu_message_id("123", "existing-menu")
    notifications.queue_batch("notify-post-123456", "Важное сообщение", ["123"], False)
    class Sender:
        def __init__(self):
            self.calls = []
        async def post(self, path, **kwargs):
            self.calls.append((path, kwargs))
            class Reply:
                def raise_for_status(self): pass
            return Reply()
        async def put(self, *args, **kwargs):
            raise AssertionError("Нельзя редактировать меню для уведомления")
    fake = Sender()
    asyncio.run(max_delivery.drain(fake))
    assert len(fake.calls) == 1
    assert fake.calls[0][1]["json"]["notify"] is True
    assert "attachments" not in fake.calls[0][1]["json"]
    assert max_delivery.menu_message_id("123") == "existing-menu"
    assert notifications.history()[0]["delivered"] == 1


def test_alert_does_not_repeat_without_new_reading(client):
    room = db.create_room("Школа", "1", "103", "Кабинет", 18, 24)
    _bound_user("123", room)
    settings.set_secret("max_token", "some-valid-test-token")
    settings.update({"alerts_enabled": "1", "alert_cooldown_minutes": "60"})
    db.add_reading(room["id"], 15.0, "manual", "Администратор")
    assert max_delivery.queue_temperature_alerts() == 0
    db.add_reading(room["id"], 15.0, "max", "MAX:123")
    assert max_delivery.queue_temperature_alerts() == 0
    db.add_reading(room["id"], 15.0, "ha", "sensor")
    assert max_delivery.queue_temperature_alerts() == 1
    assert max_delivery.queue_temperature_alerts() == 0
    item = max_delivery.pending()[0]
    assert item["delivery_mode"] == "notice"
    assert item["notify"] == 1



def test_manual_max_reading_shows_deviation_without_push(client):
    from termokontur import bot, max_ui
    room = db.create_room("Школа", "1", "104", "Кабинет", 18, 24)
    _bound_user("123", room)
    settings.set_secret("max_token", "some-valid-test-token")
    settings.update({"alerts_enabled": "1", "alert_cooldown_minutes": "60"})
    reply = bot.interpret("123", f"/temp {room['id']} 16", "test-reading-1")
    assert "Сохранено" in reply
    assert "вне установленной нормы" in reply
    assert max_delivery.queue_temperature_alerts() == 0

    # Button-based correction also displays a warning immediately, without
    # an extra notification in the outbox.
    reply = max_ui._write("123", room["id"], "c", "17", "max:correction-2")
    assert "Исправлено" in reply
    assert "вне установленной нормы" in reply
    assert max_delivery.queue_temperature_alerts() == 0
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM max_outbox").fetchone()[0] == 0


def test_notification_history_survives_sent_queue_pruning(client):
    room = db.create_room("Школа", "1", "105", "Кабинет", 18, 24)
    _bound_user("123", room)
    settings.set_secret("max_token", "some-valid-test-token")
    notifications.queue_batch("notify-hist-123456", "Тест", ["123"], True)
    with db.connect() as conn:
        event_id = conn.execute("SELECT event_id FROM notification_recipients").fetchone()[0]
    max_delivery.mark_sent(event_id)
    with db.connect() as conn:
        conn.execute("DELETE FROM max_outbox WHERE event_id=?", (event_id,))
    history = notifications.history()
    assert history[0]["recipients"] == 1
    assert history[0]["delivered"] == 1
    assert history[0]["retrying"] == 0
