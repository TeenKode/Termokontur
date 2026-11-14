from termokontur import db

def room_payload(**kwargs):
    data = {"building":"Старшая школа","floor":"2","name":"Кабинет 201",
            "kind":"Кабинет","min_temp":18,"max_temp":24}
    data.update(kwargs)
    return data

def test_auth_and_cookie(client):
    assert client.get("/api/rooms").status_code == 401
    assert client.post("/api/login",json={"password":"incorrect"}).status_code == 401
    assert client.post("/api/login",json={"password":"this-is-a-test-admin-password"}).status_code == 200
    assert client.get("/api/rooms").status_code == 200
    assert client.post("/api/logout").status_code == 200
    assert client.get("/api/rooms").status_code == 401

def test_room_readings_and_dashboard(admin):
    created = admin.post("/api/rooms",json=room_payload())
    assert created.status_code == 201, created.text
    room = created.json()
    assert admin.post("/api/rooms",json=room_payload()).status_code == 409
    assert admin.get("/api/dashboard").json()["counts"]["missing"] == 1
    reading = admin.post("/api/readings",json={"room_id":room["id"],"value":17.5})
    assert reading.status_code == 201, reading.text
    assert admin.get("/api/dashboard").json()["counts"]["below"] == 1
    assert len(admin.get("/api/readings").json()) == 1
    assert admin.patch("/api/rooms/"+str(room["id"]),json={"min_temp":17}).status_code == 200
    assert admin.get("/api/dashboard").json()["counts"]["normal"] == 1
    assert admin.get("/api/export.csv").status_code == 200

def test_reject_bad_inputs_and_injection(admin):
    assert admin.post("/api/rooms",json=room_payload(min_temp=24,max_temp=18)).status_code == 422
    assert admin.post("/api/rooms",json=room_payload(name="   ")).status_code == 422
    room = admin.post("/api/rooms",json=room_payload(building="=1+1")).json()
    assert admin.post("/api/readings",json={"room_id":room["id"],"value":999}).status_code == 422
    assert admin.patch("/api/rooms/"+str(room["id"]),json={"max_temp":14}).status_code == 422
    admin.post("/api/readings",json={"room_id":room["id"],"value":20})
    assert "'=1+1" in admin.get("/api/export.csv").text

def test_ingest_authorization_and_dedup(admin):
    rid=admin.post("/api/rooms",json=room_payload()).json()["id"]
    path="/api/ingest"
    data={"room_id":rid,"value":22.1,"device":"zigbee:test","event_id":"r1"}
    assert admin.post(path,json=data).status_code == 401
    assert admin.post(path,json=data,headers={"X-Api-Key":"bad"}).status_code == 401
    headers={"X-Api-Key":"this-is-a-long-device-ingest-key-1234567890"}
    assert admin.post(path,json=data,headers=headers).json()["inserted"]
    assert admin.post(path,json=data,headers=headers).json()["inserted"] is False
    assert len(db.recent_readings()) == 1

def test_pairing_and_disabled_room(admin):
    rid=admin.post("/api/rooms",json=room_payload()).json()["id"]
    code=admin.post(f"/api/rooms/{rid}/pairing").json()["code"]
    assert db.bind_code("12345",code)["room_id"] == rid
    assert db.bind_code("98765",code) is None
    assert db.bound_rooms("12345")[0]["id"] == rid
    assert admin.patch(f"/api/rooms/{rid}",json={"active":False}).status_code == 200
    assert admin.post("/api/readings",json={"room_id":rid,"value":22}).status_code == 422
    assert admin.get("/api/dashboard").json()["rooms"] == []

def test_example_device_key_is_disabled(admin, monkeypatch):
    rid = admin.post("/api/rooms", json=room_payload()).json()["id"]
    from termokontur import settings
    settings.set_secret("ingest_key", None)
    response = admin.post("/api/ingest",
                          json={"room_id":rid,"value":21},
                          headers={"X-Api-Key":"REPLACE_WITH_A_LONG_RANDOM_DEVICE_KEY"})
    assert response.status_code == 503

def test_example_admin_secrets_cannot_start(monkeypatch, tmp_path):
    import pytest
    from fastapi.testclient import TestClient
    from termokontur.app import app
    monkeypatch.setenv("TERMOKONTUR_DB", str(tmp_path / "db.sqlite3"))
    monkeypatch.setenv("TERMOKONTUR_ADMIN_PASSWORD", "REPLACE_WITH_A_LONG_RANDOM_PASSWORD")
    monkeypatch.setenv("TERMOKONTUR_SECRET", "REPLACE_WITH_AT_LEAST_32_RANDOM_CHARACTERS")
    with pytest.raises(RuntimeError):
        with TestClient(app):
            pass

def test_daily_requires_auth_and_counts(admin, client, monkeypatch):
    from termokontur import settings
    settings.update({"timezone": "UTC"})
    assert client.get("/api/daily").status_code == 200  # same authenticated client
    room_id = admin.post("/api/rooms", json=room_payload()).json()["id"]
    before = admin.get("/api/daily").json()
    assert before["counts"]["missing"] == 1
    assert before["counts"]["measured"] == 0
    admin.post("/api/readings", json={"room_id":room_id,"value":17.5})
    after = admin.get("/api/daily").json()
    assert after["counts"] == {"measured":1,"missing":0,"out_of_range":1}

