"""Callback-driven daily temperature entry for the MAX messenger.

Buttons are attached to every bot reply; only authorised, active room bindings
can be written. Each room shares a single current daily MAX reading across its
assigned users. Corrections preserve the original reporter and append an audit
record, rather than introducing another daily measurement.
"""
from __future__ import annotations

import hashlib
import math
import re

from . import daily, db, settings

TEMPERATURES = [18 + n * 0.5 for n in range(17)]
NUMBER = re.compile(r"[-+]?\d{1,2}(?:[.,]\d{1,2})?")
CODE = re.compile(r"[A-Fa-f0-9]{16}")
PER_PAGE = 20


def _today() -> tuple[str, str, str]:
    day, start, end = daily.daily_bounds()
    return day.strftime("%Y%m%d"), start, end


def _session(uid: str) -> dict:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT mode, room_id, page, local_day FROM max_user_sessions WHERE user_id=?",
            (str(uid),),
        ).fetchone()
    return dict(row) if row else {"mode": "", "room_id": None, "page": 0, "local_day": ""}


def _set(uid: str, mode: str = "", room_id: int | None = None, page: int = 0) -> None:
    day = _today()[0]
    with db.connect() as conn:
        conn.execute("""
            INSERT INTO max_user_sessions(user_id, mode, room_id, page, local_day)
            VALUES(?,?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET mode=excluded.mode,
                room_id=excluded.room_id, page=excluded.page, local_day=excluded.local_day
        """, (str(uid), mode, room_id, page, day))


def _btn(title: str, payload: str) -> dict:
    return {"type": "callback", "text": title, "payload": payload}


def menu_buttons(uid: str) -> list[list[dict]]:
    """A fresh menu is included even after errors and reminder/alert messages."""
    rows = [
        [_btn("Отправить за сегодня", "tk:send")],
        [_btn("Изменить за сегодня", "tk:change")],
        [_btn("Показания", "tk:today"), _btn("Кабинеты", "tk:rooms")],
    ]
    s = _session(uid)
    if s["local_day"] != _today()[0]:
        return rows
    mode = s["mode"]
    if mode in ("send_room", "change_room"):
        rooms = db.bound_rooms(uid)
        page = s["page"]
        status = {room["id"]: room for room in daily.summary()["rooms"]}
        for room in rooms[page * PER_PAGE:(page + 1) * PER_PAGE]:
            label = f"{room['building']} / {room['name']}"[:42]
            today = status.get(room["id"])
            if today and today["daily_status"] != "missing":
                label += f" · {today['measured_temp']:g}°"
            else:
                label += " · Не сдано"
            rows.append([_btn(label, f"tk:room:{'s' if mode == 'send_room' else 'c'}:{room['id']}:{s['local_day']}")])
        navigation = []
        if page:
            navigation.append(_btn("Назад", f"tk:page:{'s' if mode == 'send_room' else 'c'}:{page - 1}:{s['local_day']}"))
        if len(rooms) > (page + 1) * PER_PAGE:
            navigation.append(_btn("Далее", f"tk:page:{'s' if mode == 'send_room' else 'c'}:{page + 1}:{s['local_day']}"))
        if navigation:
            rows.append(navigation)
    elif mode in ("send_value", "change_value"):
        sign = "s" if mode == "send_value" else "c"
        vals = [_btn(f"{value:g}°", f"tk:value:{sign}:{s['room_id']}:{s['local_day']}:{value:g}")
                for value in TEMPERATURES]
        for i in range(0, len(vals), 3):
            rows.append(vals[i:i + 3])
        rows.append([_btn("Ввести вручную", f"tk:manual:{sign}:{s['room_id']}:{s['local_day']}")])
        rows.append([_btn("Выбрать кабинет", "tk:send" if sign == "s" else "tk:change")])
    elif mode in ("send_manual", "change_manual", "bind"):
        rows.append([_btn("Отмена", "tk:home")])
    return rows


def keyboard(uid: str) -> list[dict]:
    return [{"type": "inline_keyboard", "payload": {"buttons": menu_buttons(uid)}}]


