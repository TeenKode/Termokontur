"""Regression coverage for MAX webhooks, durable queues and replay safety."""
import asyncio
import socket
import subprocess
import sys

import httpx
from termokontur import bot, db, max_connection, max_delivery, settings, write_gate


def message(mid, user=123, value="/help"):
    return {
        "update_type": "message_created",
        "timestamp": 1794520000000,
        "message": {
            "sender": {"user_id": user, "is_bot": False},
            "recipient": {"chat_type": "dialog", "user_id": user},
            "body": {"mid": mid, "text": value},
        },
    }


def bot_identity(url, **_):
    assert url.endswith("/me")
    return httpx.Response(200, request=httpx.Request("GET", url), json={"user_id": 7, "is_bot": True})


def test_max_inbox_and_outbox_idempotency(client):
    db.initialize()
    assert max_delivery.enqueue(message("mid-1"))
    assert not max_delivery.enqueue(message("mid-1"))
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM max_inbox").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM max_outbox").fetchone()[0] == 1
    assert max_delivery.pending()[0]["user_id"] == "123"


def test_outbox_survives_delivery_failure(client):
    db.initialize()
    max_delivery.enqueue(message("mid-2"))

    class FailingClient:
        async def post(self, *args, **kwargs):
            raise httpx.ConnectError("offline")
    asyncio.run(max_delivery.drain(FailingClient()))
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM max_outbox").fetchone()
        assert row["attempts"] == 1
        assert row["delivered_at"] is None
        conn.execute("UPDATE max_outbox SET next_retry_at='1970-01-01T00:00:00+00:00'")

    class SuccessClient:
        def __init__(self):
            self.calls=[]
        async def post(self, *args, **kwargs):
            self.calls.append((args,kwargs))
            class Response:
                def raise_for_status(self):
                    pass
            return Response()
    fake=SuccessClient()
    asyncio.run(max_delivery.drain(fake))
    assert len(fake.calls) == 1
    assert not max_delivery.pending()


def test_binding_replay_has_same_outcome_after_processing_failure(client):
    room=db.create_room("Корпус","2","201","Кабинет",18,24)
    code=db.issue_code(room["id"])
    bound=db.bind_code("123",code,"max:message-123")
    replay=db.bind_code("123",code,"max:message-123")
    assert replay==bound
    assert db.bind_code("123",code,"max:different-event") is None
    assert db.bind_code("999",code,"max:message-123") is None


def test_webhook_registration_and_secret(admin, monkeypatch):
    class FakeResponse:
        def raise_for_status(self):
            pass
        def json(self):
            return {"success": True}
    from termokontur.web import max_api
    monkeypatch.setattr(max_api.httpx, "post", lambda *args, **kwargs: FakeResponse())
    monkeypatch.setattr(max_api.httpx, "get", bot_identity)
    url="https://school.example/api/max/webhook"
    assert admin.post("/api/settings/max-webhook",json={"url":url}).status_code == 409
    token="test-token-for-webhook-test-123"
    assert admin.put("/api/settings/max-token",json={"value":token}).status_code==200
    for bad in ("http://school.example/api/max/webhook",
                "https://127.0.0.1/api/max/webhook",
                "https://school.example:8443/api/max/webhook",
                "https://school.example/"):
        assert admin.post("/api/settings/max-webhook",json={"url":bad}).status_code==422
    response=admin.post("/api/settings/max-webhook",json={"url":url})
    assert response.status_code==200,response.text
    assert response.json()["max_mode"]=="webhook"
    cfg=admin.get("/api/settings").json()
    assert cfg["max_webhook_url"]==url
    assert cfg["max_mode"]=="webhook"
    assert "max_webhook_secret" not in cfg
    assert admin.put("/api/settings/max-token",json={"value":"something-else-long-enough"}).status_code==409

    payload=message("test-msg-webhook")
    assert admin.post("/api/max/webhook",json=payload).status_code==403
    assert admin.post("/api/max/webhook",json=payload,
                      headers={"X-Max-Bot-Api-Secret":"wrong"}).status_code==403
    secret=settings.get("max_webhook_secret")
    assert admin.post("/api/max/webhook",json=payload,
                      headers={"X-Max-Bot-Api-Secret":secret}).status_code==200
    assert admin.post("/api/max/webhook",json=payload,
                      headers={"X-Max-Bot-Api-Secret":secret}).status_code==200
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM max_outbox").fetchone()[0]==1

    def fake_delete(*args, **kwargs):
        assert kwargs["params"]["url"]==url
        return FakeResponse()
    monkeypatch.setattr(max_api.httpx,"delete",fake_delete)
    assert admin.post("/api/settings/max-polling",json={}).json()["max_mode"]=="polling"
    assert not settings.get("max_webhook_secret")
    assert admin.post("/api/max/webhook",json=message("not-active"),
                      headers={"X-Max-Bot-Api-Secret":secret}).status_code==403


