"""MAX button flows, delivery and correction regression tests."""
import asyncio
import json

from termokontur import db, daily, max_delivery, max_ui
from termokontur.bot import process_update


def _room(name="101"):
    return db.create_room("Школа", "1", name, "Кабинет", 18, 24)


def _bind(user, room):
    code = db.issue_code(room["id"])
    assert db.bind_code(str(user), code, f"bind:{user}:{room['id']}")


def _callback(user, payload, mid="c1", private=True):
    return {
        "update_type": "message_callback",
        "callback": {"user": {"user_id": user, "is_bot": False},
                     "callback_id": mid, "payload": payload},
        "message": {"recipient": {"chat_type": "dialog" if private else "chat"},
                    "body": {"mid": "botmsg1"}},
    }


def _message(user, text, mid="m1"):
    return {
        "update_type": "message_created",
        "message": {"sender": {"user_id": user, "is_bot": False},
                    "recipient": {"chat_type": "dialog"},
                    "body": {"text": text, "mid": mid}},
    }


def _payloads(user):
    return [button["payload"] for row in max_ui.menu_buttons(str(user))
            for button in row]


def test_start_and_bind_with_buttons(client):
    start = process_update({"update_type": "bot_started",
                            "user": {"user_id": 7}, "timestamp": 1000})
    assert "код" in start[1].lower()
    assert "tk:send" in _payloads(7)
    assert "tk:change" in _payloads(7)
    room = _room()
    code = db.issue_code(room["id"])
    assert "привязан" in process_update(_message(7, code))[1]
    assert "tk:send" in _payloads(7)
    assert "tk:change" in _payloads(7)


def test_single_room_submit_and_correct_without_duplicate_reading(client):
    room = _room()
    _bind(7, room)
    assert "Выберите температуру" in process_update(_callback(7, "tk:send", "c1"))[1]
    day = daily.daily_bounds()[0].strftime("%Y%m%d")
    payload = f"tk:value:s:{room['id']}:{day}:21.5"
    result = process_update(_callback(7, payload, "c2"))
    assert "Сохранено" in result[1]
    assert len(db.recent_readings()) == 1
    assert db.recent_readings()[0]["value"] == 21.5
    assert "tk:send" in _payloads(7)
    assert "tk:change" in _payloads(7)

    assert "21.5" in process_update(_callback(7, "tk:change", "c3"))[1]
    changed = process_update(_callback(7, f"tk:value:c:{room['id']}:{day}:23", "c4"))
    assert "Исправлено" in changed[1]
    assert len(db.recent_readings()) == 1
    assert db.recent_readings()[0]["value"] == 23
    with db.connect() as conn:
        audit = conn.execute("SELECT * FROM max_reading_corrections").fetchall()
    assert len(audit) == 1
    assert audit[0]["old_value"] == 21.5
    assert audit[0]["new_value"] == 23
    assert "уже сохранено" in process_update(
        _callback(7, f"tk:value:c:{room['id']}:{day}:23", "c4")
    )[1]
    assert len(db.recent_readings()) == 1


def test_multiple_rooms_choose_one_then_manual_input(client):
    first, second = _room("101"), _room("102")
    _bind(7, first)
    _bind(7, second)
    assert "Выберите кабинет" in process_update(_callback(7, "tk:send"))[1]
    day = daily.daily_bounds()[0].strftime("%Y%m%d")
    assert f"tk:room:s:{second['id']}:{day}" in _payloads(7)
    assert "102" in process_update(
        _callback(7, f"tk:room:s:{second['id']}:{day}", "c2")
    )[1]
    assert "Введите показание" in process_update(
        _callback(7, f"tk:manual:s:{second['id']}:{day}", "c3")
    )[1]
    assert "Сохранено" in process_update(_message(7, "20,7", "m2"))[1]
    assert db.recent_readings()[0]["room_id"] == second["id"]
    assert db.recent_readings()[0]["value"] == 20.7
    assert "tk:send" in _payloads(7)
    assert "tk:change" in _payloads(7)


def test_manual_correction_of_today(client):
    room = _room()
    _bind(7, room)
    day = daily.daily_bounds()[0].strftime("%Y%m%d")
    process_update(_callback(7, "tk:send"))
    process_update(_callback(7, f"tk:value:s:{room['id']}:{day}:20", "save"))
    process_update(_callback(7, "tk:change", "edit"))
    process_update(_callback(7, f"tk:manual:c:{room['id']}:{day}", "manual"))
    assert "Исправлено" in process_update(_message(7, "21,8", "manual-value"))[1]
    assert db.recent_readings()[0]["value"] == 21.8
    assert len(db.recent_readings()) == 1


