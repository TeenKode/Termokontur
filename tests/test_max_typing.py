"""Native MAX typing indicator: best-effort and no extra chat messages."""
import asyncio

import httpx

from termokontur import max_delivery


def _message(mid: str, chat_id: object = 777) -> dict:
    return {
        "update_type": "message_created",
        "message": {
            "sender": {"user_id": 12, "is_bot": False},
            "recipient": {"chat_type": "dialog", "chat_id": chat_id},
            "body": {"mid": mid, "text": "/help"},
        },
    }


class RecordingClient:
    def __init__(self, typing_status=200, typing_success=True):
        self.calls = []
        self.typing_status = typing_status
        self.typing_success = typing_success

    async def post(self, route, **kwargs):
        self.calls.append(("post", route, kwargs))
        if route.endswith("/actions"):
            return httpx.Response(
                self.typing_status, json={"success": self.typing_success},
                request=httpx.Request("POST", "https://platform-api2.max.ru" + route),
            )
        assert route in ("/messages", "/answers")
        return httpx.Response(
            200, json={"message": {"body": {"mid": "menu-12"}}},
            request=httpx.Request("POST", "https://platform-api2.max.ru" + route),
        )

    async def put(self, route, **kwargs):
        self.calls.append(("put", route, kwargs))
        return httpx.Response(
            200, json={"success": True},
            request=httpx.Request("PUT", "https://platform-api2.max.ru" + route),
        )


def test_typing_is_sent_before_regular_reply(client):
    assert max_delivery.enqueue(_message("typing-initial"))
    sender = RecordingClient()
    asyncio.run(max_delivery.drain(sender))
    assert [(method, route) for method, route, _ in sender.calls] == [
        ("post", "/chats/777/actions"),
        ("post", "/messages"),
    ]
    assert sender.calls[0][2]["json"] == {"action": "typing_on"}
    assert max_delivery.menu_message_id("12") == "menu-12"
    assert not max_delivery.pending()


def test_typing_before_callback_edit_and_no_duplicate_message(client):
    max_delivery.set_menu_message_id("12", "menu-12")
    update = {
        "update_type": "message_callback",
        "callback": {
            "callback_id": "typing-callback",
            "payload": "tk:home",
            "user": {"user_id": 12, "is_bot": False},
        },
        "message": {
            "recipient": {"chat_type": "dialog", "chat_id": 777},
            "body": {"mid": "menu-12"},
        },
    }
    assert max_delivery.enqueue(update)
    sender = RecordingClient()
    asyncio.run(max_delivery.drain(sender))
    assert [(method, route) for method, route, _ in sender.calls] == [
        ("post", "/chats/777/actions"),
        ("post", "/answers"),
    ]
    assert sender.calls[1][2]["params"]["callback_id"] == "typing-callback"
    assert not max_delivery.pending()


def test_typing_failure_never_blocks_delivery(client):
    assert max_delivery.enqueue(_message("typing-rejected"))
    sender = RecordingClient(typing_status=403)
    asyncio.run(max_delivery.drain(sender))
    assert [entry[1] for entry in sender.calls] == [
        "/chats/777/actions", "/messages",
    ]
    assert not max_delivery.pending()


def test_typing_success_false_does_not_block_delivery(client):
    assert max_delivery.enqueue(_message("typing-success-false"))
    sender = RecordingClient(typing_success=False)
    asyncio.run(max_delivery.drain(sender))
    assert len(sender.calls) == 2
    assert not max_delivery.pending()


def test_missing_or_invalid_dialog_chat_id_skips_typing(client):
    assert max_delivery.enqueue(_message("typing-invalid-id", chat_id="unknown"))
    sender = RecordingClient()
    asyncio.run(max_delivery.drain(sender))
    assert [entry[1] for entry in sender.calls] == ["/messages"]
    assert not max_delivery.pending()