def _current(conn, room_id: int, start: str, end: str) -> dict | None:
    """The current room/day reading, regardless of reporter or source."""
    row = conn.execute("""
        SELECT * FROM readings
        WHERE room_id=? AND observed_at>=? AND observed_at<?
        ORDER BY observed_at DESC, id DESC LIMIT 1
    """, (room_id, start, end)).fetchone()
    return dict(row) if row else None

def _room(uid: str, room_id: int) -> dict | None:
    return next((room for room in db.bound_rooms(uid) if room["id"] == room_id), None)


def _select(uid: str, change: bool) -> str:
    rooms = db.bound_rooms(uid)
    if not rooms:
        _set(uid, "bind")
        return "Сначала привяжите кабинет. Нажмите «Кабинеты» или отправьте код подключения, полученный у администратора."
    if len(rooms) == 1:
        return _choose_room(uid, rooms[0]["id"], change)
    _set(uid, "change_room" if change else "send_room")
    return "Выберите кабинет для изменения показания за сегодня:" if change else "Выберите кабинет для сегодняшнего замера:"


def _choose_room(uid: str, room_id: int, change: bool) -> str:
    room = _room(uid, room_id)
    if not room:
        _set(uid)
        return "Этот кабинет вам не привязан или отключён. Выберите действие в меню."
    _, start, end = _today()
    with db.connect() as conn:
        previous = _current(conn, room_id, start, end)
    room_label = f"{room['building']} / {room['name']}"
    if previous is None:
        _set(uid, "send_value", room_id)
        if change:
            return (f"За сегодня ещё не передавали показание для {room_label}.\n"
                    "Выберите температуру для первого замера или введите её вручную.")
        return f"{room_label}\nВыберите температуру кнопкой или нажмите «Ввести вручную» (например, 21,5)."
    source_label = {"max": "сотрудником через MAX", "manual": "администратором",
                    "ha": "датчиком"}.get(previous["source"], "другим способом")
    if previous["source"] != "max":
        _set(uid)
        return (f"Сегодня для {room_label} уже передано {previous['value']:g} °C "
                f"({source_label}).\nЭто измерение нельзя изменять через MAX. "
                "При необходимости обратитесь к администратору.")
    _set(uid, "change_value", room_id)
    return (f"Сегодня для {room_label} уже передано {previous['value']:g} °C "
            f"({source_label}).\n"
            "Вы можете исправить общее показание кнопкой или нажать «Ввести вручную». "
            "В истории сохранятся первоначальный автор и автор исправления.")

