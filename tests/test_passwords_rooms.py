"""Regression tests: relaxed password policy and room management."""
import pytest
from fastapi.testclient import TestClient

from termokontur import auth, db
from termokontur.app import app


def test_eight_characters_and_plain_digits_are_allowed(admin):
    assert admin.post("/api/settings/password", json={
        "current_password": "this-is-a-test-admin-password",
        "new_password": "12345678",
    }).status_code == 200
    assert admin.post("/api/login", json={"password": "12345678"}).status_code == 200
    assert auth.check_password("12345678")
    assert not auth.check_password("this-is-a-test-admin-password")


def test_seven_characters_still_rejected(admin):
    assert admin.post("/api/settings/password", json={
        "current_password": "this-is-a-test-admin-password",
        "new_password": "abc1234",
    }).status_code == 422
    assert auth.check_password("this-is-a-test-admin-password")


def test_eight_character_bootstrap(monkeypatch, tmp_path):
    monkeypatch.setenv("TERMOKONTUR_DB", str(tmp_path / "bootstrap.sqlite3"))
    monkeypatch.setenv("TERMOKONTUR_ADMIN_PASSWORD", "abcdefgh")
    with TestClient(app) as client:
        assert client.post("/api/login", json={"password": "abcdefgh"}).status_code == 200


def test_room_archive_reactivate_and_export(admin):
    room=admin.post("/api/rooms",json={
      "building": "=test", "floor":"2", "name":"Кабинет 201",
      "kind":"Кабинет", "min_temp":18, "max_temp":24
    }).json()
    assert admin.post("/api/readings",json={"room_id":room["id"],"value":21}).status_code==201
    assert admin.patch(f"/api/rooms/{room['id']}",json={"active":False}).status_code==200
    assert admin.get("/api/rooms").json()==[]
    assert len(admin.get("/api/rooms?include_inactive=true").json())==1
    exported=admin.get("/api/rooms/export.csv")
    assert exported.status_code==200
    assert "'=test" in exported.text
    assert "Отключён" in exported.text
    assert admin.patch(f"/api/rooms/{room['id']}",json={"active":True}).status_code==200
    assert len(admin.get("/api/rooms").json())==1
    assert admin.get("/api/readings").json()[0]["room_id"]==room["id"]


def test_room_export_rejects_anonymous(client):
    assert client.get("/api/rooms/export.csv").status_code == 401
