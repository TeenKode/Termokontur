import copy
import hashlib
import json
import struct
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from io import BytesIO
from zipfile import ZipFile

import pytest
from termokontur import db, maintenance, sensors, sensor_mqtt

BASE_TIME = int(time.time()) - 86400


def setup_sensor(admin):
    room = db.create_room("Школа", "1", "12", "Кабинет", 18, 24)
    created = admin.post("/api/sensors/devices", json={"name": "Датчик 12", "room_id": room["id"]})
    assert created.status_code == 200, created.text
    d = created.json()
    enrollment = {**d, "hardware_id": "0123456789ab", "model": sensors.MODEL}
    enrollment.pop("expires_in")
    enrolled = admin.post("/api/sensors/enroll", json=enrollment)
    assert enrolled.status_code == 200, enrolled.text
    return d, enrollment, {"Authorization": "Bearer "+enrolled.json()["token"]}, room


def record(seq=1, **kwargs):
    return {"seq": seq, "boot_id": "0123456789abcdef", "uptime_s": seq*1800,
            "measured_at": BASE_TIME+seq*1800, "time_quality": "rtc",
            "temperature": 21.5, "humidity": 45.1, "battery_mv": 3910,
            "interval_s": 1800, "error": "", **kwargs}


def batch(records=None, **kwargs):
    return {"model": sensors.MODEL, "firmware": "0.1.0", "records": [record()] if records is None else records,
            "next_seq": 2, "lost": 0, "rssi": -65, "battery_mv": 3910,
            "config_version": 1, "boots": 3, **kwargs}


def sync(admin, d, headers, payload):
    return admin.post("/api/sensors/devices/"+d["device_id"]+"/sync", headers=headers, json=payload)


def test_admin_only_and_no_shared_enrollment_key(client):
    assert client.get("/api/sensors/devices").status_code == 401
    assert client.post("/api/sensors/devices", json={"name":"x","room_id":1}).status_code == 401
    assert client.post("/api/sensors/enroll", json={"key":"shared"}).status_code == 422


def test_enroll_retry_and_consumption(admin):
    d, enroll, headers, room = setup_sensor(admin)
    retried = admin.post("/api/sensors/enroll", json=enroll)
    assert retried.json()["token"] == headers["Authorization"][7:]
    other = {**enroll, "hardware_id": "1123456789ab"}
    assert admin.post("/api/sensors/enroll", json=other).status_code == 409
    assert sync(admin,d,headers,batch()).status_code == 200
    assert admin.post("/api/sensors/enroll", json=enroll).status_code == 403
    listing = admin.get("/api/sensors/devices").json()[0]
    assert "token_hash" not in listing and "enrollment_hash" not in listing


def test_usb_diagnostic_sync_accepts_missing_voltage(admin):
    device, _, headers, _ = setup_sensor(admin)
    record_without_battery = record(battery_mv=0)
    assert sync(admin, device, headers, batch([record_without_battery], battery_mv=1)).status_code == 422
    assert sync(admin, device, headers, batch([record(battery_mv=1999)], battery_mv=0)).status_code == 422
    payload = batch([record_without_battery], battery_mv=0)
    for _ in range(2):
        response = sync(admin, device, headers, payload)
        assert response.status_code == 200, response.text
        assert response.json()["ack_seq"] == 2
    listing = admin.get("/api/sensors/devices").json()[0]
    assert listing["battery_mv"] == 0 and listing["status"] == "recent"
    with db.connect() as conn:
        rows = conn.execute("SELECT temperature,battery_mv FROM sensor_records").fetchall()
        assert [tuple(row) for row in rows] == [(21.5, 0)]


def test_idempotent_history_and_conflicting_replay(admin):
    d,enroll,headers,room=setup_sensor(admin)
    payload=batch()
    for _ in range(2):
        response=sync(admin,d,headers,payload)
        assert response.status_code==200,response.text
        assert response.json()["ack_seq"]==2
    with db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM sensor_records").fetchone()[0]==1
        reading=dict(conn.execute("SELECT * FROM readings").fetchone())
    assert reading["room_id"]==room["id"] and reading["source"]=="ha"
    assert reading["observed_at"].startswith(time.strftime("%Y-%m-%d",time.gmtime(payload["records"][0]["measured_at"])))
    payload["records"][0]["temperature"]=22.0
    assert sync(admin,d,headers,payload).status_code==409
    assert admin.get("/api/dashboard/history?days=7").status_code==200