def _write(uid: str, room_id: int, mode: str, raw: str, event_id: str) -> str:
    if not NUMBER.fullmatch(raw.strip()):
        return "Укажите температуру числом, например 21,5."
    value = float(raw.replace(",", "."))
    if not math.isfinite(value) or not -40 <= value <= 60:
        return "Температура должна быть в пределах −40…60 °C."
    room = _room(uid, room_id)
    if room is None:
        _set(uid)
        return "Нет доступа к этому кабинету. Выберите действие в меню."
    local_day, start, end = _today()
    now = db.utc_now()
    reporter = "MAX:" + str(uid)
    notify_others = (settings.get("alerts_enabled", "0") == "1"
                     and bool(settings.get("max_token", "")))
    message = None
    next_mode = ""
    with db.connect() as conn:
        # Serialize readers and writers for the whole room/day. Two employees
        # pressing «Send» concurrently must not create two MAX readings.
        conn.execute("BEGIN IMMEDIATE")
        bound = conn.execute("""
            SELECT 1 FROM max_bindings b JOIN rooms r ON r.id=b.room_id
            WHERE b.user_id=? AND b.room_id=? AND r.active=1
        """, (str(uid), room_id)).fetchone()
        if not bound:
            message = "Доступ к кабинету отозван."
        elif mode not in ("s", "c"):
            raise ValueError("Unknown MAX temperature action")
        elif mode == "s" and conn.execute(
            "SELECT 1 FROM readings WHERE external_id=?", (event_id,)
        ).fetchone():
            message = "Это измерение уже записано."
        elif mode == "c" and conn.execute(
            "SELECT 1 FROM max_reading_corrections WHERE event_id=?", (event_id,)
        ).fetchone():
            message = "Это исправление уже сохранено."
        else:
            previous = _current(conn, room_id, start, end)
            if mode == "s" and previous is not None:
                if previous["source"] == "max":
                    next_mode = "change_value"
                    message = (f"Сегодня для {room['building']} / {room['name']} уже передано "
                               f"{previous['value']:g} °C. Чтобы исправить общее показание, "
                               "выберите новое значение.")
                else:
                    message = (f"Сегодня для {room['building']} / {room['name']} уже есть "
                               f"показание {previous['value']:g} °C от датчика или администратора. "
                               "Изменить его через MAX нельзя.")
            elif mode == "c" and previous is None:
                next_mode = "send_value"
                message = "Показание за сегодня не найдено. Выберите температуру для первого замера."
            elif mode == "c" and previous["source"] != "max":
                message = "Текущее показание получено не через MAX. Через MAX изменить его нельзя: используйте соответствующий источник."
            elif mode == "c" and previous["value"] == value:
                message = f"Показание не изменилось: {room['name']} — {value:g} °C."
            elif mode == "c":
                conn.execute("""
                    INSERT INTO max_reading_corrections(
                        event_id, user_id, reading_id, old_value, new_value, old_observed_at, corrected_at
                    ) VALUES(?,?,?,?,?,?,?)
                """, (event_id, str(uid), previous["id"], previous["value"], value,
                      previous["observed_at"], now))
                conn.execute("UPDATE readings SET value=?, observed_at=? WHERE id=?",
                             (value, now, previous["id"]))
            else:
                conn.execute("""
                    INSERT INTO readings(room_id, value, source, reporter, observed_at, external_id)
                    VALUES (?, ?, 'max', ?, ?, ?)
                """, (room_id, value, reporter, now, event_id))
            if message is None and notify_others and (
                value < room["min_temp"] or value > room["max_temp"]
            ):
                # The author already sees an immediate MAX reply. Push only to
                # their colleagues, at most once per room/local-day/person.
                # Keep notices in the same transaction as the corrected value.
                label = "ниже нормы" if value < room["min_temp"] else "выше нормы"
                notice = (
                    f"Термоконтур: температура {label}!\n"
                    f"{room['building']} / {room['name']}: {value:g} °C\n"
                    f"Норма: {room['min_temp']:g}–{room['max_temp']:g} °C."
                )
                colleagues = conn.execute("""
                    SELECT user_id FROM max_bindings
                    WHERE room_id=? AND user_id<>?
                """, (room_id, str(uid))).fetchall()
                for colleague in colleagues:
                    recipient = colleague["user_id"]
                    token = f"max-room-deviation:{room_id}:{local_day}:{recipient}"
                    alert_id = hashlib.sha256(token.encode("utf-8")).hexdigest()
                    conn.execute("""
                        INSERT OR IGNORE INTO max_inbox(event_id,state,updated_at)
                        VALUES(?,'complete',?)
                    """, (alert_id, now))
                    conn.execute("""
                        INSERT OR IGNORE INTO max_outbox(event_id,user_id,text,next_retry_at)
                        VALUES(?,?,?,?)
                    """, (alert_id, recipient, notice, now))
                    conn.execute("""
                        INSERT OR IGNORE INTO max_outbox_ui(event_id,delivery_mode,notify)
                        VALUES(?,'notice',1)
                    """, (alert_id,))
    # Session changes must happen after the transaction to avoid SQLite locks.
    _set(uid, next_mode, room_id if next_mode else None)
    if message is not None:
        return message
    verb = "Исправлено" if mode == "c" else "Сохранено"
    result = f"{verb}: {room['building']} / {room['name']} — {value:g} °C."
    if value < room["min_temp"] or value > room["max_temp"]:
        result += (f"\nВнимание: температура вне установленной нормы "
                   f"({room['min_temp']:g}–{room['max_temp']:g} °C).")
    return (result + "\nПоказание общее для всех сотрудников кабинета. "
            "При исправлении сохраняется история изменений.")

