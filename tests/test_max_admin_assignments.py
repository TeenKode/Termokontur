"""Admin-managed MAX staff directory and no-code room assignments."""
from termokontur import db, settings


def room(name):
    return db.create_room("Старшая школа", "2", name, "Кабинет", 18, 24)


def seen(uid=701, first="Ирина", last="Петрова", username="irina_p"):
    db.remember_max_user({
        "user_id": uid, "first_name": first, "last_name": last,
        "username": username, "is_bot": False,
    })


def test_max_user_directory_requires_admin_and_shows_real_profile(client, admin):
    seen()
    response = admin.get("/api/max/users")
    assert response.status_code == 200
    person = response.json()[0]
    assert person["user_id"] == "701"
    assert person["first_name"] == "Ирина"
    assert person["last_name"] == "Петрова"
    assert person["username"] == "irina_p"
    assert person["rooms"] == []
    assert person["seen_at"]
    admin.post("/api/logout")
    assert client.get("/api/max/users").status_code == 401


def test_admin_assigns_several_rooms_without_codes(admin):
    first, second = room("201"), room("202")
    seen()
    url = "/api/max/users/701/rooms"
    result = admin.put(url, json={
        "room_ids": [first["id"], second["id"], first["id"]], "notify": False,
    })
    assert result.status_code == 200, result.text
    assert result.json()["added"] == [first["id"], second["id"]]
    assert result.json()["notification_queued"] is False
    assert {r["id"] for r in db.bound_rooms("701")} == {first["id"], second["id"]}
    directory = admin.get("/api/max/users").json()
    assert {r["id"] for r in directory[0]["rooms"]} == {first["id"], second["id"]}
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM max_outbox").fetchone()[0] == 0
    assert admin.put(url, json={"room_ids": [second["id"]]}).json()["removed"] == [first["id"]]
    assert [r["id"] for r in db.bound_rooms("701")] == [second["id"]]


def test_notify_only_new_assignments_and_never_duplicate(admin):
    first, second = room("301"), room("302")
    seen()
    settings.set_secret("max_token", "test-token-for-max-notices-123456")
    url = "/api/max/users/701/rooms"
    one = admin.put(url, json={"room_ids": [first["id"]], "notify": True})
    assert one.status_code == 200, one.text
    assert one.json()["notification_queued"] is True
    assert one.json()["added"] == [first["id"]]
    two = admin.put(url, json={"room_ids": [first["id"]], "notify": True})
    assert two.status_code == 200
    assert two.json()["added"] == []
    assert two.json()["notification_queued"] is False
    three = admin.put(url, json={"room_ids": [first["id"], second["id"]], "notify": True})
    assert three.json()["added"] == [second["id"]]
    with db.connect() as conn:
        entries = conn.execute("SELECT text FROM max_outbox ORDER BY rowid").fetchall()
        modes = conn.execute("SELECT delivery_mode FROM max_outbox_ui ORDER BY rowid").fetchall()
    assert len(entries) == 2
    assert "301" in entries[0]["text"]
    assert "302" not in entries[0]["text"]
    assert "302" in entries[1]["text"]
    assert "301" not in entries[1]["text"]
    assert "\n" in entries[0]["text"]
    assert [x["delivery_mode"] for x in modes] == ["notice", "notice"]

    removed = admin.put(url, json={"room_ids": [], "notify": True})
    assert removed.json()["notification_queued"] is False
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM max_outbox").fetchone()[0] == 2


def test_invalid_assignments_are_atomic_and_users_must_be_known(admin):
    first = room("401")
    seen()
    url = "/api/max/users/701/rooms"
    assert admin.put(url, json={"room_ids": [first["id"]]}).status_code == 200
    assert admin.put("/api/max/users/999/rooms", json={"room_ids": []}).status_code == 404
    failed = admin.put(url, json={"room_ids": [999999]})
    assert failed.status_code == 422
    assert [r["id"] for r in db.bound_rooms("701")] == [first["id"]]
    assert admin.put(url, json={"room_ids": ["not-a-number"]}).status_code == 422


def test_notification_requires_configured_bot_and_does_not_write(admin):
    first = room("501")
    seen()
    result = admin.put("/api/max/users/701/rooms",
                       json={"room_ids": [first["id"]], "notify": True})
    assert result.status_code == 409
    assert db.bound_rooms("701") == []


def test_inactive_room_bindings_are_preserved(admin):
    first, second = room("601"), room("602")
    seen()
    url = "/api/max/users/701/rooms"
    assert admin.put(url, json={"room_ids": [first["id"], second["id"]]}).status_code == 200
    db.update_room(second["id"], {"active": False})
    response = admin.put(url, json={"room_ids": []})
    assert response.status_code == 200
    assert db.bound_rooms("701") == []
    with db.connect() as conn:
        ids = {r["room_id"] for r in conn.execute(
            "SELECT room_id FROM max_bindings WHERE user_id='701'"
        )}
    assert ids == {second["id"]}
    assert admin.put(url, json={"room_ids": [second["id"]]}).status_code == 422


def test_legacy_bindings_without_profile_show_in_directory(admin):
    first = room("701")
    assert db.bind_code("710", db.issue_code(first["id"]))
    users = admin.get("/api/max/users").json()
    unknown = next(u for u in users if u["user_id"] == "710")
    assert unknown["first_name"] == ""
    assert unknown["rooms"][0]["id"] == first["id"]
