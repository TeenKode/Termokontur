"""Minimal administrative action history; never retain request bodies or secrets."""
from . import db


def record(method: str, route: str, target: str, status: int) -> None:
    with db.connect() as conn:
        conn.execute("INSERT INTO admin_audit(recorded_at,method,route,target,status) VALUES(?,?,?,?,?)",
                     (db.utc_now(), method, route[:200], target[:200], status))
        # Keep the most recent 20,000 events. This is an operational history,
        # not a tamper-proof or independent compliance audit.
        conn.execute("DELETE FROM admin_audit WHERE id <= (SELECT MAX(id)-20000 FROM admin_audit)")


def recent(limit: int = 100) -> list[dict]:
    with db.connect() as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM admin_audit ORDER BY id DESC LIMIT ?", (limit,))]
