"""Daily coverage, temperature and staff reports for active school rooms.

Historical room assignments are not currently stored; employee coverage is
calculated from *current* bindings and must not be used for disciplinary claims.
All calendar dates are interpreted in the organisation's IANA timezone.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from . import db, settings

MAX_DAYS = 366
MAX_READINGS = 500_000


def period(start: str, end: str, now: datetime | None = None) -> tuple[date, date, list[str], str, str, ZoneInfo]:
    try:
        a, b = date.fromisoformat(start), date.fromisoformat(end)
    except (TypeError, ValueError) as exc:
        raise ValueError("Укажите даты начала и окончания в формате ГГГГ-ММ-ДД") from exc
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    zone = ZoneInfo(settings.get("timezone", "UTC") or "UTC")
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


def build(start: str, end: str, building: str = "", now: datetime | None = None) -> dict:
    """A stable report snapshot with no per-room N+1 database queries."""
    _, _, days, start_utc, end_utc, zone = period(start, end, now)
    with db.connect() as conn:
        rooms = [dict(row) for row in conn.execute(
            """SELECT id, building, floor, name, min_temp, max_temp
               FROM rooms WHERE active=1 AND (?='' OR building=?)
               ORDER BY building COLLATE NOCASE, name COLLATE NOCASE, id""",
            (building, building),
        )]
        room_ids = {room["id"] for room in rooms}
        profiles = {str(row["user_id"]): _display(
            row["first_name"], row["last_name"], row["username"], str(row["user_id"])
        ) for row in conn.execute("""
            SELECT user_id, first_name, last_name, username FROM max_users
        """)}
        # Bounded by one-year date window; refuse exceptionally large exports.
        measurements = conn.execute("""
            SELECT r.id, r.room_id, r.value, r.source, r.reporter, r.observed_at
            FROM readings r JOIN rooms p ON p.id=r.room_id
            WHERE p.active=1 AND (?='' OR p.building=?)
              AND r.observed_at >= ? AND r.observed_at < ?
            ORDER BY r.observed_at, r.id
        """, (building, building, start_utc, end_utc))
        readings = []
        for row in measurements:
            if len(readings) >= MAX_READINGS:
                raise ValueError("Слишком много измерений. Уменьшите период отчёта")
            readings.append(dict(row))
        people = [dict(row) for row in conn.execute("""
            SELECT b.user_id, b.room_id,
                   COALESCE(u.first_name,'') first_name,
                   COALESCE(u.last_name,'') last_name,
                   COALESCE(u.username,'') username
            FROM max_bindings b JOIN rooms p ON p.id=b.room_id
            LEFT JOIN max_users u ON u.user_id=b.user_id
            WHERE p.active=1 AND (?='' OR p.building=?)
            ORDER BY b.user_id, p.building, p.name
        """, (building, building))]
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
        if reading["room_id"] not in room_ids or day not in days:
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
    by_room = defaultdict(list)
    room_totals = defaultdict(list)
    building_rooms = defaultdict(list)
    for room in rooms:
        building_rooms[room["building"]].append(room)
        for day in days:
            entries = samples.get((room["id"], day), [])
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
                "readings": len(entries), "deviations": raw_deviations,
                "out_of_range": abnormal,
                "source": latest["source"] if latest else None,
                "reporter": latest["reporter"] if latest else None,
                "reporter_name": reporter_name,
                "measured_at": latest["observed_at"] if latest else None,
                "min_limit": room["min_temp"], "max_limit": room["max_temp"],
            }
            room_daily.append(record)
            by_room[room["id"]].append(record)
            by_building_day[(room["building"], day)].append(record)
            if values:
                room_totals[room["id"]].append(average)
            else:
                missing.append({"date": day, "room_id": room["id"],
                                "building": room["building"], "room": room["name"]})

    def aggregate(rows: list[dict]) -> dict:
        measured = [r for r in rows if r["average"] is not None]
        return {
            "rooms_total": len(rows), "rooms_measured": len(measured),
            "missing": len(rows) - len(measured),
            "coverage": round(100 * len(measured) / len(rows), 1) if rows else 0,
            "average": _average([r["average"] for r in measured]),
            "minimum": min((r["minimum"] for r in measured), default=None),
            "maximum": max((r["maximum"] for r in measured), default=None),
            "out_of_range": sum(r["out_of_range"] for r in measured),
            "deviations": sum(r["deviations"] for r in measured),
            "readings": sum(r["readings"] for r in measured),
        }

    for day in days:
        entry = {"date": day, **aggregate([r for r in room_daily if r["date"] == day])}
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
        building_summary.append({
            "building": name, "rooms": len(building_rooms[name]),
            "average": _average(values),
            "coverage": round(sum(row["coverage"] for row in matches)/len(matches), 1),
            "missing": sum(row["missing"] for row in matches),
            "out_of_range": sum(row["out_of_range"] for row in matches),
        })
    room_stats = []
    for room in rooms:
        records = by_room[room["id"]]
        measured = [r for r in records if r["average"] is not None]
        room_stats.append({
            "room_id": room["id"], "building": room["building"], "room": room["name"],
            "days_total": len(days), "days_measured": len(measured),
            "days_missing": len(days)-len(measured),
            "coverage": round(100*len(measured)/len(days),1),
            "average": _average([r["average"] for r in measured]),
            "minimum": min((r["minimum"] for r in measured), default=None),
            "maximum": max((r["maximum"] for r in measured), default=None),
            "out_of_range_days": sum(r["out_of_range"] for r in measured),
            "deviations": sum(r["deviations"] for r in records),
        })
    employees = {}
    for p in people:
        uid = str(p["user_id"])
        user = employees.setdefault(uid, {
            "user_id": uid, "name": _display(p["first_name"],p["last_name"],p["username"],uid),
            "room_ids": set(), "rooms": [],
        })
        if p["room_id"] not in user["room_ids"]:
            user["room_ids"].add(p["room_id"])
            r = room_map[p["room_id"]]
            user["rooms"].append(r["building"] + " / " + r["name"])
    staff = []
    for uid, user in employees.items():
        assigned = user.pop("room_ids")
        slots = [r for r in room_daily if r["room_id"] in assigned]
        measured = [r for r in slots if r["average"] is not None]
        own = sum(len(authors[(uid,day)]) for day in days)
        staff.append({
            **user, "rooms_count": len(assigned), "assigned_room_days": len(slots),
            "measured_room_days": len(measured),
            "missing_room_days": len(slots)-len(measured),
            "coverage": round(100*len(measured)/len(slots),1) if slots else 0,
            "authored_readings": authored[uid], "own_room_days": own,
            "days_with_submission": sum(bool(authors[(uid,day)]) for day in days),
        })
    staff.sort(key=lambda u:(u["name"].casefold(),u["user_id"]))
    sums = aggregate(room_daily)
    return {
        "from": start, "to": end, "timezone": str(zone), "building": building,
        "generated_at": db.utc_now(), "days": len(days), "rooms_count": len(rooms),
        "summary": sums, "daily": daily_rows, "building_summary": building_summary,
        "building_daily": building_daily, "rooms": room_stats,
        "room_daily": room_daily, "staff": staff, "missing": missing,
        "note": ("Текущие закрепления сотрудников используются для исторических периодов. "
                 "Отсутствие личного ввода не означает нарушение: кабинет мог измерить другой "
                 "сотрудник или автоматический датчик. Нормы — заданные администратором пороги."),
    }


def max_summary(report: dict) -> str:
    """Compact text summary for the responsible employee's MAX dialog."""
    summary = report["summary"]
    lines = [
        "📊 Термоконтур · Сводка по температуре",
        f"Период: {report['from']} — {report['to']}",
        f"Корпус: {report['building'] or 'Все здания'}",
        f"Кабинето-дней с измерениями: {summary['rooms_measured']} из {summary['rooms_total']} ({summary['coverage']}%).",
        f"Без измерений: {summary['missing']}. Отклонений по среднему: {summary['out_of_range']}.",
    ]
    if summary["average"] is not None:
        lines.append(f"Средняя температура: {summary['average']:g} °C.")
    for row in report["building_summary"][:8]:
        lines.append(f"• {row['building']}: {row['coverage']:g}% охват, {row['missing']} пропусков")
    lines.append("Подробный отчёт и выгрузки Excel/PDF доступны администратору в панели.")
    return "\n".join(lines)[:1950]
