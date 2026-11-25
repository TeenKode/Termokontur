"""Online backup/restore, retention and temperature alerts."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

from termokontur import db, maintenance, max_delivery, migrations, settings


def room(admin, name):
    response=admin.post("/api/rooms",json={
        "building":"Школа","name":name,"floor":"2","kind":"Кабинет",
        "min_temp":18,"max_temp":24,
    })
    assert response.status_code==201,response.text
    return response.json()


def test_backup_restore_invalidates_existing_sessions(admin):
    r1=room(admin,"201")
    admin.post("/api/readings",json={"room_id":r1["id"],"value":20})
    response=admin.post("/api/backups")
    assert response.status_code==200,response.text
    backup=response.json()
    assert backup["name"].endswith(".sqlite3")
    assert Path(db.db_path().parent/"backups"/backup["name"]).is_file()
    assert admin.get("/api/backups/"+backup["name"]).status_code==200
    room(admin,"202")
    assert len(admin.get("/api/rooms").json())==2
    assert admin.post("/api/backups/restore",json={
        "name":backup["name"],"password":"wrong"}).status_code==403
    restored=admin.post("/api/backups/restore",json={
        "name":backup["name"],"password":"this-is-a-test-admin-password"})
    assert restored.status_code==200,restored.text
    assert "pre-restore" in restored.json()["safety_backup"]
    assert admin.get("/api/rooms").status_code==401
    assert admin.post("/api/login",json={"password":"this-is-a-test-admin-password"}).status_code==200
    assert [r["name"] for r in admin.get("/api/rooms").json()] == ["201"]
    assert len(admin.get("/api/readings").json())==1


def test_backup_upload_validation_and_delete(admin, monkeypatch):
    room(admin,"101")
    original=admin.post("/api/backups").json()
    snapshot=admin.get("/api/backups/"+original["name"]).content
    imported=admin.post("/api/backups/import",content=snapshot,
                        headers={"Content-Type":"application/octet-stream"})
    assert imported.status_code==200,imported.text
    assert imported.json()["name"] != original["name"]
    assert admin.post("/api/backups/import",content=b"invalid",
                      headers={"Content-Type":"application/octet-stream"}).status_code==422
    monkeypatch.setattr(maintenance,"MAX_IMPORT_BYTES",1024)
    assert admin.post("/api/backups/import",content=b"x"*1025,
                      headers={"Content-Type":"application/octet-stream"}).status_code==413
    assert len(admin.get("/api/backups").json())==2
    assert admin.delete("/api/backups/"+imported.json()["name"]).status_code==200
    assert len(admin.get("/api/backups").json())==1
    assert admin.get("/api/backups/../sneaky").status_code!=200


def test_retention_safety_and_scheduling(admin):
    r=room(admin,"203")
    before=(datetime.now(timezone.utc)-timedelta(days=100)).isoformat(timespec="seconds")
    with db.connect() as conn:
        conn.execute("""INSERT INTO readings(room_id,value,source,reporter,observed_at)
                        VALUES(?,21,'manual','tester',?)""",(r["id"],before))
    assert maintenance.prune_data(0) == {"readings": 0, "sensor_records": 0}
    assert maintenance.prune_data(30)["readings"]==1
    assert admin.post("/api/backups").status_code==200
    settings.update({"backup_interval_days":"1","backup_keep":"5","retention_days":"0"})
    first=maintenance.schedule()
    assert first["created"]
    second=maintenance.schedule()
    assert not second["created"]


def test_local_copy_not_capped_by_upload_limit(admin, monkeypatch):
    monkeypatch.setattr(maintenance, "MAX_IMPORT_BYTES", 1024)
    copy = maintenance.create_backup()
    assert copy["size"] > maintenance.MAX_IMPORT_BYTES
    assert maintenance.restore(copy["name"])["restored"] == copy["name"]


def test_backup_upload_rejects_newer_schema(admin, tmp_path):
    name = maintenance.create_backup()["name"]
    staged = tmp_path / "future.sqlite3"
    staged.write_bytes(maintenance.backup_path(name).read_bytes())
    with db.connect(staged) as conn:
        conn.execute(f"PRAGMA user_version={migrations.SCHEMA_VERSION + 1}")
    response = admin.post("/api/backups/import", content=staged.read_bytes(),
                          headers={"Content-Type": "application/octet-stream"})
    assert response.status_code == 422
    assert "версия схемы" in response.json()["detail"]
    assert [backup["name"] for backup in maintenance.list_backups()] == [name]


def test_backup_failure_does_not_skip_reports(admin, monkeypatch):
    from termokontur import report_schedule
    def unavailable_backup(kind):
        raise OSError("test storage absent")
    monkeypatch.setattr(maintenance, "create_backup", unavailable_backup)
    monkeypatch.setattr(report_schedule, "queue_due", lambda: {"queued": 1})
    result = maintenance.schedule()
    assert result["backup_error"]
    assert result["report_digests_queued"] == 1


def test_temperature_alerts_do_not_spam(client):
    r=db.create_room("Корпус","1","105","Кабинет",18,24)
    assert db.bind_code("42",db.issue_code(r["id"]))
    settings.set_secret("max_token","the-max-token-will-be-longer-than-20")
    settings.update({"alerts_enabled":"1","alert_cooldown_minutes":"60"})
    db.add_reading(r["id"],16,"manual")
    assert max_delivery.queue_temperature_alerts()==0
    db.add_reading(r["id"],16,"ha")
    assert max_delivery.queue_temperature_alerts()==1
    assert max_delivery.queue_temperature_alerts()==0
    messages=max_delivery.pending()
    assert len(messages)==1
    assert "ниже минимума" in messages[0]["text"]
    db.add_reading(r["id"],21,"ha")
    assert max_delivery.queue_temperature_alerts()==0
    db.add_reading(r["id"],26,"ha")
    assert max_delivery.queue_temperature_alerts()==1
    assert len(max_delivery.pending())==2
    settings.update({"alerts_enabled":"0"})
    db.add_reading(r["id"],16,"ha")
    assert max_delivery.queue_temperature_alerts()==0


def test_backup_rejects_missing_current_schema_column(admin, tmp_path):
    original = maintenance.backup_path(maintenance.create_backup()["name"])
    staged = tmp_path / "bad.sqlite3"
    staged.write_bytes(original.read_bytes())
    with db.connect(staged) as conn:
        conn.execute("ALTER TABLE rooms DROP COLUMN floor")
    reply = admin.post("/api/backups/import", content=staged.read_bytes(),
                       headers={"Content-Type": "application/octet-stream"})
    assert reply.status_code == 422, reply.text
    assert "rooms" in reply.json()["detail"]


def test_old_backup_is_validated_without_modifying_original(admin, tmp_path):
    original = maintenance.backup_path(maintenance.create_backup()["name"])
    staged = tmp_path / "legacy.sqlite3"
    staged.write_bytes(original.read_bytes())
    with db.connect(staged) as conn:
        conn.execute("DROP TABLE sensor_factory_tickets")
        conn.execute("PRAGMA user_version=3")
    before = staged.read_bytes()
    maintenance.inspect(staged)
    assert staged.read_bytes() == before


def test_update_backups_visible_downloadable_and_rotated(admin):
    settings.update({"backup_interval_days": "0", "backup_keep": "1"})
    original = maintenance.backup_path(maintenance.create_backup()["name"])
    import os
    old = original.parent / ("pre-update-20260101T000000Z-" + "a"*40 + ".sqlite3")
    new = original.parent / ("pre-update-20260102T000000Z-" + "b"*40 + ".sqlite3")
    old.write_bytes(original.read_bytes())
    new.write_bytes(original.read_bytes())
    os.utime(old, (100, 100))
    os.utime(new, (200, 200))
    assert admin.get("/api/backups/" + old.name).status_code == 200
    assert old.name in {x["name"] for x in maintenance.list_backups()}
    maintenance.schedule()
    assert not old.exists() and new.exists() and original.exists()
