"""Pairing is atomic under concurrent requests; updater regression checks."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
from termokontur import db


def test_code_is_consumed_by_at_most_one_user(client):
    room=db.create_room("Корпус","2","201","Кабинет",18,24)
    code=db.issue_code(room["id"])
    barrier=threading.Barrier(2)
    def try_bind(user):
        barrier.wait(timeout=5)
        return db.bind_code(user,code)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first=executor.submit(try_bind,"user-a")
        second=executor.submit(try_bind,"user-b")
        outcomes=[first.result(),second.result()]
    assert sum(result is not None for result in outcomes)==1
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM max_bindings").fetchone()[0]==1


def test_updater_is_not_broken_by_literal_escaped_newline():
    path=Path(__file__).resolve().parents[1]/"scripts"/"termokontur-apply-update"
    contents=path.read_text(encoding="utf-8")
    assert "\\nchmod" not in contents
    assert 'REVISION=$(runuser -u termokontur -- git -C "$STAGE" rev-parse HEAD)' in contents
    assert "systemctl daemon-reload" in contents
    assert "pre-update-" in contents
    assert 'install -o root -g root -m 0755 "$APP/scripts/termokontur-apply-update"' in contents
    unit=Path(__file__).resolve().parents[1]/"deploy"/"systemd"/"termokontur-update.service"
    assert "/etc/systemd/system" in unit.read_text(encoding="utf-8")