def test_batch_atomicity_and_unknown_time(admin):
    d,_,headers,_=setup_sensor(admin)
    r1=record();r2=record(2, measured_at=int(time.time())+10000)
    assert sync(admin,d,headers,batch([r1,r2],next_seq=3)).status_code==422
    with db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM sensor_records").fetchone()[0]==0
    r2["measured_at"]=None;r2["time_quality"]="unknown"
    response=sync(admin,d,headers,batch([r1,r2],next_seq=3))
    assert response.status_code==200,response.text
    with db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM sensor_records").fetchone()[0]==2
        assert conn.execute("SELECT count(*) FROM readings").fetchone()[0]==1


def test_loss_gaps_across_pages_and_empty_ack(admin):
    d,_,headers,_=setup_sensor(admin)
    payload=batch([record(3)],next_seq=8,lost=4)
    response=sync(admin,d,headers,payload)
    assert response.status_code==200,response.text
    assert response.json()["ack_seq"]==4
    response=sync(admin,d,headers,batch([record(6),record(7)],next_seq=8,lost=4))
    assert response.status_code==200,response.text
    assert response.json()["ack_seq"]==8
    response=sync(admin,d,headers,batch([],next_seq=11,lost=7))
    assert response.json()["ack_seq"]==8
    assert sync(admin,d,headers,batch([record(11)],next_seq=12,lost=7)).json()["ack_seq"]==12
    assert sync(admin,d,headers,batch([record(13)],next_seq=14,lost=7)).status_code==409


@pytest.mark.parametrize("intact", [True, False])
def test_restore_recovers_confirmed_records_or_records_gap(admin, intact):
    d, _, headers, _ = setup_sensor(admin)
    assert sync(admin, d, headers, batch()).json()["ack_seq"] == 2
    backup = maintenance.create_backup()
    assert sync(admin, d, headers, batch([record(2)], next_seq=3)).json()["ack_seq"] == 3
    maintenance.restore(backup["name"])
    acknowledged = {"device_ack": 3, "device_lost_base": 0}
    probe = sync(admin, d, headers, batch([record(3)], next_seq=4, **acknowledged))
    assert probe.json()["replay_from"] == 2
    retained = [record(2), record(3)] if intact else [record(3)]
    payload = batch(retained, next_seq=4, replay=True, **acknowledged)
    for _ in range(2):
        assert sync(admin, d, headers, payload).json()["ack_seq"] == 4
    with db.connect() as conn:
        assert [row[0] for row in conn.execute("SELECT seq FROM sensor_records ORDER BY seq")] == (
            [1, 2, 3] if intact else [1, 3]
        )
        assert conn.execute("SELECT COUNT(*) FROM sensor_events WHERE kind='restore_gap'").fetchone()[0] == int(not intact)
    assert sync(admin, d, headers, batch([record(5)], next_seq=6)).status_code == 409


def test_restore_allows_one_legacy_gap(admin):
    d, _, headers, _ = setup_sensor(admin)
    assert sync(admin, d, headers, batch()).json()["ack_seq"] == 2
    backup = maintenance.create_backup()
    assert sync(admin, d, headers, batch([record(3)], next_seq=4)).status_code == 409
    maintenance.restore(backup["name"])
    assert sync(admin, d, headers, batch([record(3)], next_seq=4)).json()["ack_seq"] == 4
    assert sync(admin, d, headers, batch([record(5)], next_seq=6)).status_code == 409


