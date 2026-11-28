"""Drain in-flight writers before replacing the live database."""
from contextlib import ExitStack, contextmanager
import fcntl
import json
import os
from time import monotonic, sleep

from . import db

RESTORE_PATH = "/api/backups/restore"
RETRY_HEADERS = {"Retry-After": "5"}
BUSY_MESSAGE = "Идёт обслуживание базы. Повторите запрос через несколько секунд."


class Busy(RuntimeError):
    """A restore or update currently owns the write gate."""


def _path(suffix):
    database = db.db_path()
    return database.with_name(f"{database.name}.{suffix}")


def _update_paused() -> bool:
    if _path("update-paused").exists():
        return True
    # The first upgrade may still be running an older installed updater.
    # Its switching status is written before starting the new processes.
    status = db.db_path().parent / "update-status.json"
    try:
        with status.open("rb") as stream:
            raw = stream.read(8193)
        record = json.loads(raw) if len(raw) <= 8192 else None
    except (OSError, ValueError):
        return False
    return (isinstance(record, dict) and record.get("state") == "installing"
            and record.get("phase") in ("switching", "verifying", "finishing"))


@contextmanager
def _locked(suffix, mode, timeout=0):
    path = _path(suffix)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDONLY | os.O_NOFOLLOW, 0o600)
    try:
        deadline = monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, mode | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if monotonic() >= deadline:
                    raise Busy(BUSY_MESSAGE) from None
                sleep(0.05)
        yield
    finally:
        os.close(fd)


@contextmanager
def activity():
    """Atomically admit a request or worker and track it until it finishes."""
    with ExitStack() as running:
        with _locked("admission.lock", fcntl.LOCK_SH):
            if _update_paused():
                raise Busy(BUSY_MESSAGE)
            running.enter_context(_locked("writers.lock", fcntl.LOCK_SH))
        yield


@contextmanager
def startup():
    """Serialize initialization with restores, including on a paused update."""
    with _locked("admission.lock", fcntl.LOCK_SH, timeout=60):
        yield


@contextmanager
def exclusive():
    """Stop admitting writers before waiting for the active operations."""
    with _locked("admission.lock", fcntl.LOCK_EX):
        if _update_paused():
            raise Busy(BUSY_MESSAGE)
        with _locked("writers.lock", fcntl.LOCK_EX, timeout=60):
            yield


def run(task):
    with activity():
        return task()


def pause_for_update():
    # The native updater restarts both processes with this marker in place.
    with exclusive():
        _path("update-paused").touch(mode=0o600, exist_ok=False)