def test_stale_button_and_unauthorised_user_do_not_write(client):
    room = _room()
    _bind(7, room)
    old_day = "20000101"
    text = process_update(
        _callback(7, f"tk:value:s:{room['id']}:{old_day}:21", "old")
    )[1]
    assert "другому дню" in text
    assert len(db.recent_readings()) == 0
    day = daily.daily_bounds()[0].strftime("%Y%m%d")
    text = process_update(
        _callback(8, f"tk:value:s:{room['id']}:{day}:21", "other")
    )[1]
    assert "Нет доступа" in text
    assert len(db.recent_readings()) == 0
    assert process_update(_callback(7, "tk:send", "group", private=False)) is None


def test_first_measurement_via_change_button_redirects_to_submission(client):
    room = _room()
    _bind(7, room)
    text = process_update(_callback(7, "tk:change"))[1]
    assert "ещё не передавали" in text
    day = daily.daily_bounds()[0].strftime("%Y%m%d")
    assert f"tk:value:s:{room['id']}:{day}:21" in _payloads(7)


def test_max_delivery_callbacks_edit_message_and_regular_replies_have_menu(client):
    room = _room()
    _bind(7, room)
    assert max_delivery.enqueue(_callback(7, "tk:send", "cb-ack"))
    queued = max_delivery.pending()
    assert len(queued) == 1
    assert queued[0]["callback_id"] == "cb-ack"
    assert queued[0]["origin_mid"] == "botmsg1"
    buttons = json.loads(queued[0]["buttons_json"])[0]["payload"]["buttons"]
    assert buttons[0][0]["text"] == "Отправить за сегодня"

    class Client:
        def __init__(self):
            self.calls = []
        async def post(self, route, **kwargs):
            self.calls.append(("post", route, kwargs))
            class Response:
                def raise_for_status(self): pass
            return Response()
        async def put(self, route, **kwargs):
            self.calls.append(("put", route, kwargs))
            class Response:
                def raise_for_status(self): pass
            return Response()

    fake = Client()
    asyncio.run(max_delivery.drain(fake))
    assert fake.calls[0][1] == "/answers"
    assert fake.calls[0][2]["params"]["callback_id"] == "cb-ack"
    assert fake.calls[0][2]["json"]["message"]["attachments"][0]["type"] == "inline_keyboard"
    assert max_delivery.menu_message_id("7") == "botmsg1"
    assert max_delivery.pending() == []

    assert max_delivery.enqueue(_message(7, "/today", "new-msg"))
    asyncio.run(max_delivery.drain(fake))
    assert fake.calls[1][0:2] == ("put", "/messages")
    assert fake.calls[1][2]["params"]["message_id"] == "botmsg1"
    assert fake.calls[1][2]["json"]["attachments"][0]["type"] == "inline_keyboard"
    assert len(fake.calls) == 2  # No extra messages created for typed commands.


def test_max_reuses_one_menu_across_multiple_typed_commands(client):
    class Client:
        def __init__(self):
            self.posts = 0
            self.edits = []
        async def post(self, route, **kwargs):
            assert route == "/messages"
            self.posts += 1
            class Response:
                def raise_for_status(self): pass
                def json(self): return {"message": {"body": {"mid": "menu-1"}}}
            return Response()
        async def put(self, route, **kwargs):
            self.edits.append((route, kwargs))
            class Response:
                def raise_for_status(self): pass
            return Response()

    fake = Client()
    for i, command in enumerate(("/start", "/help", "/today", "/rooms")):
        assert max_delivery.enqueue(_message(22, command, f"typed-{i}"))
        asyncio.run(max_delivery.drain(fake))
    assert fake.posts == 1
    assert len(fake.edits) == 3
    assert all(call[1]["params"]["message_id"] == "menu-1" for call in fake.edits)
    assert max_delivery.menu_message_id("22") == "menu-1"


def test_max_creates_new_menu_only_if_old_one_is_missing(client):
    import httpx

    class Client:
        def __init__(self):
            self.posts = 0
            self.edits = 0
        async def put(self, route, **kwargs):
            self.edits += 1
            request = httpx.Request("PUT", "https://platform-api2.max.ru/messages")
            return httpx.Response(404, request=request)
        async def post(self, route, **kwargs):
            self.posts += 1
            class Response:
                def raise_for_status(self): pass
                def json(self): return {"message": {"body": {"mid": "new-menu"}}}
            return Response()

    max_delivery.set_menu_message_id("23", "deleted-menu")
    assert max_delivery.enqueue(_message(23, "/help", "recover-menu"))
    fake = Client()
    asyncio.run(max_delivery.drain(fake))
    assert fake.edits == 1
    assert fake.posts == 1
    assert max_delivery.menu_message_id("23") == "new-menu"