def test_report_waits_for_complete_sensor_batch_across_midnight(admin):
    from termokontur import report_schedule, reports, settings
    device, _, headers, room = setup_sensor(admin)
    settings.set_secret("max_token", "test-only-max-token-for-sensor-report")
    db.remember_max_user({"user_id": "77", "first_name": "Ответственный"})
    report_schedule.create("77", "", "daily", "09:00")
    monday = datetime(2026, 11, 16, 15, tzinfo=timezone.utc)
    tuesday = datetime(2026, 11, 17, 9, tzinfo=timezone.utc)
    wednesday = datetime(2026, 11, 18, 8, tzinfo=timezone.utc)
    config = sensors.Config(send_s=172800).model_dump_json()
    with db.connect() as conn:
        conn.execute("UPDATE rooms SET created_at=? WHERE id=?", (monday.isoformat(), room["id"]))
        conn.execute("UPDATE sensor_devices SET config=?,created_at=?,last_seen=?,last_complete_sync=? WHERE id=?",
                     (config, monday.isoformat(), int(monday.timestamp()), int(monday.timestamp()), device["device_id"]))
    assert reports.build("2026-11-16", "2026-11-16", now=tuesday)["summary"]["awaiting"] == 1
    assert report_schedule.queue_due(tuesday)["queued"] == 0
    assert report_schedule.subscriptions()[0]["periods_waiting"] == 1
    assert sync(admin, device, headers, batch([record(measured_at=int(monday.timestamp()))], next_seq=3)).status_code == 200
    assert report_schedule.queue_due(tuesday)["queued"] == 0
    assert sync(admin, device, headers, batch([record(2, measured_at=int(monday.timestamp()))], next_seq=3)).status_code == 200
    assert report_schedule.queue_due(wednesday)["queued"] == 1
    assert report_schedule.queue_due(wednesday)["queued"] == 0
    assert report_schedule.subscriptions()[0]["periods_waiting"] == 0


def test_sensor_alerts_use_building_recipient_and_rearm(admin):
    from termokontur import auth, sensor_monitor, settings
    device, _, headers, room = setup_sensor(admin)
    db.remember_max_user({"user_id": "77", "first_name": "Ответственный"})
    auth.save_building_account("viewer", room["building"], "local-viewer-test-password", "77")
    settings.set_secret("max_token", "test-only-max-token-for-sensor-alerts")
    assert sync(admin, device, headers, batch(battery_mv=3720)).status_code == 200
    assert sensor_monitor.queue_alerts() == 1
    assert sensor_monitor.queue_alerts() == 0
    assert sync(admin, device, headers, batch([], battery_mv=3910)).status_code == 200
    assert sensor_monitor.queue_alerts() == 0
    with db.connect() as conn:
        conn.execute("UPDATE sensor_devices SET last_seen=last_seen-90001")
    assert sensor_monitor.queue_alerts() == 1
    with db.connect() as conn:
        notice = conn.execute("SELECT user_id,text FROM max_outbox").fetchone()
        assert notice["user_id"] == "77" and "пропущен сеанс связи" in notice["text"]


def test_humidity_report_and_workbook_use_dated_records(admin):
    from termokontur import reports
    d, _, headers, room = setup_sensor(admin)
    observed = BASE_TIME
    values = [record(1, measured_at=observed, humidity=35.0),
              record(2, measured_at=observed + 60, humidity=55.0),
              record(3, measured_at=None, time_quality="unknown", humidity=99.0)]
    assert sync(admin, d, headers, batch(values, next_seq=4)).status_code == 200
    day = datetime.fromtimestamp(observed, timezone.utc).date().isoformat()
    row = reports.build(day, day, room["building"])["building_daily"][0]
    assert (row["humidity_average"], row["humidity_minimum"], row["humidity_maximum"], row["humidity_readings"]) == (45, 35, 55, 2)
    excel = admin.get("/api/reports/export.xlsx", params={"start": day, "end": day, "building": room["building"]})
    with ZipFile(BytesIO(excel.content)) as workbook:
        assert "Влажность" in workbook.read("xl/workbook.xml").decode()


def test_keys_disable_configuration_and_migration(admin):
    d,_,headers,room=setup_sensor(admin)
    assert sync(admin,d,{"Authorization":"Bearer wrong"},batch()).status_code==401
    config=sensors.Config().model_dump();config["measure_s"]=0
    assert admin.put(f'/api/sensors/devices/{d["device_id"]}/config',json=config).status_code==422
    config=sensors.Config(measure_s=3600).model_dump()
    assert admin.put(f'/api/sensors/devices/{d["device_id"]}/config',json=config).json()["version"]==2
    assert sync(admin,d,headers,batch()).json()["config"]["measure_s"]==3600
    db.initialize();db.initialize()
    with db.connect() as conn:
        assert [r[0] for r in conn.execute("SELECT version FROM sensor_migrations ORDER BY version")] == [1, 2]
    admin.patch(f'/api/sensors/devices/{d["device_id"]}',json={"name":"12","room_id":room["id"],"enabled":False})
    assert sync(admin,d,headers,batch()).status_code==403
    admin.patch(f'/api/sensors/devices/{d["device_id"]}',json={"name":"12","room_id":room["id"],"enabled":True})
    assert admin.post(f'/api/sensors/devices/{d["device_id"]}/rekey').status_code==200
    assert sync(admin,d,headers,batch()).status_code==401