def test_webhook_rejects_payloads_without_auth(client):
    assert client.post("/api/max/webhook",content=b"{}").status_code==403


def test_unprocessed_event_is_retryable(client, monkeypatch):
    from termokontur import bot
    original=bot.process_update
    def broken(_update):
        raise RuntimeError("transient internal failure")
    monkeypatch.setattr(bot,"process_update",broken)
    import pytest
    with pytest.raises(RuntimeError):
        max_delivery.enqueue(message("retries"))
    monkeypatch.setattr(bot,"process_update",original)
    assert max_delivery.enqueue(message("retries"))


def test_incomplete_event_is_not_acknowledged(client):
    import hashlib
    import pytest
    update=message("incomplete-1")
    key=hashlib.sha256(max_delivery.event_key(update).encode()).hexdigest()
    with db.connect() as conn:
        conn.execute("""INSERT INTO max_inbox(event_id,state,updated_at)
                        VALUES(?,'processing',?)""",(key,db.utc_now()))
    with pytest.raises(max_delivery.ProcessingPending):
        max_delivery.enqueue(update)
    with db.connect() as conn:
        conn.execute("UPDATE max_inbox SET updated_at='2000-01-01T00:00:00+00:00' WHERE event_id=?",(key,))
    assert max_delivery.enqueue(update)
    assert not max_delivery.enqueue(update)
    assert len(max_delivery.pending())==1


def test_webhook_incomplete_event_returns_retryable_status(admin,monkeypatch):
    from termokontur.web import max_api
    class Response:
        def raise_for_status(self): pass
        def json(self): return {"success":True}
    monkeypatch.setattr(max_api.httpx,"post",lambda *args,**kwargs:Response())
    monkeypatch.setattr(max_api.httpx, "get", bot_identity)
    admin.put("/api/settings/max-token",json={"value":"this-is-a-valid-token-123456789"})
    admin.post("/api/settings/max-webhook",json={"url":"https://school.example/api/max/webhook"})
    import hashlib
    update=message("incomplete-2")
    key=hashlib.sha256(max_delivery.event_key(update).encode()).hexdigest()
    with db.connect() as conn:
        conn.execute("INSERT INTO max_inbox(event_id,state,updated_at,bot_scope) VALUES(?,'processing',?,?)",
                     (key, db.utc_now(), max_connection.current(conn).scope))
    secret=settings.get("max_webhook_secret")
    resp=admin.post("/api/max/webhook",json=update,headers={"X-Max-Bot-Api-Secret":secret})
    assert resp.status_code==503



def test_replies_are_not_abandoned_after_ten_failures(client):
    """An extended network outage must not permanently strand a MAX reply."""
    db.initialize()
    max_delivery.enqueue(message("long-outage"))
    with db.connect() as conn:
        conn.execute("""
            UPDATE max_outbox SET attempts=12,
                next_retry_at='1970-01-01T00:00:00+00:00'
        """)
    pending=max_delivery.pending()
    assert len(pending)==1
    assert pending[0]["attempts"]==12
    info=max_delivery.diagnostics()
    assert info["undelivered"]==1
    assert info["repeatedly_failed"]==1
    assert info["ready"]==1
    max_delivery.mark_failed(pending[0]["event_id"],12)
    with db.connect() as conn:
        row=conn.execute("SELECT attempts, delivered_at FROM max_outbox").fetchone()
        assert row["attempts"]==13
        assert row["delivered_at"] is None


