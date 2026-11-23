"""Administrator account stored in SQLite; PBKDF2 and session invalidation."""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets

from . import db

ITERATIONS = 310_000

def _digest(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, ITERATIONS
    ).hex()

def bootstrap() -> None:
    """Import existing bootstrap password once. Do not re-enable it after changes."""
    with db.connect() as conn:
        if conn.execute("SELECT 1 FROM admin_account WHERE id=1").fetchone():
            return
        initial = os.environ.get("TERMOKONTUR_ADMIN_PASSWORD", "")
        if len(initial) < 8 or initial.startswith("REPLACE_WITH_"):
            raise RuntimeError("Set a unique TERMOKONTUR_ADMIN_PASSWORD (8+ chars) for first setup")
        salt = secrets.token_bytes(24)
        conn.execute(
            """INSERT INTO admin_account(id, salt, password_hash, session_epoch)
               VALUES (1, ?, ?, 1)""",
            (salt.hex(), _digest(initial, salt)),
        )

def check_password(password: str) -> bool:
    with db.connect() as conn:
        row = conn.execute("SELECT salt,password_hash FROM admin_account WHERE id=1").fetchone()
    if not row:
        return False
    actual = _digest(password, bytes.fromhex(row["salt"]))
    return hmac.compare_digest(actual, row["password_hash"])

def session_epoch() -> int:
    with db.connect() as conn:
        row = conn.execute("SELECT session_epoch FROM admin_account WHERE id=1").fetchone()
    return row["session_epoch"] if row else 0

def change_password(old: str, new: str) -> bool:
    if len(new) < 8 or len(new) > 256:
        raise ValueError("Новый пароль: от 8 до 256 символов")
    if new == old:
        raise ValueError("Укажите другой пароль")
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT salt,password_hash FROM admin_account WHERE id=1").fetchone()
        if not row or not hmac.compare_digest(
            _digest(old, bytes.fromhex(row["salt"])), row["password_hash"]
        ):
            return False
        salt = secrets.token_bytes(24)
        conn.execute(
            """UPDATE admin_account SET salt=?,password_hash=?,
               session_epoch=session_epoch+1 WHERE id=1""",
            (salt.hex(), _digest(new, salt)),
        )
    return True


def building_account(username: str) -> dict | None:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM building_accounts WHERE username=?", (username,)).fetchone()
        return dict(row) if row else None


def check_building_password(username: str, password: str) -> dict | None:
    account = building_account(username)
    salt = bytes.fromhex(account["salt"]) if account else bytes(24)
    actual = _digest(password, salt)
    if account and hmac.compare_digest(actual, account["password_hash"]):
        return account
    return None


def list_building_accounts() -> list[dict]:
    with db.connect() as conn:
        return [dict(row) for row in conn.execute("""
            SELECT username,building,max_user_id FROM building_accounts ORDER BY building,username
        """)]


def save_building_account(username: str, building: str, password: str | None, max_user_id: str | None) -> dict:
    if not re.fullmatch(r"[a-z][a-z0-9_.-]{2,31}", username):
        raise ValueError("Логин должен содержать от 3 до 32 латинских букв, цифр, точек, дефисов или подчёркиваний")
    if password is not None and not 8 <= len(password) <= 256:
        raise ValueError("Пароль должен содержать от 8 до 256 символов")
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if not conn.execute("SELECT 1 FROM rooms WHERE building=?", (building,)).fetchone():
            raise ValueError("Выберите существующий корпус")
        if max_user_id and not conn.execute("SELECT 1 FROM max_users WHERE user_id=?", (max_user_id,)).fetchone():
            raise ValueError("Пользователь должен сначала открыть бота MAX")
        current = conn.execute("SELECT salt,password_hash FROM building_accounts WHERE username=?", (username,)).fetchone()
        if password is None:
            if not current:
                raise ValueError("Укажите пароль новой учётной записи")
            salt, digest = current["salt"], current["password_hash"]
        else:
            new_salt = secrets.token_bytes(24)
            salt, digest = new_salt.hex(), _digest(password, new_salt)
        conn.execute("""
            INSERT INTO building_accounts(username,building,salt,password_hash,session_key,max_user_id)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(username) DO UPDATE SET building=excluded.building,salt=excluded.salt,
                password_hash=excluded.password_hash,session_key=excluded.session_key,max_user_id=excluded.max_user_id
        """, (username, building, salt, digest, secrets.token_hex(16), max_user_id))
    return {"username": username, "building": building, "max_user_id": max_user_id}


def remove_building_account(username: str) -> bool:
    with db.connect() as conn:
        return bool(conn.execute("DELETE FROM building_accounts WHERE username=?", (username,)).rowcount)