def test_mqtt_secret_and_discovery(admin):
    d,_,headers,_=setup_sensor(admin)
    sync(admin,d,headers,batch())
    cfg=sensor_mqtt.MQTTConfig(host="localhost",password="test-secret")
    response=admin.put("/api/sensors/mqtt",json=cfg.model_dump())
    assert response.status_code==200,response.text
    assert response.json()["password_configured"] and "password" not in response.json()
    cfg.password=None;admin.put("/api/sensors/mqtt",json=cfg.model_dump())
    assert sensor_mqtt.configuration()["password"]=="test-secret"
    with db.connect() as conn:
        device=sensors.get_device(conn,d["device_id"])
        latest=conn.execute("SELECT * FROM sensor_records").fetchone()
    messages=list(sensor_mqtt.messages(device,latest,cfg.model_dump()))
    assert len(messages)==6
    assert json.loads(messages[0][1])["unique_id"].endswith("_temperature")
    assert json.loads(messages[-2][1])["humidity"]==45.1
    assert messages[-1][1]=="online"


def fake_firmware(version="0.2.0",chip=5):
    data=bytearray(1024);data[0]=0xe9;struct.pack_into("<H",data,12,chip);struct.pack_into("<I",data,32,0xabcd5432)
    data[48:48+len(version)]=version.encode();name=b"termokontur-sensor";data[80:80+len(name)]=name
    return bytes(data)


def test_firmware_target_and_auth(admin):
    d,_,headers,_=setup_sensor(admin)
    assert admin.post("/api/sensors/firmware",files={"file":("x.bin",b"invalid")}).status_code==422
    assert admin.post("/api/sensors/firmware",files={"file":("x.bin",fake_firmware(chip=0))}).status_code==422
    response=admin.post("/api/sensors/firmware",files={"file":("x.bin",fake_firmware())})
    assert response.status_code==200,response.text
    fw=response.json()["id"]
    route=f'/api/sensors/devices/{d["device_id"]}'
    assert admin.get(route+f"/firmware/{fw}",headers=headers).status_code==403
    assert admin.put(route+f"/ota?firmware_id={fw}").status_code==200
    assert sync(admin,d,headers,batch()).json()["ota"]["version"]=="0.2.0"
    assert admin.get(route+f"/firmware/{fw}",headers=headers).content==fake_firmware()
    assert admin.get(route+f"/firmware/{fw}").status_code==401
    assert sync(admin,d,headers,batch(ota_failures=3,ota_generation=1)).json()["ota"] is None
    assert admin.put(route+"/ota").status_code==200


@pytest.mark.parametrize("patch",[
    {"seq":0},{"humidity":101.0},{"temperature":float("inf")},
    {"measured_at":None},{"time_quality":"unknown"},{"error":"sensor_io"},
])
def test_record_validation(patch):
    with pytest.raises(ValueError):sensors.Record(**record(**patch))


def test_retry_generation_and_parallel_duplicate_packets(admin):
    d,_,headers,_=setup_sensor(admin)
    payload=batch()
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses=list(pool.map(lambda _:sync(admin,d,headers,payload),range(8)))
    assert all(r.status_code==200 for r in responses)
    with db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM sensor_records").fetchone()[0]==1
    fw=admin.post("/api/sensors/firmware",files={"file":("new.bin",fake_firmware())}).json()["id"]
    route=f'/api/sensors/devices/{d["device_id"]}/ota?firmware_id={fw}'
    admin.put(route)
    assert sync(admin,d,headers,batch(ota_generation=1,ota_failures=3)).json()["ota"] is None
    admin.put(route)
    proposal=sync(admin,d,headers,batch(ota_generation=1,ota_failures=3)).json()["ota"]
    assert proposal["generation"]==2


def test_mqtt_publish_failure_is_durable(admin,monkeypatch):
    d,_,headers,_=setup_sensor(admin)
    assert sync(admin,d,headers,batch()).status_code==200
    sensor_mqtt.configure(sensor_mqtt.MQTTConfig(enabled=True,host="localhost",tls=False))
    import paho.mqtt.client as mqtt
    def fail(*args,**kwargs):raise OSError("simulated disconnected broker")
    monkeypatch.setattr(mqtt.Client,"connect",fail)
    sensor_mqtt.drain()
    queued=sensor_mqtt.public()["outbox"]
    assert len(queued)==1 and queued[0]["attempts"]==1 and queued[0]["error"]=="OSError"
    assert queued[0]["next_retry"]>time.time()
    with db.connect() as conn:
        assert sensors.get_device(conn,d["device_id"])["ack_seq"]==2