def test_max_diagnostics_requires_login(client):
    assert client.get("/api/max/diagnostics").status_code == 401


def test_max_diagnostics_checks_remote_webhook_without_exposing_token(admin, monkeypatch):
    from termokontur.web import max_api
    token = "test-max-token-diagnostics-123456789"
    url = "https://school.example/api/max/webhook"
    settings.set_secret("max_token", token)
    settings.set_max_webhook(url, "long-webhook-secret-1234567890")

    class FakeResponse:
        def __init__(self, payload):
            self.payload = payload
        def raise_for_status(self):
            pass
        def json(self):
            return self.payload

    def mock_api(urls):
        def fake_get(endpoint, **kwargs):
            assert kwargs["headers"]["Authorization"] == token
            if endpoint.endswith("/me"):
                return FakeResponse({"is_bot": True, "username": "termokontur_test"})
            assert endpoint == "https://platform-api2.max.ru/subscriptions"
            return FakeResponse({"subscriptions": [{"url": u} for u in urls]})
        return fake_get

    monkeypatch.setattr(max_api.httpx, "get", mock_api([url]))
    response = admin.get("/api/max/diagnostics")
    assert response.status_code == 200
    assert response.json()["webhook_registered"] is True
    assert response.json()["token_valid"] is True
    assert response.json()["bot_username"] == "termokontur_test"
    assert "test-max-token" not in response.text
    assert "long-webhook-secret" not in response.text

    monkeypatch.setattr(max_api.httpx, "get", mock_api([]))
    result = admin.get("/api/max/diagnostics").json()
    assert result["webhook_registered"] is False
    assert result["webhook_check_error"] is False

    def unavailable(*args, **kwargs):
        raise httpx.ConnectError("MAX unavailable")
    monkeypatch.setattr(max_api.httpx, "get", unavailable)
    result = admin.get("/api/max/diagnostics").json()
    assert result["webhook_registered"] is None
    assert result["api_check_error"] is True
    assert "MAX unavailable" not in str(result)


def test_max_diagnostics_detects_polling_conflict_and_worker_heartbeat(admin, monkeypatch):
    from termokontur.web import max_api
    token = "test-max-token-diagnostics-123456789"
    settings.set_secret("max_token", token)
    db.set_state("max_worker_heartbeat", db.utc_now() + "|polling")

    class Response:
        def __init__(self, payload): self.payload = payload
        def raise_for_status(self): pass
        def json(self): return self.payload

    active = [{"url": "https://another-project.example/webhook"}]
    def fake_get(url, **kwargs):
        if url.endswith("/me"):
            return Response({"is_bot": True, "username": "test_bot"})
        return Response({"subscriptions": active})
    monkeypatch.setattr(max_api.httpx, "get", fake_get)

    result = admin.get("/api/max/diagnostics").json()
    assert result["worker_running"] is True
    assert result["worker_mode"] == "polling"
    assert result["token_valid"] is True
    assert result["polling_blocked_by_webhook"] is True
    assert result["subscription_count"] == 1

    active.clear()
    assert admin.get("/api/max/diagnostics").json()["polling_blocked_by_webhook"] is False


def test_max_diagnostics_distinguishes_invalid_token(admin, monkeypatch):
    from termokontur.web import max_api
    settings.set_secret("max_token", "test-invalid-token-1234567890")
    def unauthorized(url, **kwargs):
        return httpx.Response(401, request=httpx.Request("GET", url))
    monkeypatch.setattr(max_api.httpx, "get", unauthorized)
    result = admin.get("/api/max/diagnostics").json()
    assert result["token_valid"] is False
    assert result["webhook_registered"] is None
    assert not result["api_check_error"]


