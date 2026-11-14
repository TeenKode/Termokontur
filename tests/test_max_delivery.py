"""Regression coverage for MAX webhooks, durable queues and replay safety."""
import asyncio
import httpx
from termokontur import db, max_delivery, settings


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
    from termokontur import app as app_module
    monkeypatch.setattr(app_module.httpx, "post", lambda *args, **kwargs: FakeResponse())
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
    monkeypatch.setattr(app_module.httpx,"delete",fake_delete)
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
    from termokontur import app as app_module
    class Response:
        def raise_for_status(self): pass
        def json(self): return {"success":True}
    monkeypatch.setattr(app_module.httpx,"post",lambda *args,**kwargs:Response())
    admin.put("/api/settings/max-token",json={"value":"this-is-a-valid-token-123456789"})
    admin.post("/api/settings/max-webhook",json={"url":"https://school.example/api/max/webhook"})
    import hashlib
    update=message("incomplete-2")
    key=hashlib.sha256(max_delivery.event_key(update).encode()).hexdigest()
    with db.connect() as conn:
        conn.execute("INSERT INTO max_inbox(event_id,state,updated_at) VALUES(?,'processing',?)",(key,db.utc_now()))
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
    from termokontur import app as app_module
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

    monkeypatch.setattr(app_module.httpx, "get", mock_api([url]))
    response = admin.get("/api/max/diagnostics")
    assert response.status_code == 200
    assert response.json()["webhook_registered"] is True
    assert response.json()["token_valid"] is True
    assert response.json()["bot_username"] == "termokontur_test"
    assert "test-max-token" not in response.text
    assert "long-webhook-secret" not in response.text

    monkeypatch.setattr(app_module.httpx, "get", mock_api([]))
    result = admin.get("/api/max/diagnostics").json()
    assert result["webhook_registered"] is False
    assert result["webhook_check_error"] is False

    def unavailable(*args, **kwargs):
        raise httpx.ConnectError("MAX unavailable")
    monkeypatch.setattr(app_module.httpx, "get", unavailable)
    result = admin.get("/api/max/diagnostics").json()
    assert result["webhook_registered"] is None
    assert result["api_check_error"] is True
    assert "MAX unavailable" not in str(result)


def test_max_diagnostics_detects_polling_conflict_and_worker_heartbeat(admin, monkeypatch):
    from termokontur import app as app_module
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
    monkeypatch.setattr(app_module.httpx, "get", fake_get)

    result = admin.get("/api/max/diagnostics").json()
    assert result["worker_running"] is True
    assert result["worker_mode"] == "polling"
    assert result["token_valid"] is True
    assert result["polling_blocked_by_webhook"] is True
    assert result["subscription_count"] == 1

    active.clear()
    assert admin.get("/api/max/diagnostics").json()["polling_blocked_by_webhook"] is False


def test_max_diagnostics_distinguishes_invalid_token(admin, monkeypatch):
    from termokontur import app as app_module
    settings.set_secret("max_token", "test-invalid-token-1234567890")
    def unauthorized(url, **kwargs):
        return httpx.Response(401, request=httpx.Request("GET", url))
    monkeypatch.setattr(app_module.httpx, "get", unauthorized)
    result = admin.get("/api/max/diagnostics").json()
    assert result["token_valid"] is False
    assert result["webhook_registered"] is None
    assert not result["api_check_error"]


def test_replacing_bot_token_resets_old_poll_marker(admin):
    original = "test-token-for-the-first-max-bot-123"
    replacement = "test-token-for-the-next-max-bot-456"
    assert admin.put("/api/settings/max-token", json={"value": original}).status_code == 200
    db.set_state("max_marker", "123456")
    assert admin.put("/api/settings/max-token", json={"value": original}).status_code == 200
    assert db.get_state("max_marker") == "123456"
    assert admin.put("/api/settings/max-token", json={"value": replacement}).status_code == 200
    assert db.get_state("max_marker") == ""


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
