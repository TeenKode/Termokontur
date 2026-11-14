"""MAX profile labels, admin unbinding and safe pairing-code management."""
import csv
import io

from termokontur import db
from termokontur.bot import process_update


def room(admin, number):
    response = admin.post("/api/rooms", json={
        "building": "Старшая школа", "floor": "2", "name": f"Кабинет {number}",
        "kind": "Кабинет", "min_temp": 18, "max_temp": 24,
    })
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_profile_from_max_private_updates_and_partial_fields(admin):
    rid = room(admin, 201)
    code = db.issue_code(rid)
    event = {"update_type": "bot_started", "timestamp": 123456,
             "user": {"user_id": 1001, "first_name": "Иван",
                      "last_name": "Петров", "username": "i_petrov"}}
    assert process_update(event) is not None
    assert db.bind_code("1001", code) is not None

    users = admin.get("/api/rooms").json()[0]["max_users"]
    assert users == [{"user_id": "1001", "first_name": "Иван",
                      "last_name": "Петров", "username": "i_petrov"}]

    # MAX may omit optional profile fields; a partial update must not erase them.
    msg = {"update_type": "message_created", "message": {
        "sender": {"user_id": 1001, "first_name": "Иван"},
        "recipient": {"chat_type": "dialog"},
        "body": {"mid": "m10", "text": "/rooms"},
    }}
    assert process_update(msg) is not None
    assert admin.get("/api/rooms").json()[0]["max_users"] == users

    callback = {"update_type": "message_callback",
                "callback": {"user": {"user_id": 1001, "first_name": "Иван",
                                     "last_name": "Сидоров"},
                             "callback_id": "cb1", "payload": "tk:home"},
                "message": {"recipient": {"chat_type": "dialog"}}}
    assert process_update(callback) is not None
    assert admin.get("/api/rooms").json()[0]["max_users"][0]["last_name"] == "Сидоров"

    # Never store profile information from a public/group chat.
    msg["message"]["sender"] = {"user_id": 9001, "first_name": "Не сохранять"}
    msg["message"]["recipient"] = {"chat_type": "chat"}
    assert process_update(msg) is None
    with db.connect() as conn:
        assert conn.execute("SELECT 1 FROM max_users WHERE user_id='9001'").fetchone() is None


def test_admin_room_list_multiple_assignments_and_unbinding(admin, client):
    rid = room(admin, 202)
    other = room(admin, 203)
    db.remember_max_user({"user_id": 11, "first_name": "Анна", "last_name": "Иванова"})
    assert db.bind_code("11", db.issue_code(rid))
    assert db.bind_code("22", db.issue_code(rid))
    assert db.bind_code("11", db.issue_code(other))

    rooms = admin.get("/api/rooms").json()
    target = next(r for r in rooms if r["id"] == rid)
    assert len(target["max_users"]) == 2
    assert target["max_users"][0]["first_name"] == "Анна"
    assert target["max_users"][1]["user_id"] == "22"
    assert target["max_users"][1]["first_name"] == ""

    removed = admin.delete(f"/api/rooms/{rid}/max-bindings/11")
    assert removed.status_code == 200
    assert removed.json() == {"removed": True}
    assert db.bound_rooms("11")[0]["id"] == other
    target = next(r for r in admin.get("/api/rooms").json() if r["id"] == rid)
    assert [u["user_id"] for u in target["max_users"]] == ["22"]
    assert admin.delete(f"/api/rooms/{rid}/max-bindings/11").status_code == 404
    assert admin.delete("/api/rooms/999999/max-bindings/11").status_code == 404

    exported = admin.get("/api/rooms/export.csv")
    lines = list(csv.DictReader(io.StringIO(exported.text.lstrip("\ufeff")), delimiter=";"))
    assert "Пользователи MAX" in lines[0]
    assert any(line["Кабинет"] == "Кабинет 203" and
               "Анна Иванова" in line["Пользователи MAX"] for line in lines)

    assert admin.post("/api/logout").status_code == 200
    assert client.get("/api/rooms").status_code == 401
    assert client.delete(f"/api/rooms/{rid}/max-bindings/22").status_code == 401
    assert db.bound_rooms("22")[0]["id"] == rid


def test_active_codes_are_only_metadata_and_can_be_revoked(admin, client):
    first, second = room(admin, 301), room(admin, 302)
    assert admin.get("/api/max/pairing-codes").json() == []
    issued = admin.post("/api/max/pairing-codes", json={
        "room_ids": [second, first], "validity_minutes": 120,
    })
    assert issued.status_code == 200
    assert "no-store" in issued.headers["cache-control"]
    created = issued.json()["codes"]
    assert len(created) == 2
    assert all(len(item["code"]) == 16 for item in created)

    listed = admin.get("/api/max/pairing-codes")
    assert listed.status_code == 200
    assert "no-store" in listed.headers["cache-control"]
    entries = listed.json()
    assert [r["room_id"] for r in entries] == [first, second]
    assert all("code" not in item and "digest" not in item for item in entries)
    assert all("expires_at" in item for item in entries)
    assert all(item["code"] not in listed.text for item in created)

    first_code = next(x["code"] for x in created if x["id"] == first)
    second_code = next(x["code"] for x in created if x["id"] == second)
    assert admin.delete(f"/api/rooms/{first}/pairing").json() == {"revoked": True}
    assert admin.delete(f"/api/rooms/{first}/pairing").json() == {"revoked": False}
    assert admin.delete("/api/rooms/999999/pairing").status_code == 404
    assert db.bind_code("bad", first_code) is None
    assert db.bind_code("ok", second_code) is not None
    assert admin.get("/api/max/pairing-codes").json() == []

    assert admin.post("/api/logout").status_code == 200
    assert client.get("/api/max/pairing-codes").status_code == 401
    assert client.post("/api/max/pairing-codes", json={"room_ids": [first]}).status_code == 401
    assert client.delete(f"/api/rooms/{first}/pairing").status_code == 401


def test_room_template_exposes_max_management_controls():
    from pathlib import Path
    html = (Path(__file__).resolve().parents[1] / "termokontur/static/index.html").read_text()
    js = (Path(__file__).resolve().parents[1] / "termokontur/static/app.js").read_text()
    for element in ("max-codes-issued", "max-active-codes-list", "max-codes-unbound",
                    "max-codes-no-code", "max-codes-download-issued", "max-codes-copy-all"):
        assert f'id="{element}"' in html
        assert element in js
