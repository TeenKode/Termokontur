"""SQLite online backups, validation, restore and retention maintenance."""
from __future__ import annotations

import logging
import re
import secrets
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory
from threading import Lock

from . import db, migrations, settings

UPDATE_NAME_PATTERN = re.compile(r"^pre-update-[0-9TZ]{16}-[a-f0-9]{40}\.sqlite3$")
NAME_PATTERN = re.compile(r"^termokontur-(?:manual|auto|import|pre-restore)-[0-9TZ]{16}-[0-9a-f]{12}\.sqlite3$")
REQUIRED_TABLES = {"rooms", "readings", "settings", "admin_account", "max_bindings"}
MAX_IMPORT_BYTES = 1024**3
_restore_lock = Lock()
logger = logging.getLogger(__name__)

def backup_dir() -> Path:
    folder = db.db_path().parent / "backups"
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    folder.chmod(0o700)
    return folder

def _filename(kind: str) -> str:
    if kind not in ("manual", "auto", "import", "pre-restore"):
        raise ValueError("Недопустимый тип резервной копии")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"termokontur-{kind}-{stamp}-{secrets.token_hex(6)}.sqlite3"

def backup_path(name: str) -> Path:
    if not (NAME_PATTERN.fullmatch(name) or UPDATE_NAME_PATTERN.fullmatch(name)):
        raise ValueError("Недопустимое имя резервной копии")
    root = backup_dir().resolve()
    target = root / name
    if target.is_symlink() or not target.is_file() or target.resolve().parent != root:
        raise ValueError("Резервная копия не найдена")
    return target

def inspect(path: Path) -> None:
    if path.stat().st_size < 4096:
        raise ValueError("Файл слишком мал для резервной копии SQLite")
    uri = f"{path.resolve().as_uri()}?mode=ro"
    try:
        with closing(sqlite3.connect(uri, uri=True, timeout=5)) as conn:
            conn.execute("PRAGMA trusted_schema=OFF")
            migrations.read_version(conn)
            if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("Резервная копия повреждена")
            schema = conn.execute("SELECT name, type FROM sqlite_master").fetchall()
            tables = {name for name, kind in schema if kind == "table"}
            if not REQUIRED_TABLES.issubset(tables):
                raise ValueError("Файл не является резервной копией Термоконтура")
            if any(kind in ("trigger", "view") for _, kind in schema):
                raise ValueError("Файл содержит неподдерживаемые объекты SQLite")
            if not conn.execute("SELECT 1 FROM admin_account WHERE id=1").fetchone():
                raise ValueError("Резервная копия не содержит учётную запись администратора")
    except sqlite3.DatabaseError as exc:
        raise ValueError("Невозможно прочитать SQLite-файл") from exc

def _copy_database(source: Path, dest: Path) -> None:
    uri = source.resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=30)) as reader:
        with closing(sqlite3.connect(dest, timeout=30)) as writer:
            reader.backup(writer, pages=128, sleep=0.1)


