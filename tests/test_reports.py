"""Regression tests for school reports, exports and scheduled MAX summaries."""
from datetime import datetime, timezone
from io import BytesIO
from zipfile import ZipFile

from termokontur import db, report_exports, report_schedule, reports, settings


NOW = datetime(2026, 11, 18, 15, tzinfo=timezone.utc)


def create_room(name, building="Старшая школа"):
    return db.create_room(building, "2", name, "Кабинет", 18, 24)


def reading(room_id, value, observed, source="max", reporter="MAX:71"):
    with db.connect() as conn:
        conn.execute(
            """INSERT INTO readings(room_id,value,source,reporter,observed_at)
               VALUES(?,?,?,?,?)""",
            (room_id, value, source, reporter, observed),
        )


def users_and_rooms():
    one = create_room("101")
    two = create_room("201", "Начальная школа")
    for uid, name in (("71", "Ирина"), ("72", "Ольга")):
        db.remember_max_user({"user_id": uid, "first_name": name,
                              "last_name": "Иванова", "is_bot": False})
        with db.connect() as conn:
            conn.execute("INSERT INTO max_bindings(user_id,room_id) VALUES(?,?)",
                         (uid, one["id"]))
    reading(one["id"], 18, "2026-11-17T09:00:00+00:00")
    reading(one["id"], 22, "2026-11-17T11:00:00+00:00")
    reading(two["id"], 27, "2026-11-18T09:00:00+00:00", "ha", "sensor")
    return one, two


def test_report_room_day_averages_and_coverage(client):
    one,two=users_and_rooms()
    report = reports.build("2026-11-17", "2026-11-18", now=NOW)
    s = report["summary"]
    assert s["rooms_total"] == 4
    assert s["rooms_measured"] == 2
    assert s["missing"] == 2
    assert s["coverage"] == 50
    assert s["average"] == 23.5
    assert s["out_of_range"] == 1
    assert s["deviations"] == 1
    assert s["readings"] == 3
    assert report["daily"][0]["average"] == 20
    assert report["daily"][1]["average"] == 27
    assert len(report["missing"]) == 2
    assert len(report["building_summary"]) == 2
    assert len(report["building_daily"]) == 4
    assert len(report["rooms"]) == 2
    assert any(d["room_id"] == one["id"] and d["average"] == 20
               for d in report["room_daily"])


def test_staff_stats_do_not_blame_shared_room_colleague(client):
    users_and_rooms()
    report = reports.build("2026-11-17", "2026-11-18", now=NOW)
    staff = {p["user_id"]:p for p in report["staff"]}
    assert staff["71"]["coverage"] == 50
    assert staff["72"]["coverage"] == 50
    assert staff["71"]["authored_readings"] == 2
    assert staff["72"]["authored_readings"] == 0
    assert staff["72"]["days_with_submission"] == 0
    assert staff["72"]["measured_room_days"] == 1
    assert staff["72"]["missing_room_days"] == 1


def test_building_filter_does_not_include_other_buildings(client):
    users_and_rooms()
    r = reports.build("2026-11-17","2026-11-18","Начальная школа",now=NOW)
    assert r["rooms_count"] == 1
    assert r["summary"]["rooms_measured"] == 1
    assert r["summary"]["missing"] == 1
    assert r["staff"] == []
    assert r["building_summary"][0]["building"] == "Начальная школа"


def test_invalid_period_and_future_dates(client):
    for start,end in (("2026-11-18","2026-11-17"),
                      ("2026-11-18","2026-11-19"),
                      ("2025-01-01","2026-11-18"),
                      ("bad-date","2026-11-18")):
        import pytest
        with pytest.raises(ValueError):
            reports.build(start,end,now=NOW)


