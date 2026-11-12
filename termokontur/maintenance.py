"""SQLite online backups, validation, restore and retention maintenance."""
from __future__ import annotations

import os
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import db, settings

NAME_PATTERN = re.compile(r"^termokontur-(?:manual|auto|import|pre-restore)-[0-9TZ]{16}-[0-9a-f]{12}\.sqlite3$")
REQUIRED_TABLES = {"rooms", "readings", "settings", "admin_account", "max_bindings"}
MAX_IMPORT_BYTES = 100 * 1024 * 1024

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
    if not NAME_PATTERN.fullmatch(name):
        raise ValueError("Недопустимое имя резервной копии")
    root = backup_dir().resolve()
    target = root / name
    if target.is_symlink() or not target.is_file() or target.resolve().parent != root:
        raise ValueError("Резервная копия не найдена")
    return target

def inspect(path: Path) -> None:
    if path.stat().st_size > MAX_IMPORT_BYTES or path.stat().st_size < 4096:
        raise ValueError("Размер копии не соответствует ограничениям")
    uri = f"{path.resolve().as_uri()}?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True, timeout=5) as conn:
            conn.execute("PRAGMA trusted_schema=OFF")
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

def create_backup(kind: str = "manual") -> dict:
    filename = _filename(kind)
    dest = backup_dir() / filename
    source = sqlite3.connect(str(db.db_path()), timeout=30)
    target = sqlite3.connect(str(dest), timeout=30)
    try:
        with target:
            source.backup(target, pages=128, sleep=0.1)
    except Exception:
        target.close()
        dest.unlink(missing_ok=True)
        raise
    finally:
        source.close()
        target.close()
    dest.chmod(0o600)
    inspect(dest)
    return {"name": filename, "size": dest.stat().st_size,
            "created_at": datetime.fromtimestamp(dest.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")}

def list_backups() -> list[dict]:
    result = []
    for file in backup_dir().iterdir():
        if not NAME_PATTERN.fullmatch(file.name) or file.is_symlink() or not file.is_file():
            continue
        stat=file.stat()
        result.append({"name":file.name,"size":stat.st_size,
                       "created_at":datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(timespec="seconds")})
    return sorted(result,key=lambda row:row["created_at"],reverse=True)

def restore(name: str) -> dict:
    source_path = backup_path(name)
    inspect(source_path)
    previous_epoch = 0
    with db.connect() as conn:
        row=conn.execute("SELECT session_epoch FROM admin_account WHERE id=1").fetchone()
        previous_epoch=int(row["session_epoch"] if row else 0)
    safeguard=create_backup("pre-restore")
    try:
        with sqlite3.connect(str(source_path), timeout=30) as source:
            with sqlite3.connect(str(db.db_path()), timeout=30) as destination:
                source.backup(destination, pages=128, sleep=0.1)
        db.initialize()
        with db.connect() as conn:
            current=conn.execute("SELECT session_epoch FROM admin_account WHERE id=1").fetchone()
            new_epoch=max(previous_epoch,int(current[0]))+1
            conn.execute("UPDATE admin_account SET session_epoch=? WHERE id=1", (new_epoch,))
        return {"restored": name, "safety_backup": safeguard["name"], "relogin_required": True}
    except Exception:
        with sqlite3.connect(str(backup_path(safeguard["name"])), timeout=30) as source:
            with sqlite3.connect(str(db.db_path()), timeout=30) as destination:
                source.backup(destination)
        raise

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
    backups=list_backups()
    auto=[b for b in backups if "-auto-" in b["name"]]
    now=datetime.now(timezone.utc)
    if days and (not auto or
                 now - datetime.fromisoformat(auto[0]["created_at"]) >= timedelta(days=days)):
        made=create_backup("auto")
    backups=list_backups()
    # Keep user-created copies; only rotate automatic backups.
    for old in [b for b in backups if "-auto-" in b["name"]][keep:]:
        delete_backup(old["name"])
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
    return {"created":made["name"] if made else None, "automatic_backups":len(auto)}
