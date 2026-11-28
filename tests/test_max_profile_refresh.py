"""MAX profile recovery for users who were bound before names were stored."""
import httpx
from termokontur import db, max_connection, settings
from termokontur.web import max_api


def remember(uid, first="", last="", username="", name=""):
    db.remember_max_user({
        "user_id": uid, "first_name": first, "last_name": last,
        "username": username, "name": name, "is_bot": False,
    })


def record_chat(uid: str, chat_id: str = "4321"):
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO max_inbox(event_id,state,updated_at,bot_scope) VALUES (?,'complete',?,?)",
            ("profile-event-" + uid, db.utc_now(), max_connection.current(conn).scope),
        )
        conn.execute(
            "INSERT INTO max_outbox(event_id,user_id,text,next_retry_at) VALUES (?,?,?,?)",
            ("profile-event-" + uid, uid, "Привет", db.utc_now()),
        )
        conn.execute(
            "INSERT INTO max_outbox_ui(event_id,chat_id) VALUES (?,?)",
            ("profile-event-" + uid, chat_id),
        )


def test_bound_user_refreshes_name_without_changing_rooms(admin, monkeypatch):
    room = db.create_room("Старшая школа", "", "102", "Кабинет", 18, 24)
    uid = "4567"
    assert db.bind_code(uid, db.issue_code(room["id"]))
    settings.set_secret("max_token", "test-max-token-987654321")
    record_chat(uid)

    def fake_get(url, **kwargs):
        assert url == "https://platform-api2.max.ru/chats/4321"
        assert kwargs["headers"]["Authorization"] == "test-max-token-987654321"
        return httpx.Response(200, json={
            "chat_id": 4321, "type": "dialog",
            "dialog_with_user": {
                "user_id": 4567, "first_name": "Мария",
                "last_name": "Иванова", "username": "maria_ivanova",
                "is_bot": False,
            },
        }, request=httpx.Request("GET", url))

    monkeypatch.setattr(max_api.httpx, "get", fake_get)
    response = admin.post("/api/max/users/4567/refresh")
    assert response.status_code == 200, response.text
    profile = response.json()["profile"]
    assert profile["first_name"] == "Мария"
    assert profile["last_name"] == "Иванова"
    assert profile["username"] == "maria_ivanova"
    assert db.bound_rooms(uid)[0]["id"] == room["id"]


def test_refresh_needs_verified_dialog(admin):
    remember("4567")
    result = admin.post("/api/max/users/4567/refresh")
    assert result.status_code == 409
    assert "/start" in result.json()["detail"]


def test_refresh_is_admin_only(client):
    assert client.post("/api/max/users/4567/refresh").status_code == 401


def test_refresh_rejects_mismatched_identity(admin, monkeypatch):
    remember("4567")
    settings.set_secret("max_token", "test-max-token-987654321")
    record_chat("4567")
    def fake_get(url, **kwargs):
        return httpx.Response(200, json={
            "type": "dialog", "dialog_with_user": {
                "user_id": 12345, "first_name": "Чужой",
            },
        }, request=httpx.Request("GET", url))
    monkeypatch.setattr(max_api.httpx, "get", fake_get)
    response = admin.post("/api/max/users/4567/refresh")
    assert response.status_code == 502
    assert db.list_max_users()[0]["first_name"] == ""


def test_refresh_max_api_failure_is_safe(admin, monkeypatch):
    remember("4567")
    settings.set_secret("max_token", "test-max-token-987654321")
    record_chat("4567")
    def fake_get(*args, **kwargs):
        raise httpx.ConnectError("PRIVATE NETWORK DETAILS")
    monkeypatch.setattr(max_api.httpx, "get", fake_get)
    response = admin.post("/api/max/users/4567/refresh")
    assert response.status_code == 502
    assert "PRIVATE NETWORK DETAILS" not in response.text


def test_profile_name_fallback_and_empty_events_do_not_wipe_name(client):
    remember("4567", name="Александра Волкова")
    assert db.list_max_users()[0]["first_name"] == "Александра Волкова"
    remember("4567")
    assert db.list_max_users()[0]["first_name"] == "Александра Волкова"