def handle_callback(uid: str, payload: str, event_id: str) -> str:
    parts = payload.split(":")
    if not payload.startswith("tk:"):
        return "Кнопка не распознана. Используйте меню ниже."
    action = parts[1] if len(parts) > 1 else ""
    if action == "home":
        _set(uid)
        return "Главное меню. Выберите действие:"
    if action == "send":
        return _select(uid, False)
    if action == "change":
        return _select(uid, True)
    if action == "rooms":
        _set(uid)
        rooms = db.bound_rooms(uid)
        if not rooms:
            _set(uid, "bind")
            return "Пока нет привязанных кабинетов. Отправьте код подключения, полученный у администратора."
        return "Ваши кабинеты:\n" + "\n".join(
            f"• {r['building']} / {r['name']} (ID {r['id']})" for r in rooms[:25])
    if action == "today":
        _set(uid)
        # Keep existing /today semantics for all reading sources.
        from .bot import interpret
        return interpret(uid, "/today", event_id)
    if action == "bind":
        _set(uid, "bind")
        return "Отправьте код подключения, выданный администратором."
    if action in ("page", "room", "manual", "value"):
        if len(parts) < 5 or parts[2] not in ("s", "c"):
            return "Некорректная кнопка. Начните заново из меню."
        mode = parts[2]
        try:
            room_or_page = int(parts[3])
        except ValueError:
            return "Некорректная кнопка. Начните заново из меню."
        if parts[4] != _today()[0]:
            _set(uid)
            return "Эта кнопка относится к другому дню. Выберите действие заново."
        if action == "page":
            rooms = db.bound_rooms(uid)
            if room_or_page < 0 or room_or_page * PER_PAGE >= len(rooms):
                return "Страница не найдена."
            _set(uid, "change_room" if mode == "c" else "send_room", page=room_or_page)
            return "Выберите кабинет:"
        if action == "room":
            return _choose_room(uid, room_or_page, mode == "c")
        if not _room(uid, room_or_page):
            _set(uid)
            return "Нет доступа к этому кабинету."
        if action == "manual":
            _set(uid, "change_manual" if mode == "c" else "send_manual", room_or_page)
            return "Введите показание одним сообщением, например 21,5 (допустимо −40…60 °C)."
        if len(parts) != 6:
            return "Некорректное значение кнопки."
        return _write(uid, room_or_page, mode, parts[5], "max:callback:" + event_id)
    return "Кнопка больше не поддерживается. Выберите действие в меню."


def handle_message(uid: str, text: str, event_id: str) -> str | None:
    """Handle active button flows; return None to keep legacy slash commands."""
    clean = text.strip()
    if clean.lower() in ("меню", "главное меню"):
        _set(uid)
        return "Главное меню. Выберите действие:"
    s = _session(uid)
    if s["local_day"] != _today()[0]:
        _set(uid)
        s = _session(uid)
    if clean.startswith("/"):
        return None
    if s["mode"] == "bind" and CODE.fullmatch(clean):
        room = db.bind_code(uid, clean, event_id)
        if room:
            _set(uid)
            return f"Кабинет привязан: {room['building']} / {room['name']}. Теперь можно отправить показание за сегодня."
        return "Код недействителен или истёк. Запросите новый у администратора."
    if s["mode"] in ("send_manual", "change_manual"):
        if not NUMBER.fullmatch(clean):
            return "Введите температуру числом, например 21,5, либо нажмите «Отмена»."
        return _write(uid, s["room_id"], "s" if s["mode"] == "send_manual" else "c",
                      clean, "max:message:" + event_id)
    return None


def handle_start(uid: str, payload: str, event_id: str) -> str:
    _set(uid)
    if payload and CODE.fullmatch(payload):
        room = db.bind_code(uid, payload, event_id)
        if room is None:
            return "Код подключения не подошёл или истёк. Введите действующий код от администратора."
        return f"Кабинет привязан: {room['building']} / {room['name']}.\nТеперь выберите «Отправить за сегодня»."
    rooms = db.bound_rooms(uid)
    if rooms:
        return "Термоконтур — сбор температуры.\nВыберите действие кнопкой ниже. Ваших кабинетов: " + str(len(rooms)) + "."
    _set(uid, "bind")
    return "Термоконтур — сбор температуры.\nДля начала отправьте код подключения кабинета от администратора. После этого используйте кнопки ниже."
