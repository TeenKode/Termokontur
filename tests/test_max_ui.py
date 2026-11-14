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


def test_shared_room_day_one_reading_and_other_employee_can_correct(client):
    room = _room()
    _bind(7, room)
    _bind(8, room)
    day = daily.daily_bounds()[0].strftime("%Y%m%d")
    assert "Сохранено" in process_update(
        _callback(7, f"tk:value:s:{room['id']}:{day}:21", "7send")
    )[1]
    # This is the same room/day, not a new record for a different person.
    duplicate = process_update(_callback(8, f"tk:value:s:{room['id']}:{day}:22", "8send"))[1]
    assert "уже передано 21" in duplicate
    assert len(db.recent_readings()) == 1
    assert "21" in process_update(_callback(8, "tk:send", "8menu"))[1]
    assert "tk:value:c:" + str(room["id"]) in " ".join(_payloads(8))

    changed = process_update(
        _callback(8, f"tk:value:c:{room['id']}:{day}:19", "8change")
    )[1]
    assert "Исправлено" in changed
    stored = db.recent_readings()[0]
    assert stored["value"] == 19
    assert stored["reporter"] == "MAX:7"  # Original author remains.
    assert stored["corrections_count"] == 1
    changes = db.reading_corrections(stored["id"])
    assert len(changes) == 1
    assert changes[0]["user_id"] == "8"
    assert changes[0]["old_value"] == 21
    assert changes[0]["new_value"] == 19
    assert len(db.recent_readings()) == 1
    assert daily.pending_users(daily.summary()) == {}

    # Replay of the same correction must not duplicate the audit.
    assert "уже" in process_update(
        _callback(8, f"tk:value:c:{room['id']}:{day}:19", "8change")
    )[1]
    assert len(db.reading_corrections(stored["id"])) == 1


def test_unbound_person_cannot_change_someone_elses_reading(client):
    room = _room()
    _bind(7, room)
    day = daily.daily_bounds()[0].strftime("%Y%m%d")
    process_update(_callback(7, f"tk:value:s:{room['id']}:{day}:21", "7send"))
    assert "Нет доступа" in process_update(
        _callback(9, f"tk:value:c:{room['id']}:{day}:18", "9change")
    )[1]
    assert db.recent_readings()[0]["value"] == 21
    assert db.reading_corrections(db.recent_readings()[0]["id"]) == []


def test_automatic_reading_is_not_overwritten_through_max(client):
    room = _room()
    _bind(7, room)
    db.add_reading(room["id"], 21, "ha", "sensor-1")
    day = daily.daily_bounds()[0].strftime("%Y%m%d")
    assert "датчиком" in process_update(_callback(7, "tk:send", "status"))[1]
    assert "нельзя" in process_update(
        _callback(7, f"tk:value:c:{room['id']}:{day}:19", "wrong-source")
    )[1]
    assert len(db.recent_readings()) == 1
    assert db.recent_readings()[0]["value"] == 21


def test_legacy_temp_command_uses_same_shared_daily_guard(client):
    from termokontur.bot import interpret
    room = _room()
    _bind(7, room)
    _bind(8, room)
    assert "Сохранено" in interpret("7", "/temp 20", "legacy-one")
    assert "уже передано" in interpret("8", "/temp 22", "legacy-two")
    assert len(db.recent_readings()) == 1
    assert db.recent_readings()[0]["value"] == 20


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


def test_simultaneous_employees_cannot_create_duplicate_daily_max_readings(client):
    from concurrent.futures import ThreadPoolExecutor
    room = _room()
    for uid in (7, 8, 9, 10):
        _bind(uid, room)
    def submit(uid):
        return max_ui._write(str(uid), room["id"], "s", str(18 + uid % 4),
                             f"max:message:concurrent-{uid}")
    with ThreadPoolExecutor(max_workers=4) as executor:
        answers = list(executor.map(submit, (7, 8, 9, 10)))
    assert sum("Сохранено" in reply for reply in answers) == 1
    assert sum("уже передано" in reply for reply in answers) == 3
    assert len(db.recent_readings()) == 1


def test_manual_max_deviation_pushes_once_to_other_assigned_colleagues(client):
    from termokontur import settings
    room = _room()
    _bind(7, room)
    _bind(8, room)
    settings.update({"alerts_enabled": "1"})
    settings.set_secret("max_token", "testing-alerts-to-colleagues-123")
    first = max_ui._write("7", room["id"], "s", "16", "max:message:abnormal-1")
    assert "вне установленной нормы" in first
    queued = max_delivery.pending()
    assert len(queued) == 1
    assert queued[0]["user_id"] == "8"
    assert queued[0]["delivery_mode"] == "notice"
    assert queued[0]["notify"] == 1
    assert "16" in queued[0]["text"]
    assert "\n" in queued[0]["text"]

    # Correcting another person's reading produces at most one notice per
    # recipient and day. The correcting employee sees a direct warning.
    other = max_ui._write("8", room["id"], "c", "17", "max:callback:abnormal-2")
    assert "вне установленной нормы" in other
    assert sorted(x["user_id"] for x in max_delivery.pending()) == ["7", "8"]
    max_ui._write("7", room["id"], "c", "15", "max:callback:abnormal-3")
    assert sorted(x["user_id"] for x in max_delivery.pending()) == ["7", "8"]
