"""ESP-01S compatibility, 1 MB flash deployment and cross-model isolation."""

import time
import pytest
from termokontur import db, sensors


def esp01(admin):
    room = db.create_room("Школа", "1", "ESP01", "Кабинет", 18, 24)
    response = admin.post("/api/sensors/devices",
                          json={"name": "ESP-01S", "room_id": room["id"]})
    assert response.status_code == 200, response.text
    created = response.json()
    enrollment = admin.post("/api/sensors/enroll", json={
        "device_id": created["device_id"], "code": created["code"],
        "hardware_id": "aabbccddeeff", "model": "esp01-sht40-v1",
        "sensor_type": "sht4x",
    })
    assert enrollment.status_code == 200, enrollment.text
    token = enrollment.json()["token"]
    return created["device_id"], {"Authorization": "Bearer " + token}


def packet(model="esp01-sht40-v1", battery_mv=0):
    return {
        "model": model, "sensor_type": "sht4x", "firmware": "0.1.0",
        "next_seq": 2, "lost": 0, "rssi": -65, "battery_mv": battery_mv,
        "config_version": 1, "boots": 1,
        "records": [{
            "seq": 1, "boot_id": "0000000000000001", "uptime_s": 0,
            "measured_at": int(time.time()) - 100, "time_quality": "server",
            "temperature": 21.5, "humidity": 44.3,
            "battery_mv": battery_mv, "interval_s": 1800, "error": "",
        }]
    }


def test_esp01_enroll_sync_and_unknown_battery(admin):
    device, token = esp01(admin)
    r = admin.post(f"/api/sensors/devices/{device}/sync",
                   headers=token, json=packet())
    assert r.status_code == 200, r.text
    assert r.json()["ack_seq"] == 2
    listing = admin.get("/api/sensors/devices").json()
    d = next(x for x in listing if x["id"] == device)
    assert d["model"] == "esp01-sht40-v1"
    assert d["battery_mv"] == 0
    assert d["status"] != "low_battery"
    with db.connect() as conn:
        record = conn.execute("SELECT battery_mv FROM sensor_records WHERE device_id=?",
                              (device,)).fetchone()
        assert record[0] == 0


def test_esp01_reject_model_spoof_and_bogus_voltage(admin):
    device, token = esp01(admin)
    bad = admin.post(f"/api/sensors/devices/{device}/sync",
                     headers=token, json=packet(model="c3-sht40-v1"))
    assert bad.status_code == 409
    for wrong in (1, 1999, 5001):
        bad = admin.post(f"/api/sensors/devices/{device}/sync",
                         headers=token, json=packet(battery_mv=wrong))
        assert bad.status_code == 422, bad.text


def test_esp01_cannot_receive_c3_firmware(admin):
    device, token = esp01(admin)
    with db.connect() as conn:
        conn.execute("""INSERT INTO sensor_firmware(version,model,sha256,image,created_at)
                        VALUES(?,?,?,?,?)""",
                     ("9.0.0", "c3-sht40-v1", "0" * 64, b"x" * 256,
                      db.utc_now()))
        fw_id = conn.execute("SELECT id FROM sensor_firmware WHERE version='9.0.0'").fetchone()[0]
    r = admin.put(f"/api/sensors/devices/{device}/ota?firmware_id={fw_id}")
    assert r.status_code == 422, r.text
