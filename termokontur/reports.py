"""Coverage, climate and historical staff assignments by local school days."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from . import db, school_calendar, sensor_monitor, settings

MAX_DAYS = 366
MAX_READINGS = 500_000


def period(start: str, end: str, now: datetime | None = None, *,
           zone: ZoneInfo | None = None) -> tuple[date, date, list[str], str, str, ZoneInfo]:
    try:
        a, b = date.fromisoformat(start), date.fromisoformat(end)
    except (TypeError, ValueError) as exc:
        raise ValueError("Укажите даты начала и окончания в формате ГГГГ-ММ-ДД") from exc
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    zone = zone or settings.local_zone()
    today = instant.astimezone(zone).date()
    if a > b or (b - a).days >= MAX_DAYS or b > today:
        raise ValueError("Выберите период до сегодняшнего дня, не более 366 дней")
    days = [(a + timedelta(days=i)).isoformat() for i in range((b-a).days + 1)]
    first = datetime.combine(a, time.min, tzinfo=zone).astimezone(timezone.utc)
    last = datetime.combine(b + timedelta(days=1), time.min, tzinfo=zone).astimezone(timezone.utc)
    return a, b, days, first.isoformat(timespec="seconds"), last.isoformat(timespec="seconds"), zone


def _average(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 2) if values else None


def _display(first: str | None, last: str | None, username: str | None, uid: str) -> str:
    return (" ".join(x for x in (first, last) if x).strip()
            or ("@" + username if username else "MAX ID " + uid))


def build(start: str, end: str, building: str = "", now: datetime | None = None, *,
          zone: ZoneInfo | None = None) -> dict:
    """A stable report snapshot with no per-room N+1 database queries."""
    first, _, days, start_utc, end_utc, zone = period(start, end, now, zone=zone)
    dates = [first + timedelta(days=offset) for offset in range(len(days) + 1)]
    boundaries = [datetime.combine(day, time.min, tzinfo=zone).astimezone(timezone.utc)
                  .isoformat(timespec="seconds") for day in dates]
    epochs = [int(datetime.fromisoformat(boundary).timestamp()) for boundary in boundaries]
    now_epoch = int((now or datetime.now(timezone.utc)).timestamp())
    calendar, weekdays = school_calendar.load()
    day_set = set(days)
    day_index = {name: index for index, name in enumerate(days)}
    with db.connect() as conn:
        conn.execute("BEGIN")
        rooms = [dict(row) for row in conn.execute(
            """SELECT id, building, floor, name, min_temp, max_temp,
                      MIN(created_at, COALESCE(
                        (SELECT MIN(observed_at) FROM readings WHERE room_id=rooms.id),
                        created_at
                      )) AS started_at
               FROM rooms WHERE (?='' OR building=?)
               ORDER BY building COLLATE NOCASE, name COLLATE NOCASE, id""",
            (building, building),
        )]
        activity = defaultdict(list)
        for row in conn.execute("""
            SELECT a.room_id,a.changed_at,a.active FROM room_activity a
            JOIN rooms p ON p.id=a.room_id
            WHERE a.changed_at<? AND (?='' OR p.building=?)
            ORDER BY a.room_id,a.changed_at,a.id
        """, (end_utc, building, building)):
            activity[row["room_id"]].append((row["changed_at"], bool(row["active"])))
        profiles = {str(row["user_id"]): _display(
            row["first_name"], row["last_name"], row["username"], str(row["user_id"])
        ) for row in conn.execute("""
            SELECT user_id, first_name, last_name, username FROM max_users
        """)}
        # Bounded by one-year date window; refuse exceptionally large exports.
        measurements = conn.execute("""
            SELECT r.id, r.room_id, r.value, r.source, r.reporter, r.observed_at
            FROM readings r JOIN rooms p ON p.id=r.room_id
            WHERE (?='' OR p.building=?)
              AND r.observed_at >= ? AND r.observed_at < ?
            ORDER BY r.observed_at, r.id
        """, (building, building, start_utc, end_utc))
        readings = []
        for row in measurements:
            if len(readings) >= MAX_READINGS:
                raise ValueError("Слишком много измерений. Уменьшите период отчёта")
            readings.append(dict(row))
        assignments = list(conn.execute("""
            SELECT a.user_id,a.room_id,a.starts_at,a.ends_at
            FROM max_binding_periods a JOIN rooms p ON p.id=a.room_id
            WHERE (?='' OR p.building=?) AND a.starts_at<? AND (a.ends_at IS NULL OR a.ends_at>?)
                  AND (a.ends_at IS NULL OR a.starts_at<a.ends_at)
            ORDER BY a.user_id, p.building, p.name, a.starts_at
        """, (building, building, end_utc, start_utc)))
        history_start = conn.execute("SELECT value FROM app_state WHERE key='binding_history_since'").fetchone()
        devices = sensor_monitor.by_room(conn, building)
        humidity = {}
        for row in conn.execute("""
            SELECT r.room_id,r.measured_at,r.humidity FROM sensor_records r JOIN rooms p ON p.id=r.room_id
            WHERE r.measured_at>=? AND r.measured_at<? AND r.humidity IS NOT NULL
              AND r.error='' AND (?='' OR p.building=?)
        """, (epochs[0], epochs[-1], building, building)):
            local_date = datetime.fromtimestamp(row["measured_at"], zone).date().isoformat()
            value = row["humidity"]
            stats = humidity.setdefault((row["room_id"], local_date), [0.0, 0, value, value])
            stats[0] += value
            stats[1] += 1
            stats[2] = min(stats[2], value)
            stats[3] = max(stats[3], value)
    room_map = {r["id"]: r for r in rooms}
    # One value per classroom and calendar day for fair building averages.
    samples = defaultdict(list)
    authors = defaultdict(set)
    authored = defaultdict(int)
    for reading in readings:
        dt = datetime.fromisoformat(reading["observed_at"])
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        day = dt.astimezone(zone).date().isoformat()
        if day not in day_set:
            continue
        key = (reading["room_id"], day)
        samples[key].append(reading)
        if reading["source"] == "max" and str(reading["reporter"]).startswith("MAX:"):
            uid = reading["reporter"][4:]
            authors[(uid, day)].add(reading["room_id"])
            authored[uid] += 1

    daily_rows = []
    room_daily = []
    missing = []
    by_building_day = defaultdict(list)
    by_day = defaultdict(list)
    by_room = defaultdict(list)
    room_totals = defaultdict(list)
    building_rooms = defaultdict(list)
    schedules = {name: calendar.for_building(name, weekdays) for name in {room["building"] for room in rooms}}
    for room in rooms:
        events = iter(activity[room["id"]])
        change = next(events, None)
        active = True
        for i, day in enumerate(days):
            begins, ends = boundaries[i:i + 2]
            while change and change[0] <= begins:
                active = change[1]
                change = next(events, None)
            active_any = active
            while change and change[0] < ends:
                active = change[1]
                active_any |= active
                change = next(events, None)
            scheduled = room["started_at"] < ends and active_any
            entries = samples.get((room["id"], day), [])
            wet = humidity.get((room["id"], day))
            if not scheduled and not entries and not wet:
                continue
            expected = scheduled and schedules[room["building"]](dates[i])
            waiting = sensor_monitor.waiting_until(devices.get(room["id"], ()), epochs[i], epochs[i + 1],
                                                 now_epoch) if expected else None
            values = [float(x["value"]) for x in entries]
            average = _average(values)
            minimum = min(values) if values else None
            maximum = max(values) if values else None
            abnormal = bool(values and not room["min_temp"] <= average <= room["max_temp"])
            raw_deviations = sum(not room["min_temp"] <= v <= room["max_temp"] for v in values)
            latest = entries[-1] if entries else None
            reporter_name = None
            if latest:
                if latest["source"] == "max":
                    uid = str(latest["reporter"])[4:] if str(latest["reporter"]).startswith("MAX:") else ""
                    reporter_name = profiles.get(uid, "MAX ID " + uid)
                elif latest["source"] == "ha":
                    reporter_name = "Автоматический датчик"
                else:
                    reporter_name = "Администратор панели"
            record = {
                "date": day, "room_id": room["id"], "building": room["building"],
                "room": room["name"], "average": average,
                "minimum": minimum, "maximum": maximum,
                "humidity_average": round(wet[0] / wet[1], 2) if wet else None,
                "humidity_minimum": wet[2] if wet else None, "humidity_maximum": wet[3] if wet else None,
                "humidity_readings": wet[1] if wet else 0,
                "readings": len(entries), "deviations": raw_deviations,
                "expected": expected,
                "waiting_until": waiting,
                "out_of_range": abnormal,
                "source": latest["source"] if latest else None,
                "reporter": latest["reporter"] if latest else None,
                "reporter_name": reporter_name,
                "measured_at": latest["observed_at"] if latest else None,
                "min_limit": room["min_temp"], "max_limit": room["max_temp"],
            }
            room_daily.append(record)
            by_room[room["id"]].append(record)
            by_day[day].append(record)
            by_building_day[(room["building"], day)].append(record)
            if values:
                room_totals[room["id"]].append(average)
            elif expected and waiting is None:
                missing.append({"date": day, "room_id": room["id"],
                                "building": room["building"], "room": room["name"]})
        if by_room[room["id"]]:
            building_rooms[room["building"]].append(room)

    def aggregate(rows: list[dict]) -> dict:
        measured = [r for r in rows if r["average"] is not None]
        humidity_rows = [r for r in rows if r["humidity_average"] is not None]
        expected_count = sum(r["expected"] for r in rows)
        completed = sum(r["expected"] for r in measured)
        awaiting = sum(r["expected"] and r["average"] is None and r["waiting_until"] is not None for r in rows)
        return {
            "rooms_total": expected_count, "rooms_measured": completed,
            "missing": expected_count - completed - awaiting, "awaiting": awaiting,
            "sync_pending": sum(r["waiting_until"] is not None for r in rows),
            "coverage": round(100 * completed / expected_count, 1) if expected_count else None,
            "average": _average([r["average"] for r in measured]),
            "minimum": min((r["minimum"] for r in measured), default=None),
            "maximum": max((r["maximum"] for r in measured), default=None),
            "humidity_average": _average([r["humidity_average"] for r in humidity_rows]),
            "humidity_minimum": min((r["humidity_minimum"] for r in humidity_rows), default=None),
            "humidity_maximum": max((r["humidity_maximum"] for r in humidity_rows), default=None),
            "humidity_readings": sum(r["humidity_readings"] for r in humidity_rows),
            "humidity_rooms": len(humidity_rows),
            "out_of_range": sum(r["out_of_range"] for r in measured),
            "deviations": sum(r["deviations"] for r in measured),
            "readings": sum(r["readings"] for r in measured),
        }

    for day in days:
        entry = {"date": day, **aggregate(by_day[day])}
        daily_rows.append(entry)
    building_daily = []
    for name in sorted(building_rooms):
        for day in days:
            building_daily.append({"building": name, "date": day,
                                   **aggregate(by_building_day[(name, day)])})
    building_summary = []
    for name in sorted(building_rooms):
        matches = [row for row in building_daily if row["building"] == name]
        values = [row["average"] for row in matches if row["average"] is not None]
        humidity_days = [row for row in matches if row["humidity_average"] is not None]
        expected = sum(row["rooms_total"] for row in matches)
        building_summary.append({
            "building": name, "rooms": len(building_rooms[name]),
            "average": _average(values),
            "humidity_average": _average([r["humidity_average"] for r in humidity_days]),
            "humidity_minimum": min((r["humidity_minimum"] for r in humidity_days), default=None),
            "humidity_maximum": max((r["humidity_maximum"] for r in humidity_days), default=None),
            "humidity_readings": sum(r["humidity_readings"] for r in humidity_days),
            "coverage": round(100 * sum(row["rooms_measured"] for row in matches) / expected, 1)
                        if expected else None,
            "missing": sum(row["missing"] for row in matches),
            "awaiting": sum(row["awaiting"] for row in matches),
            "out_of_range": sum(row["out_of_range"] for row in matches),
        })
    room_stats = []
    for room in rooms:
        records = by_room[room["id"]]
        if not records:
            continue
        measured = [r for r in records if r["average"] is not None]
        expected = sum(r["expected"] for r in records)
        completed = sum(r["expected"] for r in measured)
        awaiting = sum(r["average"] is None and r["waiting_until"] is not None for r in records)
        room_stats.append({
            "room_id": room["id"], "building": room["building"], "room": room["name"],
            "days_total": expected, "days_measured": completed,
            "days_missing": expected - completed - awaiting, "days_awaiting": awaiting,
            "coverage": round(100 * completed / expected, 1) if expected else None,
            "average": _average([r["average"] for r in measured]),
            "minimum": min((r["minimum"] for r in measured), default=None),
            "maximum": max((r["maximum"] for r in measured), default=None),
            "out_of_range_days": sum(r["out_of_range"] for r in measured),
            "deviations": sum(r["deviations"] for r in records),
        })
    assignment_rooms = defaultdict(set)
    assignment_days = defaultdict(dict)
    for assignment in assignments:
        uid, room_id = str(assignment["user_id"]), assignment["room_id"]
        for record in by_room[room_id]:
            index = day_index[record["date"]]
            if assignment["starts_at"] >= boundaries[index + 1] or (
                assignment["ends_at"] and assignment["ends_at"] <= boundaries[index]
            ):
                continue
            assignment_rooms[uid].add(room_id)
            if record["expected"]:
                assignment_days[uid][(room_id, record["date"])] = record
    staff = []
    for uid in assignment_rooms.keys() | authored.keys():
        assigned = assignment_rooms[uid]
        names = sorted(f"{room_map[room_id]['building']} / {room_map[room_id]['name']}" for room_id in assigned)
        slots = assignment_days[uid]
        completed = sum(record["average"] is not None for record in slots.values())
        awaiting = sum(record["average"] is None and record["waiting_until"] is not None for record in slots.values())
        own = sum(len(authors[(uid, day)]) for day in days)
        staff.append({
            "user_id": uid, "name": profiles.get(uid, "MAX ID " + uid),
            "rooms": names, "rooms_count": len(assigned), "assigned_room_days": len(slots),
            "measured_room_days": completed,
            "missing_room_days": len(slots) - completed - awaiting, "awaiting_room_days": awaiting,
            "coverage": round(100 * completed / len(slots), 1) if slots else None,
            "authored_readings": authored[uid], "own_room_days": own,
            "days_with_submission": sum(bool(authors[(uid,day)]) for day in days),
        })
    staff.sort(key=lambda u:(u["name"].casefold(),u["user_id"]))
    sums = aggregate(room_daily)
    history_since = datetime.fromisoformat(history_start[0]).astimezone(zone).date().isoformat() if history_start else None
    note = ("Сдача учитывает школьный календарь; температуру может передать любой закреплённый сотрудник "
            "или датчик. Пороги задаёт администратор.")
    if history_since and start < history_since:
        note += f" Даты назначений сохраняются с {history_since}; за более ранние дни доступны записи о внесённых показаниях."
    return {
        "from": start, "to": end, "timezone": str(zone), "building": building,
        "generated_at": db.utc_now(), "days": len(days), "rooms_count": len(room_stats),
        "summary": sums, "daily": daily_rows, "building_summary": building_summary,
        "building_daily": building_daily, "rooms": room_stats,
        "room_daily": room_daily, "staff": staff, "missing": missing,
        "staff_history_since": history_since,
        "note": note,
    }


def max_summary(report: dict) -> str:
    """Compact text summary for the responsible employee's MAX dialog."""
    summary = report["summary"]
    lines = [
        "📊 Термоконтур · Сводка по температуре",
        f"Период: {report['from']} — {report['to']}",
        f"Корпус: {report['building'] or 'Все здания'}",
    ]
    if summary["rooms_total"]:
        lines.append(f"Кабинето-дней с измерениями: {summary['rooms_measured']} из "
                     f"{summary['rooms_total']} ({summary['coverage']}%).")
        lines.append(f"Без измерений: {summary['missing']}.")
        if summary["awaiting"]:
            lines.append(f"Ожидают выгрузку: {summary['awaiting']}.")
    else:
        lines.append("По школьному календарю замеры за этот период не требовались.")
    lines.append(f"Отклонений по среднему: {summary['out_of_range']}.")
    if summary["average"] is not None:
        lines.append(f"Средняя температура: {summary['average']:g} °C.")
    for row in report["building_summary"][:8]:
        if row["coverage"] is not None:
            lines.append(f"• {row['building']}: {row['coverage']:g}% охват, {row['missing']} пропусков")
    lines.append("Подробный отчёт и выгрузки Excel/PDF доступны администратору в панели.")
    return "\n".join(lines)[:1950]