def test_max_failed_edit_is_retried_without_spamming(client):
    import httpx

    class Client:
        def __init__(self):
            self.posts = 0
        async def put(self, route, **kwargs):
            request = httpx.Request("PUT", "https://platform-api2.max.ru/messages")
            return httpx.Response(503, request=request)
        async def post(self, route, **kwargs):
            self.posts += 1
            raise AssertionError("A failed edit must not create a new message")

    max_delivery.set_menu_message_id("24", "existing-menu")
    assert max_delivery.enqueue(_message(24, "/help", "failure"))
    fake = Client()
    asyncio.run(max_delivery.drain(fake))
    assert fake.posts == 0
    assert len(max_delivery.pending()) == 0  # Delayed for retry, not dropped.
    with db.connect() as conn:
        row = conn.execute("SELECT attempts, delivered_at FROM max_outbox").fetchone()
    assert row["attempts"] == 1 and row["delivered_at"] is None


def test_replayed_delivery_cannot_repeat_callback_effect(client):
    room = _room()
    _bind(7, room)
    day = daily.daily_bounds()[0].strftime("%Y%m%d")
    event = _callback(7, f"tk:value:s:{room['id']}:{day}:21", "once")
    assert max_delivery.enqueue(event)
    assert not max_delivery.enqueue(event)
    assert len(db.recent_readings()) == 1
    assert "уже" in process_update(
        _callback(7, f"tk:value:s:{room['id']}:{day}:23", "once")
    )[1].lower()
    assert db.recent_readings()[0]["value"] == 21


def test_change_own_reading_only(client):
    room = _room()
    _bind(7, room)
    _bind(8, room)
    day = daily.daily_bounds()[0].strftime("%Y%m%d")
    process_update(_callback(7, f"tk:value:s:{room['id']}:{day}:21", "7send"))
    result = process_update(_callback(8, f"tk:value:c:{room['id']}:{day}:19", "8change"))[1]
    assert "не найдено" in result
    assert db.recent_readings()[0]["value"] == 21


def test_reminder_or_alert_outbox_has_default_buttons(client):
    # Existing alert producer writes a plain outbox row; delivery adds menu.
    room = _room()
    _bind(7, room)
    with db.connect() as conn:
        conn.execute("INSERT INTO max_inbox(event_id,state,updated_at) VALUES('alert','complete',?)", (db.utc_now(),))
        conn.execute("""INSERT INTO max_outbox(event_id,user_id,text,next_retry_at)
                        VALUES('alert','7','Сигнал',?)""", (db.utc_now(),))
    class Client:
        async def post(self, route, **kwargs):
            assert kwargs["json"]["attachments"][0]["type"] == "inline_keyboard"
            class Response:
                def raise_for_status(self):
                    pass
            return Response()
    asyncio.run(max_delivery.drain(Client()))


def test_webhook_subscription_refresh_adds_callback_without_rotating_secret(client):
    from termokontur import settings
    settings.set_secret("max_token", "max-test-token-long-enough-123")
    settings.set_max_webhook("https://school.example/api/max/webhook",
                             "the-existing-school-secret")
    calls = []

    class Response:
        def __init__(self, result):
            self.result = result
        def raise_for_status(self):
            pass
        def json(self):
            return self.result

    class Client:
        async def get(self, route):
            assert route == "/subscriptions"
            return Response({"subscriptions": [{
                "url": "https://school.example/api/max/webhook",
                "update_types": ["message_created", "bot_started"],
            }]})
        async def post(self, route, **kwargs):
            calls.append((route, kwargs))
            return Response({"success": True})

    assert asyncio.run(max_delivery.ensure_webhook_subscription(Client()))
    assert len(calls) == 1
    assert calls[0][0] == "/subscriptions"
    assert calls[0][1]["json"]["secret"] == "the-existing-school-secret"
    assert "message_callback" in calls[0][1]["json"]["update_types"]


def test_webhook_subscription_already_up_to_date_does_not_post(client):
    from termokontur import settings
    settings.set_secret("max_token", "max-test-token-long-enough-123")
    settings.set_max_webhook("https://school.example/api/max/webhook",
                             "another-school-secret")
    class Response:
        def raise_for_status(self):
            pass
        def json(self):
            return {"subscriptions": [{
                "url": "https://school.example/api/max/webhook",
                "update_types": ["message_created", "bot_started", "message_callback"],
            }]}
    class Client:
        async def get(self, route):
            return Response()
        async def post(self, *a, **kw):
            raise AssertionError("No subscription refresh expected")
    assert asyncio.run(max_delivery.ensure_webhook_subscription(Client()))


def test_retention_can_remove_reading_and_its_correction_audit(client):
    room = _room()
    _bind(7, room)
    day = daily.daily_bounds()[0].strftime("%Y%m%d")
    process_update(_callback(7, f"tk:value:s:{room['id']}:{day}:20", "add"))
    process_update(_callback(7, f"tk:value:c:{room['id']}:{day}:21", "edit"))
    reading_id = db.recent_readings()[0]["id"]
    with db.connect() as conn:
        conn.execute("DELETE FROM readings WHERE id=?", (reading_id,))
        assert conn.execute("SELECT count(*) FROM max_reading_corrections").fetchone()[0] == 0
