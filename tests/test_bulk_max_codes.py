"""Bulk MAX pairing code CSV: permissions, atomicity and one-time codes."""
import csv
import io
from datetime import datetime, timedelta, timezone

from termokontur import db


def create_room(admin, number, *, building="Корпус"):
    response = admin.post("/api/rooms", json={
        "building": building, "floor": "1",
        "name": f"Кабинет {number}", "kind": "Кабинет",
        "min_temp": 18, "max_temp": 24,
    })
    assert response.status_code == 201, response.text
    return response.json()


def csv_rows(response):
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/csv")
    assert "no-store" in response.headers["cache-control"]
    assert response.content.startswith(b"\xef\xbb\xbf")
    return list(csv.DictReader(io.StringIO(response.text.lstrip("\ufeff")), delimiter=";"))


def test_mass_export_is_admin_only(client, admin):
    room = create_room(admin, "101")
    admin.post("/api/logout")
    response = client.post("/api/max/pairing-codes.csv",
                           json={"room_ids":[room["id"]],"validity_minutes":30})
    assert response.status_code == 401
    assert not db.bind_code("visitor", "not-a-code")


def test_mass_issue_csv_and_one_time_binding(admin):
    first=create_room(admin, "201")
    second=create_room(admin, "202")
    codes = csv_rows(admin.post("/api/max/pairing-codes.csv", json={
        "room_ids": [second["id"],first["id"]], "validity_minutes":1440,
    }))
    assert len(codes) == 2
    assert [line["Кабинет"] for line in codes] == ["Кабинет 201", "Кабинет 202"]
    assert all(line["Команда боту"] == "/bind " + line["Код MAX"] for line in codes)
    assert all(len(line["Код MAX"]) == 16 for line in codes)
    assert len({line["Код MAX"] for line in codes}) == 2
    for line in codes:
        expires=datetime.fromisoformat(line["Действует до (UTC)"])
        assert timedelta(hours=23,minutes=58) <= expires-datetime.now(timezone.utc) <= timedelta(days=1)
        with db.connect() as conn:
            codes_in_db=[r["digest"] for r in conn.execute("SELECT digest FROM pairing_codes")]
        assert line["Код MAX"] not in codes_in_db
        bound=db.bind_code("teacher"+line["ID кабинета"],line["Код MAX"])
        assert bound is not None
        assert bound["room_id"] == int(line["ID кабинета"])
        assert db.bind_code("another"+line["ID кабинета"],line["Код MAX"]) is None


def test_reissue_invalidates_old_codes(admin):
    room=create_room(admin,"104")
    old_code=db.issue_code(room["id"])
    batch=csv_rows(admin.post("/api/max/pairing-codes.csv",json={
        "room_ids":[room["id"]], "validity_minutes":120,
    }))
    new_code=batch[0]["Код MAX"]
    assert old_code != new_code
    assert db.bind_code("teacher", old_code) is None
    assert db.bind_code("teacher", new_code)["room_id"] == room["id"]
    after_issue=db.issue_code(room["id"])
    assert after_issue != new_code


def test_invalid_room_selection_does_not_revoke_existing_code(admin):
    first=create_room(admin,"301")
    disabled=create_room(admin,"302")
    old=db.issue_code(first["id"])
    assert admin.patch(f"/api/rooms/{disabled['id']}",json={"active":False}).status_code==200
    response=admin.post("/api/max/pairing-codes.csv",json={
        "room_ids":[first["id"], disabled["id"]], "validity_minutes":1440,
    })
    assert response.status_code == 422
    assert db.bind_code("teacher",old)["room_id"] == first["id"]


def test_mass_export_validation_and_excel_escape(admin):
    room=create_room(admin, "401", building="=HYPERLINK(\"https://example.invalid\")")
    assert admin.post("/api/max/pairing-codes.csv",
                      json={"room_ids":[room["id"],room["id"]]}).status_code==422
    assert admin.post("/api/max/pairing-codes.csv",
                      json={"room_ids":[room["id"]],"validity_minutes":999}).status_code==422
    assert admin.post("/api/max/pairing-codes.csv",json={"room_ids":[]}).status_code==422
    assert admin.post("/api/max/pairing-codes.csv",
                      json={"room_ids":[room["id"]]*251}).status_code==422
    rows=csv_rows(admin.post("/api/max/pairing-codes.csv",json={
        "room_ids":[room["id"]],"validity_minutes":30,
    }))
    assert rows[0]["Корпус"].startswith("'=HYPERLINK(")
