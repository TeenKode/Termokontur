"""Administrator account stored in SQLite; PBKDF2 and session invalidation."""
from __future__ import annotations

import hashlib
import hmac
import os
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
        if len(initial) < 12 or initial.startswith("REPLACE_WITH_"):
            raise RuntimeError("Set a unique TERMOKONTUR_ADMIN_PASSWORD (12+ chars) for first setup")
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
    if len(new) < 12 or len(new) > 256:
        raise ValueError("Новый пароль: от 12 до 256 символов")
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