def test_replacing_bot_token_resets_old_poll_marker(admin):
    original = "test-token-for-the-first-max-bot-123"
    replacement = "test-token-for-the-next-max-bot-456"
    assert admin.put("/api/settings/max-token", json={"value": original}).status_code == 200
    db.set_state("max_marker", "123456")
    max_delivery.set_menu_message_id("777", "old-bot-menu")
    assert admin.put("/api/settings/max-token", json={"value": original}).status_code == 200
    assert db.get_state("max_marker") == "123456"
    assert admin.put("/api/settings/max-token", json={"value": replacement}).status_code == 200
    assert db.get_state("max_marker") == ""
    assert max_delivery.menu_message_id("777") is None


def test_late_poll_response_cannot_change_new_connection(client, monkeypatch):
    old_token = "test-token-for-first-polling-bot"
    new_token = "test-token-for-second-polling-bot"
    settings.set_secret("max_token", old_token)
    old = max_connection.identify(max_connection.current(), {"is_bot": True, "user_id": 1})
    max_connection.poll_result(old, cursor="first-cursor")

    def receive(request):
        assert request.url.path == "/updates"
        assert request.url.params["marker"] == "first-cursor"
        settings.set_secret("max_token", new_token)
        return httpx.Response(200, json={"updates": [message("late-response")], "marker": "late-cursor"})

    actual_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs:
                        actual_client(**kwargs, transport=httpx.MockTransport(receive)))
    asyncio.run(asyncio.wait_for(bot.poll(old_token), timeout=3))

    new = max_connection.identify(max_connection.current(), {"is_bot": True, "user_id": 2})
    assert new.token == new_token and max_connection.marker(new) is None
    assert max_connection.marker(old) == "first-cursor"
    assert max_delivery.diagnostics()["parked_other_bots"] == 0
    assert not db.list_max_users()


def test_outbox_resumes_only_for_same_verified_bot(client):
    settings.set_secret("max_token", "test-original-token-for-bot-one")
    original = max_connection.identify(max_connection.current(), {"is_bot": True, "user_id": 1})
    max_connection.poll_result(original, cursor="bot-one")
    assert max_delivery.enqueue(message("bot-scoped-message"), expected=original)

    settings.set_secret("max_token", "test-original-token-for-bot-two")
    second = max_connection.identify(max_connection.current(), {"is_bot": True, "user_id": 2})
    assert not max_delivery.pending(expected=second)
    assert max_delivery.diagnostics()["parked_other_bots"] == 1
    assert max_delivery.enqueue(message("bot-scoped-message"), expected=second)
    assert len(max_delivery.pending(expected=second)) == 1

    settings.set_secret("max_token", "test-replacement-token-for-bot-one")
    renewed = max_connection.identify(max_connection.current(), {"is_bot": True, "user_id": 1})
    assert max_connection.marker(renewed) == "bot-one"
    assert not max_delivery.enqueue(message("bot-scoped-message"), expected=renewed)
    assert len(max_delivery.pending(expected=renewed)) == 1


def test_worker_reports_ready_with_delivery_paused(tmp_path, monkeypatch):
    monkeypatch.setenv("TERMOKONTUR_DB", str(tmp_path / "termokontur.sqlite3"))
    monkeypatch.delenv("MAX_BOT_TOKEN", raising=False)
    write_gate.pause_for_update()
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as notify:
        address = str(tmp_path / "notify.sock")
        notify.bind(address)
        notify.settimeout(5)
        monkeypatch.setenv("NOTIFY_SOCKET", address)
        with subprocess.Popen([sys.executable, "-m", "termokontur.bot"]) as worker:
            try:
                assert notify.recv(128) == b"READY=1"
                with db.connect() as conn:
                    assert conn.execute("SELECT 1 FROM settings WHERE key='__bootstrapped'").fetchone()
                assert db.get_state("max_worker_heartbeat") is None
            finally:
                worker.terminate()
                worker.wait(timeout=5)


def test_max_webhook_stream_has_strict_size_limit(admin):
    from termokontur import settings
    settings.set_max_webhook(
        "https://school.example/api/max/webhook", "long-webhook-secret-1234567890"
    )
    secret=settings.get("max_webhook_secret")
    response=admin.post("/api/max/webhook", content=b"x"*65537,
                        headers={"X-Max-Bot-Api-Secret":secret})
    assert response.status_code==413
    assert not max_delivery.pending()