def test_report_exports_and_access_control(client,admin):
    create_room("101")
    start=end=datetime.now(timezone.utc).date().isoformat()
    assert client.post("/api/logout").status_code==200
    for url in ("/api/reports","/api/reports/export.xlsx","/api/reports/export.pdf"):
        assert client.get(url,params={"start":start,"end":end}).status_code == 401
    assert admin.post("/api/login",json={"password":"this-is-a-test-admin-password"}).status_code == 200
    page=admin.get("/api/reports",params={"start":start,"end":end})
    assert page.status_code==200
    assert page.json()["rooms_count"] == 1
    excel=admin.get("/api/reports/export.xlsx",params={"start":start,"end":end})
    assert excel.status_code==200,excel.text[:300]
    assert excel.content.startswith(b"PK")
    with ZipFile(BytesIO(excel.content)) as archive:
        text=archive.read("xl/workbook.xml").decode()
        for sheet in ("Сводка","Сдача","Температура","Здания","Сотрудники","Пропуски"):
            assert sheet in text
    pdf=admin.get("/api/reports/export.pdf",params={"start":start,"end":end})
    assert pdf.status_code==200,pdf.text[:300]
    assert pdf.content.startswith(b"%PDF")
    assert len(pdf.content)>3000
    assert admin.get("/api/reports",params={
        "start":"2000-01-01","end":end}).status_code==422


def test_scheduled_digest_once_per_period_and_retention_safe(admin):
    settings.update({"timezone":"Asia/Vladivostok"})
    db.remember_max_user({"user_id":71, "first_name":"Мария", "is_bot":False})
    create_room("401")
    settings.set_secret("max_token","test-valid-max-token-for-report-123456")
    day=report_schedule.create("71","","daily","09:00",0,True)
    week=report_schedule.create("71","","weekly","09:00",0,True)
    monday=datetime(2026,11,16,0,15,tzinfo=timezone.utc)
    result=report_schedule.queue_due(now=monday)
    assert result["queued"]==2
    assert report_schedule.queue_due(now=monday)["queued"]==0
    with db.connect() as conn:
        rows=conn.execute("SELECT text FROM max_outbox").fetchall()
        assert len(rows)==2
        assert all("Сводка по температуре" in x["text"] for x in rows)
        conn.execute("UPDATE max_outbox SET delivered_at='2001-01-01T00:00:00+00:00'")
    from termokontur import max_delivery
    max_delivery.prune(30)
    assert report_schedule.queue_due(now=monday)["queued"]==0
    assert report_schedule.set_enabled(day["id"],False)
    assert report_schedule.remove(week["id"])


def test_subscription_api_auth_and_validation(client,admin):
    db.remember_max_user({"user_id":71,"first_name":"Ирина","is_bot":False})
    settings.set_secret("max_token","test-valid-max-token-for-report-123456")
    body={"user_id":"71","building":"","frequency":"weekly",
          "send_time":"11:30","weekday":4,"enabled":True}
    assert client.post("/api/logout").status_code==200
    assert client.get("/api/reports/subscriptions").status_code==401
    assert admin.post("/api/login",json={"password":"this-is-a-test-admin-password"}).status_code==200
    assert admin.post("/api/reports/subscriptions",json={**body,"user_id":"999"}).status_code==422
    assert admin.post("/api/reports/subscriptions",json={**body,"send_time":"29:00"}).status_code==422
    result=admin.post("/api/reports/subscriptions",json=body)
    assert result.status_code==201,result.text
    sid=result.json()["id"]
    assert len(admin.get("/api/reports/subscriptions").json())==1
    assert admin.patch(f"/api/reports/subscriptions/{sid}",json={"enabled":False}).status_code==200
    assert admin.get("/api/reports/subscriptions").json()[0]["enabled"]==0
    assert admin.delete(f"/api/reports/subscriptions/{sid}").status_code==200
    assert admin.get("/api/reports/subscriptions").json()==[]


def test_daily_timezone_crosses_utc_midnight(client):
    settings.update({"timezone":"Asia/Vladivostok"})
    room=create_room("301")
    reading(room["id"],21,"2026-11-17T20:00:00+00:00")
    now=datetime(2026,11,18,1,0,tzinfo=timezone.utc)
    r=reports.build("2026-11-18","2026-11-18",now=now)
    assert r["summary"]["rooms_measured"]==1
    assert r["daily"][0]["average"]==21
