"""Expected upload times for sleeping sensors and the rooms they cover."""
from collections import defaultdict
from datetime import datetime, timezone
import json
import secrets

from . import db, settings


def upload_times(device: dict, *, complete: bool = False) -> tuple[int, int]:
    config = device["config"]
    seen = device["last_complete_sync"] if complete else device["last_seen"]
    last = seen or int(datetime.fromisoformat(device["created_at"]).timestamp())
    scheduled = last + config["send_s"]
    grace = min(3600, max(900, config["measure_s"] * 2))
    return scheduled, scheduled + grace


def battery_warning(device: dict) -> bool:
    battery = device["battery_mv"]
    return bool(battery and battery <= min(4200, max(device["config"]["cutoff_mv"], device["config"]["wifi_min_mv"]) + 150))


def by_room(conn, building="") -> dict[int, list[dict]]:
    groups = defaultdict(list)
    for row in conn.execute("""
        SELECT d.id,d.name,d.room_id,d.config,d.created_at,d.last_seen,d.last_complete_sync,
               d.battery_mv,r.building,r.name AS room
        FROM sensor_devices d JOIN rooms r ON r.id=d.room_id
        WHERE d.token_hash IS NOT NULL AND d.enabled=1 AND r.active=1 AND (?='' OR r.building=?)
    """, (building, building)):
        device = dict(row)
        device["config"] = json.loads(device["config"])
        if device["config"]["enabled"]:
            groups[device["room_id"]].append(device)
    return groups


def waiting_until(devices, day_start: int, day_end: int, now: int) -> int | None:
    deadlines = []
    for device in devices:
        created = int(datetime.fromisoformat(device["created_at"]).timestamp())
        if created >= day_end or (device["last_complete_sync"] or 0) >= day_end:
            continue
        _, deadline = upload_times(device, complete=True)
        if deadline > max(now, day_start):
            deadlines.append(deadline)
    return max(deadlines, default=None)


def queue_alerts(now: datetime | None = None) -> int:
    """Queue each active sensor condition once per subscribed MAX recipient."""
    if not settings.get("max_token"):
        return 0
    instant = now or datetime.now(timezone.utc)
    stamp = instant.astimezone(timezone.utc).isoformat(timespec="seconds")
    epoch = int(instant.timestamp())
    count = 0
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        recipients = defaultdict(set)
        for row in conn.execute("SELECT building,max_user_id FROM building_accounts WHERE max_user_id IS NOT NULL"):
            recipients[row["building"]].add(row["max_user_id"])
        active = set()
        existing = {(r["device_id"], r["user_id"], r["issue"]): r["event_id"]
                    for r in conn.execute("SELECT * FROM sensor_alert_state")}
        for devices in by_room(conn).values() if recipients else ():
            for sensor in devices:
                _, deadline = upload_times(sensor)
                overdue = epoch > deadline
                issue = "offline" if overdue else "battery" if battery_warning(sensor) else None
                if issue is None:
                    continue
                detail = ("пропущен сеанс связи" if overdue else
                          f"низкое напряжение аккумулятора ({sensor['battery_mv']/1000:.2f} В)")
                message = (f"⚠️ Термоконтур · Датчик\n{sensor['building']} / {sensor['room']}\n"
                           f"{sensor['name']} · {detail}.")
                for recipient in recipients[sensor["building"]]:
                    key = (sensor["id"], recipient, issue)
                    active.add(key)
                    if key in existing:
                        continue
                    event_id = "sensor-alert:" + secrets.token_hex(16)
                    conn.execute("INSERT INTO max_inbox(event_id,state,updated_at) VALUES(?,'complete',?)",
                                 (event_id, stamp))
                    conn.execute("INSERT INTO max_outbox(event_id,user_id,text,next_retry_at) VALUES(?,?,?,?)",
                                 (event_id, recipient, message, stamp))
                    conn.execute("INSERT INTO max_outbox_ui(event_id,delivery_mode,notify) VALUES(?,'notice',1)",
                                 (event_id,))
                    conn.execute("INSERT INTO sensor_alert_state VALUES(?,?,?,?)", (*key, event_id))
                    count += 1
        for key in existing.keys() - active:
            conn.execute("DELETE FROM max_outbox WHERE event_id=? AND delivered_at IS NULL", (existing[key],))
            conn.execute("DELETE FROM sensor_alert_state WHERE device_id=? AND user_id=? AND issue=?", key)
    return count