def test_temperature_deviation_notifies_all_bound_employees(client):
    room = db.create_room("Старшая школа", "1", "108", "Кабинет", 18, 24)
    for uid in ("7", "8"):
        assert db.bind_code(uid, db.issue_code(room["id"]))
    settings.update({"alerts_enabled": "1"})
    settings.set_secret("max_token", "test-max-token-for-notifications-123")
    # Out-of-range automatic sensor data generate a push to each assigned user.
    # Manual MAX readings show an immediate in-chat warning instead.
    db.add_reading(room["id"], 16.5, "ha", "zigbee:108")
    assert max_delivery.queue_temperature_alerts() == 2
    with db.connect() as conn:
        queued = conn.execute(
            "SELECT user_id, text FROM max_outbox ORDER BY user_id"
        ).fetchall()
    assert [row["user_id"] for row in queued] == ["7", "8"]
    assert all("ниже минимума" in row["text"] for row in queued)
    assert max_delivery.queue_temperature_alerts() == 0  # cooldown


def test_max_permanent_rejection_is_distinguished_from_network_retry(client):
    assert max_delivery.enqueue(message("terminal"))
    class Rejected:
        async def post(self, route, **kwargs):
            return httpx.Response(403, request=httpx.Request("POST","https://example.test/messages"))
    asyncio.run(max_delivery.drain(Rejected()))
    assert max_delivery.pending() == []
    assert max_delivery.diagnostics()["permanently_failed"] == 1
    with db.connect() as conn:
        assert conn.execute("SELECT delivered_at FROM max_outbox").fetchone()[0] is None
    assert max_delivery.enqueue(message("new-after-terminal"))
    assert len(max_delivery.pending()) == 1


def test_max_menu_order_does_not_block_other_users(client):
    for mid, uid in (("first",123),("second",123),("other",456)):
        assert max_delivery.enqueue(message(mid,user=uid))
    initial = max_delivery.pending()
    assert [x["user_id"] for x in initial] == ["123","123","456"]
    max_delivery.mark_failed(initial[0]["event_id"],0)
    assert [x["user_id"] for x in max_delivery.pending()] == ["456"]
    with db.connect() as conn:
        conn.execute("UPDATE max_outbox SET next_retry_at='2000-01-01T00:00:00+00:00'")
    assert [x["event_id"] for x in max_delivery.pending()] == [x["event_id"] for x in initial]


def test_command_and_reply_are_atomic_even_after_effect(client, monkeypatch):
    from termokontur import max_ui
    import pytest
    room = db.create_room("Корпус", "1", "101", "Кабинет", 18, 24)
    code = db.issue_code(room["id"])
    original = max_ui.keyboard
    def broken(_uid):
        raise RuntimeError("failure after command, before reply commit")
    monkeypatch.setattr(max_ui, "keyboard", broken)
    update = message("atomic-bind", value="/bind " + code)
    with pytest.raises(RuntimeError):
        max_delivery.enqueue(update)
    assert not db.bound_rooms("123")
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM max_inbox").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM max_outbox").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM pairing_codes").fetchone()[0] == 1
    monkeypatch.setattr(max_ui, "keyboard", original)
    assert max_delivery.enqueue(update)
    assert not max_delivery.enqueue(update)
    assert len(db.bound_rooms("123")) == 1

    update = message("atomic-reading", value="/temp 21,5")
    monkeypatch.setattr(max_ui, "keyboard", broken)
    with pytest.raises(RuntimeError):
        max_delivery.enqueue(update)
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM readings WHERE source='max'").fetchone()[0] == 0
    monkeypatch.setattr(max_ui, "keyboard", original)
    assert max_delivery.enqueue(update)
    assert not max_delivery.enqueue(update)
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM readings WHERE source='max'").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM max_outbox").fetchone()[0] == 2


def test_concurrent_duplicate_command_commits_once(client):
    from concurrent.futures import ThreadPoolExecutor
    update = message("concurrent-atomic")
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(max_delivery.enqueue, [update] * 8))
    assert results.count(True) == 1
    assert len(max_delivery.pending()) == 1
