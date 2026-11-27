from datetime import datetime

from termokontur import db, settings


def test_building_account_scopes_dashboard_reports_and_downloads(admin):
    settings.update({"timezone": "Asia/Vladivostok"})
    for building in ("Начальная", "Старшая"):
        room = db.create_room(building, "", "101", "Кабинет", 18, 24)
        db.add_reading(room["id"], 21, "manual")
    password = "local-viewer-test-password"
    admin_session = admin.cookies.get("tk_session")
    reply = admin.put("/api/building-accounts/junior", json={"building": "Начальная", "password": password})
    assert reply.json() == {"username": "junior", "building": "Начальная", "max_user_id": None}
    reply = admin.post("/api/login", json={"username": "junior", "password": password})
    assert reply.json() == {"ok": True, "building": "Начальная", "timezone": "Asia/Vladivostok"}
    viewer_session = admin.cookies.get("tk_session")
    assert admin.get("/api/session").json() == {
        "authenticated": True, "building": "Начальная", "timezone": "Asia/Vladivostok",
    }
    for path in ("/api/dashboard", "/api/daily", "/api/dashboard/history"):
        assert {r["building"] for r in admin.get(path).json()["rooms"]} == {"Начальная"}
    assert admin.get("/api/dashboard/history", params={"building": "Старшая"}).status_code == 403
    now = datetime.now(settings.local_zone()).date().isoformat()
    date_range = {"start": now, "end": now}
    for path in ("/api/reports", "/api/reports/export.xlsx", "/api/reports/export.pdf"):
        assert admin.get(path, params=date_range).status_code == 200
        assert admin.get(path, params={**date_range, "building": "Старшая"}).status_code == 403
    assert admin.get("/api/reports", params=date_range).json()["building"] == "Начальная"
    assert admin.get("/api/settings").status_code == 401
    assert admin.get("/api/reports/subscriptions").status_code == 401
    assert admin.post("/api/readings", json={"room_id": 1, "value": 22}).status_code == 401
    assert admin.post("/api/logout").status_code == 200
    admin.cookies.set("tk_session", admin_session)
    assert admin.delete("/api/building-accounts/junior").status_code == 200
    admin.cookies.set("tk_session", viewer_session)
    assert admin.get("/api/dashboard").status_code == 401