def test_change_password_and_revoke_sessions(admin):
    other_cookie = admin.cookies.get("tk_session")
    assert admin.post("/api/settings/password",json={
        "current_password":"wrong",
        "new_password":"replacement-password-1234"}).status_code == 403
    assert admin.post("/api/settings/password",json={
        "current_password":"this-is-a-test-admin-password",
        "new_password":"replacement-password-1234"}).status_code == 200
    assert admin.get("/api/rooms").status_code == 401
    assert admin.post("/api/login",json={"password":"this-is-a-test-admin-password"}).status_code == 401
    assert admin.post("/api/login",json={"password":"replacement-password-1234"}).status_code == 200
    from termokontur.app import app
    from fastapi.testclient import TestClient
    with TestClient(app) as second:
        second.cookies.set("tk_session",other_cookie)
        assert second.get("/api/rooms").status_code == 401


def test_manage_runtime_settings_and_secrets(admin, monkeypatch):
    cfg = admin.get("/api/settings")
    assert cfg.status_code == 200
    assert "ingest_key" not in cfg.json()
    assert "this-is-a-long-device-ingest-key-1234567890" not in cfg.text
    payload = cfg.json()
    payload = {k:v for k,v in payload.items() if not k.endswith("_configured")}
    payload.update({"organization_name":"ЦО Открытие",
                    "timezone":"Asia/Vladivostok","stale_hours":12,
                    "reminders_enabled":True, "reminder_time":"09:15",
                    "reminder_days":[0,1,2,3,4],"reminder_window_minutes":60})
    response=admin.put("/api/settings",json=payload)
    assert response.status_code == 200,response.text
    assert response.json()["organization_name"] == "ЦО Открытие"
    payload["timezone"]="Invalid/Foo"
    assert admin.put("/api/settings",json=payload).status_code == 422
    from termokontur import settings
    settings.set_secret("max_token","a-valid-token-very-long-12345")
    assert admin.get("/api/settings").json()["max_token_configured"]
    assert "a-valid-token-very-long-12345" not in admin.get("/api/settings").text
    admin.put("/api/settings/max-token",json={"value":None})
    assert not admin.get("/api/settings").json()["max_token_configured"]
    settings.bootstrap()
    assert not admin.get("/api/settings").json()["max_token_configured"]


def test_bulk_add_rooms(admin):
    payload={"building":"Старшая школа","floor":"2","kind":"Кабинет",
             "min_temp":18,"max_temp":24,"names":["Кабинет 201","Кабинет 202","Кабинет 203"]}
    resp=admin.post("/api/rooms/bulk",json=payload)
    assert resp.status_code==200,resp.text
    assert resp.json()["created"]==3
    second=admin.post("/api/rooms/bulk",json=payload).json()
    assert second["created"]==0 and len(second["skipped"])==3
    assert len(admin.get("/api/rooms").json())==3
    payload["names"]=["Повтор","Повтор"]
    assert admin.post("/api/rooms/bulk",json=payload).status_code==422
    payload["names"]=[""]
    assert admin.post("/api/rooms/bulk",json=payload).status_code==422


def test_rotate_ingest_key(admin):
    room=admin.post("/api/rooms",json=room_payload()).json()
    old="this-is-a-long-device-ingest-key-1234567890"
    rotated=admin.post("/api/settings/ingest-key").json()["key"]
    assert len(rotated)>20 and rotated!=old
    payload={"room_id":room["id"],"value":21}
    assert admin.post("/api/ingest",json=payload,headers={"X-Api-Key":old}).status_code==401
    assert admin.post("/api/ingest",json=payload,headers={"X-Api-Key":rotated}).status_code==200


def test_update_endpoint_is_protected_and_unsupported(admin):
    assert admin.get("/api/update/status").status_code==200
    assert not admin.get("/api/update/status").json()["supported"]
    assert admin.post("/api/update/request",json={}).status_code==409


def test_native_update_request_without_root_commands(admin, monkeypatch, tmp_path):
    from termokontur import updates, db
    systemd_path = tmp_path / "termokontur-update.path"
    systemd_path.write_text("[Path]\nPathExists=/var/lib/termokontur/update-request\n")
    monkeypatch.setattr(updates, "SYSTEMD_PATH", systemd_path)
    (db.db_path().parent / "installed-revision").write_text("a"*40+"\n")
    current = admin.get("/api/update/status").json()
    assert current["supported"]
    assert current["installed_revision"] == "a"*40

    class FakeGitHubResponse:
        def raise_for_status(self):
            pass
        def json(self):
            return {"sha": "b"*40}

    monkeypatch.setattr(updates.httpx, "get", lambda *a, **kw: FakeGitHubResponse())
    checked = admin.get("/api/update/check")
    assert checked.status_code == 200
    assert checked.json()["available"]
    first = admin.post("/api/update/request", json={})
    assert first.status_code == 200
    assert (db.db_path().parent/"update-request").read_text().strip() == "main"
    assert admin.post("/api/update/request", json={}).status_code == 409
    assert admin.get("/api/update/status").json()["state"] == "queued"



