"""Own MAX polling state and queued work by the verified bot identity."""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import secrets

from . import db


class Changed(RuntimeError):
    """The response belongs to a connection that is no longer configured."""


class WebhookActive(ValueError):
    """Switch to polling before replacing the token."""


def credential_scope(token: str) -> str:
    return "token:" + hashlib.sha256(token.encode()).hexdigest() if token else ""


@dataclass(frozen=True)
class Connection:
    token: str = field(repr=False)
    generation: str
    bot_id: str
    mode: str
    webhook_url: str
    webhook_secret: str = field(repr=False)

    @property
    def scope(self) -> str:
        return "bot:" + self.bot_id if self.bot_id else credential_scope(self.token)


def current(conn=None) -> Connection:
    if conn is None:
        with db.connect() as reader:
            return current(reader)
    values = dict(conn.execute("""
        SELECT key,value FROM settings
        WHERE key IN ('max_token','max_mode','max_webhook_url','max_webhook_secret')
        UNION ALL
        SELECT key,value FROM app_state WHERE key IN ('max_generation','max_bot_id')
    """))
    return Connection(values.get("max_token", ""), values.get("max_generation", ""),
                      values.get("max_bot_id", ""), values.get("max_mode", "polling"),
                      values.get("max_webhook_url", ""), values.get("max_webhook_secret", ""))


def require_current(conn, expected: Connection) -> Connection:
    active = current(conn)
    if (active.token != expected.token or active.generation != expected.generation
            or (expected.bot_id and active.bot_id != expected.bot_id)):
        raise Changed("MAX connection changed")
    return active


def _set_state(conn, key: str, value: str) -> None:
    conn.execute("""INSERT INTO app_state(key,value) VALUES(?,?)
                 ON CONFLICT(key) DO UPDATE SET value=excluded.value""", (key, value))


def renew_generation(conn) -> None:
    _set_state(conn, "max_generation", secrets.token_hex(16))


def replace_token(token: str | None) -> None:
    with db.connect() as conn:
        db.begin(conn, immediate=True)
        previous = current(conn)
        if previous.mode == "webhook":
            raise WebhookActive("Сначала отключите webhook MAX, чтобы изменить токен")
        if previous.token == (token or ""):
            return
        if token is None:
            conn.execute("DELETE FROM settings WHERE key='max_token'")
        else:
            conn.execute("""INSERT INTO settings(key,value) VALUES('max_token',?)
                         ON CONFLICT(key) DO UPDATE SET value=excluded.value""", (token,))
        renew_generation(conn)
        for key in ("max_bot_id", "max_marker", "max_poll_last_success_at", "max_poll_last_error"):
            _set_state(conn, key, "")
        conn.execute("DELETE FROM max_menu_messages")


def identify(expected: Connection, payload) -> Connection:
    bot_id = str(payload.get("user_id", "")) if isinstance(payload, dict) and payload.get("is_bot") is True else ""
    if not (expected.token and 1 <= len(bot_id) <= 20 and bot_id.isascii()
            and bot_id.isdecimal() and 0 < int(bot_id) < 2**64):
        raise ValueError("Invalid MAX bot identity")
    bot_id = str(int(bot_id))
    with db.connect() as conn:
        db.begin(conn, immediate=True)
        active = require_current(conn, expected)
        if active.bot_id:
            if active.bot_id != bot_id:
                raise ValueError("MAX bot identity changed")
            return active
        scope = "bot:" + bot_id
        # Legacy and queued pre-verification work stays with the token that
        # created it until MAX confirms which bot owns it.
        conn.execute("UPDATE max_inbox SET bot_scope=? WHERE bot_scope=?", (scope, active.scope))
        conn.execute("""INSERT OR IGNORE INTO app_state(key,value)
                     SELECT ?,value FROM app_state WHERE key=?""",
                     ("max_marker:" + scope, "max_marker:" + active.scope))
        conn.execute("DELETE FROM app_state WHERE key=?", ("max_marker:" + active.scope,))
        _set_state(conn, "max_bot_id", bot_id)
        return current(conn)


def marker(expected: Connection) -> str | None:
    return db.get_state("max_marker:" + expected.scope)


def poll_result(expected: Connection, *, cursor=None, error="") -> None:
    with db.connect() as conn:
        db.begin(conn, immediate=True)
        require_current(conn, expected)
        if cursor is not None:
            _set_state(conn, "max_marker:" + expected.scope, str(cursor))
        if not error:
            _set_state(conn, "max_poll_last_success_at", db.utc_now())
        _set_state(conn, "max_poll_last_error", error)