def test_mqtt_does_not_drop_concurrent_generation(admin,monkeypatch):
    d,_,headers,_=setup_sensor(admin)
    sync(admin,d,headers,batch())
    sensor_mqtt.configure(sensor_mqtt.MQTTConfig(enabled=True,host="localhost",tls=False))
    import paho.mqtt.client as mqtt
    class Receipt:
        def wait_for_publish(self,timeout):pass
        def is_published(self):return True
    class Fake:
        def __init__(self,*args,**kwargs):self.first=True
        def connect(self,*args,**kwargs):pass
        def loop_start(self):pass
        def disconnect(self):pass
        def loop_stop(self):pass
        def publish(self,*args,**kwargs):
            if self.first:
                self.first=False
                with db.connect() as conn:conn.execute("UPDATE sensor_mqtt_outbox SET generation=generation+1")
            return Receipt()
    monkeypatch.setattr(mqtt,"Client",Fake)
    sensor_mqtt.drain()
    assert len(sensor_mqtt.public()["outbox"])==1


def test_reuse_board_requires_disabling_previous_registration(admin):
    d,_,headers,room=setup_sensor(admin)
    new=admin.post("/api/sensors/devices",json={"name":"После ремонта","room_id":room["id"]}).json()
    body={"device_id":new["device_id"],"code":new["code"],"model":sensors.MODEL,"hardware_id":"0123456789ab"}
    assert admin.post("/api/sensors/enroll",json=body).status_code==409
    admin.patch(f'/api/sensors/devices/{d["device_id"]}',json={"name":"Старый архив","room_id":room["id"],"enabled":False})
    assert admin.post("/api/sensors/enroll",json=body).status_code==200
    assert sync(admin,d,headers,batch()).status_code==401


@pytest.mark.parametrize("kind", ["dht11", "dht22", "sht3x", "sht4x", "aht20", "bme280", "bmp280", "ds18b20"])
def test_sensor_types_enroll_sync_and_replay(admin, kind):
    d, enrollment, headers, room = setup_sensor(admin)
    enrollment["sensor_type"] = kind
    assert admin.post("/api/sensors/enroll", json=enrollment).status_code == 200
    sample = record(humidity=None if kind in ("ds18b20", "bmp280") else 0.0)
    payload = batch([sample], sensor_type=kind)
    for _ in range(2):
        response = sync(admin, d, headers, payload)
        assert response.status_code == 200, response.text
    device = admin.get("/api/sensors/devices").json()[0]
    assert device["sensor_type"] == kind
    history = admin.get(f"/api/sensors/devices/{d['device_id']}/records").json()
    assert len(history) == 1
    assert history[0]["humidity"] == sample["humidity"]
    assert len(admin.get("/api/readings").json()) == 1


def test_ds18b20_negative_temperature_and_no_fake_humidity(admin):
    d, enrollment, headers, _ = setup_sensor(admin)
    enrollment["sensor_type"] = "ds18b20"
    assert admin.post("/api/sensors/enroll", json=enrollment).status_code == 200
    payload = batch([record(temperature=-55.0, humidity=None)], sensor_type="ds18b20")
    assert sync(admin, d, headers, payload).status_code == 200
    # Full sensor archive supports -55; room-temperature journal retains its -40 limit.
    assert admin.get("/api/readings").json() == []
    payload = batch([record(2, humidity=50.0)], next_seq=3, sensor_type="ds18b20")
    assert sync(admin, d, headers, payload).status_code == 422
    with db.connect() as conn:
        device = dict(conn.execute("SELECT * FROM sensor_devices").fetchone())
    messages = dict(sensor_mqtt.messages(device, None, sensor_mqtt.MQTTConfig().model_dump()))
    topic = f"homeassistant/sensor/{d['device_id']}/humidity/config"
    assert messages[topic] == ""


