from termokontur import db, migrations, sensors

from test_sensors import batch, setup_sensor, sync


def test_retire_esp01_without_losing_measurements(admin):
    device, _, headers, room = setup_sensor(admin)
    assert sync(admin, device, headers, batch()).status_code == 200
    device_id = device["device_id"]
    with db.connect() as conn:
        conn.execute("UPDATE sensor_devices SET model='esp01-sht40-v1', battery_mv=0 WHERE id=?", (device_id,))
        conn.execute("UPDATE sensor_records SET battery_mv=0 WHERE device_id=?", (device_id,))
        conn.execute("PRAGMA user_version=2")

    db.initialize()
    route = f"/api/sensors/devices/{device_id}"
    archived = admin.get("/api/sensors/devices").json()[0]
    assert archived["status"] == "retired"
    assert archived["next_sync_at"] is None
    assert archived["enabled"] == 0
    assert admin.get(f"{route}/records").json()[0]["battery_mv"] == 0
    assert admin.get(f"{route}/records").json()[0]["temperature"] == 21.5
    with db.connect() as conn:
        assert sensors.get_device(conn, device_id)["enabled"] == 0
        assert conn.execute("SELECT COUNT(*) FROM readings WHERE room_id=?", (room["id"],)).fetchone()[0] == 1
    assert admin.patch(route, json={"name": "Снова активен", "room_id": room["id"], "enabled": True}).status_code == 410
    assert admin.post(f"{route}/rekey").status_code == 410
    assert admin.put(f"{route}/ota?firmware_id=1").status_code == 410
    assert sync(admin, device, headers, batch()).status_code == 403
    denied = admin.post("/api/sensors/devices",
                        json={"name": "ESP-01S", "room_id": room["id"], "model": "esp01-sht40-v1"})
    assert denied.status_code == 422


def test_schema_v1_upgrade_keeps_existing_c3_device(admin):
    room = db.create_room("Школа", "1", "101", "Кабинет", 18, 24)
    reply = admin.post("/api/sensors/devices", json={"name": "C3", "room_id": room["id"]})
    assert reply.status_code == 200, reply.text
    device = reply.json()
    with db.connect() as conn:
        conn.execute("ALTER TABLE sensor_devices DROP COLUMN model")
        conn.execute("PRAGMA user_version=1")

    db.initialize()
    db.initialize()
    with db.connect() as conn:
        assert migrations.read_version(conn) == migrations.SCHEMA_VERSION
        stored = sensors.get_device(conn, device["device_id"])
        assert stored["model"] == sensors.MODEL
        assert stored["room_id"] == room["id"]
        assert stored["enabled"] == 1
        assert stored["enrollment_hash"] == sensors.digest(device["code"])
