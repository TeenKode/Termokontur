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
