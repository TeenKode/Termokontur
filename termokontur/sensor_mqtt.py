"""Optional MQTT Discovery bridge with a durable coalescing outbox."""
from __future__ import annotations
import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from pydantic import Field, model_validator
from . import db, sensors

log = logging.getLogger(__name__)

class MQTTConfig(sensors.Strict):
    enabled: bool = False
    host: str = Field(default="", max_length=253, pattern=r"^[A-Za-z0-9.:-]*$")
    port: int = Field(default=8883, ge=1, le=65535)
    tls: bool = True
    username: str = Field(default="", max_length=128)
    password: str | None = Field(default=None, max_length=256)
    discovery_prefix: str = Field(default="homeassistant", pattern=r"^[A-Za-z0-9_-]{1,64}$")

    @model_validator(mode="after")
    def host_required(self):
        if self.enabled and not self.host:
            raise ValueError("Укажите MQTT-сервер")
        return self


def configuration():
    with db.connect() as conn:
        row = conn.execute("SELECT data FROM sensor_mqtt_settings WHERE id=1").fetchone()
    return json.loads(row[0]) if row else MQTTConfig().model_dump()


def public():
    data = configuration()
    data["password_configured"] = bool(data.pop("password", None))
    with db.connect() as conn:
        data["outbox"] = [dict(x) for x in conn.execute("SELECT device_id,attempts,error,next_retry FROM sensor_mqtt_outbox")]
    return data


def configure(payload):
    data = payload.model_dump()
    if data["password"] is None:
        data["password"] = configuration().get("password")
    with db.connect() as conn:
        conn.execute("INSERT INTO sensor_mqtt_settings VALUES(1,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                     (json.dumps(data),))
        conn.execute("""INSERT INTO sensor_mqtt_outbox(device_id) SELECT id FROM sensor_devices WHERE 1
                     ON CONFLICT(device_id) DO UPDATE SET generation=generation+1,next_retry=0""")
    return public()


def messages(device, latest, config):
    ident = device["id"]
    base = "termokontur/" + ident
    common = {"identifiers": [ident], "name": device["name"], "manufacturer": "Термоконтур",
              "model": sensors.MODEL, "sw_version": device["firmware"]}
    for key, name, unit, device_class in (
        ("temperature", "Температура", "°C", "temperature"),
        ("humidity", "Влажность", "%", "humidity"),
        ("voltage", "Напряжение аккумулятора", "V", "voltage"),
        ("rssi", "Сигнал Wi-Fi", "dBm", "signal_strength"),
    ):
        discovery = {"name": name, "unique_id": ident+"_"+key, "state_topic": base+"/state",
                     "value_template": "{{ value_json."+key+" }}", "device_class": device_class,
                     "unit_of_measurement": unit, "device": common,
                     "availability_topic": base+"/availability", "state_class": "measurement"}
        yield config["discovery_prefix"]+"/sensor/"+ident+"/"+key+"/config", json.dumps(discovery, ensure_ascii=False)
    state = {"voltage": device["battery_mv"]/1000 if device["battery_mv"] else None,
             "rssi": device["rssi"], "measured_at": None, "temperature": None, "humidity": None}
    if latest:
        state.update({key: latest[key] for key in ("temperature", "humidity")})
        if latest["measured_at"]:
            state["measured_at"] = datetime.fromtimestamp(latest["measured_at"], timezone.utc).isoformat()
    yield base+"/state", json.dumps(state)
    status = sensors.public_device(device)["status"]
    yield base+"/availability", "offline" if status in ("stale", "disabled", "not_enrolled", "waiting") else "online"


def drain():
    config = configuration()
    if not config["enabled"]:
        return
    import paho.mqtt.client as mqtt
    with db.connect() as conn:
        # Also refresh stale availability once per worker cycle after successful sends.
        for row in conn.execute("SELECT * FROM sensor_devices WHERE last_seen IS NOT NULL"):
            if sensors.public_device(row)["status"] in ("stale", "disabled"):
                conn.execute("INSERT OR IGNORE INTO sensor_mqtt_outbox(device_id) VALUES(?)", (row["id"],))
        pending = [dict(x) for x in conn.execute("SELECT * FROM sensor_mqtt_outbox WHERE next_retry<=? LIMIT 20", (int(time.time()),))]
    if not pending:
        return
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="termokontur-"+sensors.digest(str(db.db_path().resolve()))[:12])
    if config["username"]:
        client.username_pw_set(config["username"], config.get("password"))
    if config["tls"]:
        client.tls_set()
    client.connect_timeout = 5
    connected = False
    try:
        client.connect(config["host"], config["port"], keepalive=30)
        client.loop_start()
        connected = True
        for item in pending:
            with db.connect() as conn:
                device = sensors.get_device(conn, item["device_id"])
                latest = conn.execute("SELECT * FROM sensor_records WHERE device_id=? AND error='' ORDER BY seq DESC LIMIT 1", (device["id"],)).fetchone()
            for topic, payload in messages(device, latest, config):
                receipt = client.publish(topic, payload, qos=1, retain=True)
                receipt.wait_for_publish(timeout=5)
                if not receipt.is_published():
                    raise TimeoutError("MQTT PUBACK timeout")
            with db.connect() as conn:
                # A simultaneous sync must keep its newer generation queued.
                conn.execute("DELETE FROM sensor_mqtt_outbox WHERE device_id=? AND generation=?", (item["device_id"], item["generation"]))
    except Exception as exc:
        log.warning("Sensor MQTT publish failed: %s", type(exc).__name__)
        with db.connect() as conn:
            for item in pending:
                attempts = item["attempts"] + 1
                conn.execute("UPDATE sensor_mqtt_outbox SET attempts=?,next_retry=?,error=? WHERE device_id=? AND generation=?",
                             (attempts, int(time.time())+min(3600,30*2**min(attempts,7)), type(exc).__name__, item["device_id"], item["generation"]))
    finally:
        if connected:
            client.disconnect()
            client.loop_stop()


async def worker():
    while True:
        await asyncio.sleep(30)
        try:
            await asyncio.to_thread(drain)
        except Exception:
            log.exception("Sensor MQTT worker failed")