def create_backup(kind: str = "manual") -> dict:
    dest = backup_dir() / _filename(kind)
    with NamedTemporaryFile(dir=dest.parent, prefix=".backup-", delete=False) as temp:
        staged = Path(temp.name)
    try:
        _copy_database(db.db_path(), staged)
        inspect(staged)
        staged.replace(dest)
    finally:
        staged.unlink(missing_ok=True)
    return {"name": dest.name, "size": dest.stat().st_size,
            "created_at": datetime.fromtimestamp(dest.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")}

def list_backups() -> list[dict]:
    result = []
    for file in backup_dir().iterdir():
        if not (NAME_PATTERN.fullmatch(file.name) or UPDATE_NAME_PATTERN.fullmatch(file.name)) or file.is_symlink() or not file.is_file():
            continue
        stat=file.stat()
        result.append({"name":file.name,"size":stat.st_size,
                       "created_at":datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(timespec="seconds")})
    return sorted(result,key=lambda row:row["created_at"],reverse=True)

def restore(name: str) -> dict:
    source_path = backup_path(name)
    inspect(source_path)
    with _restore_lock, TemporaryDirectory(dir=source_path.parent, prefix=".restore-") as workspace:
        staged = Path(workspace) / "restore.sqlite3"
        _copy_database(source_path, staged)
        db.initialize(staged)
        with db.connect() as live, db.connect(staged) as target:
            previous = live.execute("SELECT session_epoch FROM admin_account WHERE id=1").fetchone()[0]
            target.execute("UPDATE admin_account SET session_epoch=MAX(session_epoch,?)+1 WHERE id=1", (previous,))
            target.execute("UPDATE sensor_devices SET restore_pending=1 WHERE token_hash IS NOT NULL")
        safeguard = create_backup("pre-restore")
        # SQLite applies the validated, migrated image in a single write.
        _copy_database(staged, db.db_path())
    return {"restored": name, "safety_backup": safeguard["name"], "relogin_required": True}

def delete_backup(name: str) -> bool:
    backup_path(name).unlink()
    return True

def prune_data(retention_days: int) -> dict:
    if retention_days == 0:
        return {"readings": 0, "receipts": 0}
    if not 30 <= retention_days <= 3650:
        raise ValueError("Недопустимый срок хранения")
    cutoff=(datetime.now(timezone.utc)-timedelta(days=retention_days)).isoformat(timespec="seconds")
    with db.connect() as conn:
        readings=conn.execute("DELETE FROM readings WHERE observed_at<?", (cutoff,)).rowcount
    # Replays must remain idempotent at least while the corresponding
    # message receipts are retained for 30 days.
    from . import max_delivery
    max_delivery.prune(min(retention_days,30))
    return {"readings":readings}

def schedule() -> dict:
    days=int(settings.get("backup_interval_days", "1"))
    keep=int(settings.get("backup_keep", "30"))
    retention=int(settings.get("retention_days", "0"))
    if not (0<=days<=30 and 1<=keep<=100 and (retention==0 or 30<=retention<=3650)):
        raise ValueError("Неверные настройки обслуживания")
    made=None
    auto = []
    backup_error = False
    now=datetime.now(timezone.utc)
    try:
        auto = [item for item in list_backups() if "-auto-" in item["name"]]
        if days and (not auto or now - datetime.fromisoformat(auto[0]["created_at"]) >= timedelta(days=days)):
            made = create_backup("auto")
            auto.insert(0, made)
        for old in auto[keep:]:
            delete_backup(old["name"])
        auto = auto[:keep]
        updates = [item for item in list_backups() if UPDATE_NAME_PATTERN.fullmatch(item["name"])]
        for old in updates[keep:]:
            delete_backup(old["name"])
    except (OSError, ValueError, sqlite3.Error):
        backup_error = True
        logger.exception("Scheduled backup failed")
    if retention:
        prune_data(retention)
    else:
        from . import max_delivery
        max_delivery.prune(30)
    with db.connect() as conn:
        conn.execute("DELETE FROM pairing_codes WHERE expires_at<?", (db.utc_now(),))
        cutoff=(now-timedelta(days=45)).isoformat(timespec="seconds")
        conn.execute("DELETE FROM max_bind_events WHERE recorded_at<?", (cutoff,))
        conn.execute("DELETE FROM reminder_deliveries WHERE updated_at<?", (cutoff,))
    # Scheduled digests use the durable MAX outbox and do not send HTTP here.
    # A failure must not interrupt backups, retention or maintenance.
    from . import report_schedule
    try:
        dispatched = report_schedule.queue_due()
    except (ValueError, sqlite3.Error) as exc:
        logger.warning("Report scheduling failed: %s", type(exc).__name__)
        dispatched = {"queued": 0}
    from . import sensor_monitor
    try:
        alerts = sensor_monitor.queue_alerts()
    except (ValueError, sqlite3.Error):
        logger.exception("Sensor health scheduling failed")
        alerts = 0
    return {"created": made["name"] if made else None,
            "automatic_backups": len(auto), "backup_error": backup_error,
            "report_digests_queued": dispatched["queued"], "sensor_alerts_queued": alerts}