def test_update_status_reports_version_progress_and_stale_result(admin, monkeypatch, tmp_path):
    import json
    import os
    from termokontur import __version__, updates, db

    unit = tmp_path / "termokontur-update.path"
    unit.write_text("[Path]\nPathExists=/var/lib/termokontur/update-request\n")
    monkeypatch.setattr(updates, "SYSTEMD_PATH", unit)
    data = db.db_path().parent
    (data / "installed-revision").write_text("a" * 40 + "\n")
    event = data / "update-status.json"
    event.write_text(json.dumps({
        "state": "installing", "phase": "dependencies",
        "message": "Установка Python-зависимостей…"
    }), encoding="utf-8")
    info = admin.get("/api/update/status").json()
    assert info["state"] == "installing"
    assert info["phase"] == "dependencies"
    assert info["installed_version"] == __version__
    assert info["installed_revision"] == "a" * 40
    assert info["updated_at"]
    assert "Python" in info["message"]
    assert admin.post("/api/update/request", json={}).status_code == 409

    (data / "update-request").touch()
    assert admin.get("/api/update/status").json()["state"] == "queued"
    (data / "update-request").unlink()

    monkeypatch.setattr(updates, "STALLED_SECONDS", 10)
    os.utime(event, (0, 0))
    stalled = admin.get("/api/update/status").json()
    assert stalled["state"] == "failed"
    assert stalled["phase"] == "failed"
    assert "журнал" in stalled["message"]
    assert admin.post("/api/update/request", json={}).status_code == 200


def test_remote_version_is_pinned_to_checked_commit(admin, monkeypatch, tmp_path):
    from termokontur import updates, db, __version__

    unit = tmp_path / "termokontur-update.path"
    unit.write_text("[Path]\n")
    monkeypatch.setattr(updates, "SYSTEMD_PATH", unit)
    (db.db_path().parent / "installed-revision").write_text("a" * 40 + "\n")
    requested_urls = []

    class Response:
        def __init__(self, url):
            self.url = url
            self.text = '__version__ = "0.2.0"\n'
        def raise_for_status(self):
            pass
        def json(self):
            return {"sha": "b" * 40}

    def fake_get(url, **kwargs):
        requested_urls.append(url)
        return Response(url)

    monkeypatch.setattr(updates.httpx, "get", fake_get)
    result = admin.get("/api/update/check").json()
    assert result["version"] == __version__
    assert result["available"] is True
    assert result["latest_version"] == "0.2.0"
    assert result["latest_revision"] == "b" * 40
    assert requested_urls == [
        updates.GITHUB_HEAD, updates.GITHUB_VERSION.format(sha="b" * 40)
    ]


def test_ha_event_ids_are_namespaced_by_sensor_and_room(admin):
    first=admin.post("/api/rooms",json=room_payload(name="101")).json()
    second=admin.post("/api/rooms",json=room_payload(name="102")).json()
    key="this-is-a-long-device-ingest-key-1234567890"
    headers={"X-Api-Key":key}
    base={"room_id":first["id"],"value":20.5,"device":"sensor.a","event_id":"2026-11-12"}
    assert admin.post("/api/ingest",json=base,headers=headers).json()["inserted"]
    assert not admin.post("/api/ingest",json=base,headers=headers).json()["inserted"]
    assert admin.post("/api/ingest",json={**base,"device":"sensor.b"},headers=headers).json()["inserted"]
    assert admin.post("/api/ingest",json={**base,"room_id":second["id"]},headers=headers).json()["inserted"]
    assert len(admin.get("/api/readings").json())==3


def test_dashboard_assets_bust_cache_and_match_html(client):
    """Avoid old JS trying to access removed DOM nodes after native updates."""
    from hashlib import sha256
    from pathlib import Path
    from termokontur.app import _ASSET_FINGERPRINT

    static = Path(__file__).resolve().parents[1] / "termokontur" / "static"
    expected = sha256(b"".join(
        (static / name).read_bytes() for name in ("app.js", "style.css", "theme.js")
    )).hexdigest()[:16]
    assert _ASSET_FINGERPRINT == expected
    page = client.get("/")
    assert page.status_code == 200
    assert page.headers["cache-control"] == "no-store"
    assert "__ASSET_FINGERPRINT__" not in page.text
    assert f'/static/app.js?v={expected}' in page.text
    assert f'/static/style.css?v={expected}' in page.text
    assert f'/static/theme.js?v={expected}' in page.text

    for filename in ("app.js", "style.css", "theme.js"):
        asset = client.get(f"/static/{filename}?v={expected}")
        assert asset.status_code == 200
        assert asset.headers["cache-control"] == "no-store"

    js = client.get(f"/static/app.js?v={expected}").text
    assert '"update-current-version"' in js
    assert '"update-version"' not in js
    assert 'id="update-current-version"' in page.text