def test_bmp280_temperature_only_and_humidity_must_be_null(admin):
    d, enrollment, headers, room = setup_sensor(admin)
    enrollment["sensor_type"] = "bmp280"
    assert admin.post("/api/sensors/enroll", json=enrollment).status_code == 200
    payload = batch([record(temperature=23.4, humidity=None)], sensor_type="bmp280")
    assert sync(admin, d, headers, payload).status_code == 200
    assert admin.get("/api/readings").json()[0]["value"] == 23.4
    assert admin.get(f"/api/sensors/devices/{d['device_id']}/records").json()[0]["humidity"] is None
    payload = batch([record(2, temperature=24.0, humidity=0.0)], next_seq=3, sensor_type="bmp280")
    assert sync(admin, d, headers, payload).status_code == 422
    with db.connect() as conn:
        device = dict(conn.execute("SELECT * FROM sensor_devices WHERE id=?", (d["device_id"],)).fetchone())
        assert conn.execute("SELECT count(*) FROM sensor_records WHERE device_id=?", (d["device_id"],)).fetchone()[0] == 1
    announcements = dict(sensor_mqtt.messages(device, None, sensor_mqtt.MQTTConfig().model_dump()))
    assert announcements[f"homeassistant/sensor/{d['device_id']}/humidity/config"] == ""
    assert announcements[f"homeassistant/sensor/{d['device_id']}/temperature/config"]


def test_sensor_type_mismatch_and_missing_humidity_are_atomic(admin):
    d, _, headers, _ = setup_sensor(admin)
    assert sync(admin, d, headers, batch([record(humidity=None)])).status_code == 422
    assert sync(admin, d, headers, batch(sensor_type="dht22")).status_code == 409
    assert sync(admin, d, headers, batch(sensor_type="unknown")).status_code == 422
    with db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM sensor_records").fetchone()[0] == 0
        assert conn.execute("SELECT ack_seq FROM sensor_devices").fetchone()[0] == 1
    # Legacy payload without sensor_type still works.
    assert sync(admin, d, headers, batch()).status_code == 200


def test_sensor_type_migration_preserves_existing_data(admin):
    d, _, headers, _ = setup_sensor(admin)
    assert sync(admin, d, headers, batch()).status_code == 200
    with db.connect() as conn:
        before = list(conn.execute("SELECT * FROM sensor_records"))
        conn.execute("ALTER TABLE sensor_devices DROP COLUMN sensor_type")
        conn.execute("ALTER TABLE sensor_devices DROP COLUMN last_complete_sync")
        conn.execute("PRAGMA user_version=0")
    db.initialize()
    db.initialize()
    with db.connect() as conn:
        row = conn.execute("SELECT sensor_type,last_complete_sync,last_seen FROM sensor_devices").fetchone()
        assert row["sensor_type"] == "sht4x"
        assert row["last_complete_sync"] == row["last_seen"]
        assert list(conn.execute("SELECT * FROM sensor_records")) == before


def test_enrollment_denied_while_room_disabled(admin):
    room = db.create_room('Школа', '1', 'Закрыт', 'Кабинет', 18, 24)
    device = admin.post('/api/sensors/devices', json={'name': 'Новый', 'room_id': room['id']}).json()
    enrollment = {key: device[key] for key in ('device_id', 'code')}
    enrollment.update(hardware_id='0123456789ab', model=sensors.MODEL)
    with db.connect() as conn:
        conn.execute('UPDATE rooms SET active=0 WHERE id=?', (room['id'],))
    response = admin.post('/api/sensors/enroll', json=enrollment)
    assert response.status_code == 403
    assert 'token' not in response.json()
    with db.connect() as conn:
        saved = conn.execute('SELECT token_hash,hardware_id FROM sensor_devices WHERE id=?', (device['device_id'],)).fetchone()
        assert tuple(saved) == (None, None)
        conn.execute('UPDATE rooms SET active=1 WHERE id=?', (room['id'],))
    assert admin.post('/api/sensors/enroll', json=enrollment).status_code == 200


def test_relaxed_intervals_and_voltage_limits():
    config = sensors.Config(measure_s=60, send_s=1, cool_s=0, wifi_budget_s=300,
                            cutoff_mv=3000, wifi_min_mv=0, ota_min_mv=3300)
    assert config.send_s == 1 and config.wifi_min_mv == 0
    assert sensors.Record.model_validate(record(interval_s=1)).interval_s == 1
    assert sensors.Record.model_validate(record(interval_s=604800)).interval_s == 604800
    for kwargs in ({"measure_s":0}, {"send_s":0}, {"cutoff_mv":2999},
                   {"cutoff_mv":3300,"wifi_min_mv":3200}, {"ota_min_mv":3299}):
        with pytest.raises(ValueError):
            sensors.Config(**kwargs)
