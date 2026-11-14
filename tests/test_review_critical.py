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
    for phase in ("preparing", "downloading", "dependencies", "backup",
                  "switching", "verifying", "finishing", "success", "failed"):
        assert phase in contents
    assert 'check_writable_dir "$UNITS_DIR"' in contents
    assert 'check_writable_dir "/usr/local/sbin"' in contents
    assert contents.index('check_writable_dir "$UNITS_DIR"') < contents.rindex('systemctl stop termokontur.service')
    assert 'if ! install -o root -g root -m 0644 "$OLD_UNITS/$unit"' in contents
    assert 'install -o root -g root -m 0755 "$APP/scripts/termokontur-apply-update"' in contents
    unit=Path(__file__).resolve().parents[1]/"deploy"/"systemd"/"termokontur-update.service"
    unit_content = unit.read_text(encoding="utf-8")
    assert "ProtectSystem=full" in unit_content
    assert "ReadWritePaths=/opt /var/lib/termokontur /etc/systemd/system /usr/local/sbin" in unit_content


def test_updater_ui_contains_accessible_state_and_version_fields():
    static = Path(__file__).resolve().parents[1] / "termokontur" / "static"
    html = (static / "index.html").read_text(encoding="utf-8")
    js = (static / "app.js").read_text(encoding="utf-8")
    for element_id in (
        "update-current-version", "update-current-build",
        "update-latest-version", "update-latest-build",
        "update-status-text", "update-progress", "update-step-label",
        "update-step-count", "update-progress-fill", "update-state",
        "check-update", "install-update", "reload-after-update",
    ):
        assert f'id="{element_id}"' in html
        assert f'"{element_id}"' in js
    assert 'aria-live="polite"' in html
    assert 'role="progressbar"' in html
    assert 'setInterval(()=>{' in js
    assert 'updateAvailable=false' in js


def test_settings_workspace_has_unique_ids_and_working_category_targets():
    """Prevent empty panels and missing controls after reorganizing settings."""
    from html.parser import HTMLParser
    import re

    class Scan(HTMLParser):
        def __init__(self):
            super().__init__()
            self.ids = []
            self.targets = []
            self.views = []
        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if "id" in attrs:
                self.ids.append(attrs["id"])
            if "data-settings-target" in attrs:
                self.targets.append(attrs["data-settings-target"])
            if "data-settings-view" in attrs:
                self.views.append(attrs["data-settings-view"])

    static = Path(__file__).resolve().parents[1] / "termokontur" / "static"
    html = (static / "index.html").read_text(encoding="utf-8")
    js = (static / "app.js").read_text(encoding="utf-8")
    page = Scan()
    page.feed(html)
    assert len(page.ids) == len(set(page.ids))
    assert set(page.targets) == set(page.views) == {
        "summary", "general", "notifications", "max",
        "sensors", "backups", "security", "updates"
    }
    assert len(page.views) == 8
    assert not (set(re.findall(r'\$\("([a-zA-Z0-9-]+)"\)', js)) - set(page.ids))
    for form in (
        "general-settings-form", "reminders-settings-form",
        "alerts-settings-form", "maintenance-form",
        "max-settings-form", "password-settings-form"
    ):
        assert form in page.ids
    assert "saveSettingsPatch" in js
    assert "settingsDirty" in js
    assert "data-dirty" in js
    assert "renderSettingsSummary" in js
    assert "updateAvailable=false" in js
    assert "X-Api-Key" in html


def test_max_polling_services_use_debian_ca_bundle():
    """The Python httpx default bundle must not mask locally trusted MAX CAs."""
    root = Path(__file__).resolve().parents[1]
    for name in ("termokontur.service", "termokontur-max.service"):
        content = (root / "deploy" / "systemd" / name).read_text(encoding="utf-8")
        assert "Environment=SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt" in content
        assert "verify=False" not in content
    html = (root / "termokontur" / "static" / "index.html").read_text(encoding="utf-8")
    js = (root / "termokontur" / "static" / "app.js").read_text(encoding="utf-8")
    assert 'id="switch-polling"' in html
    assert 'id="max-webhook-form"' not in html
    assert 'id="activate-webhook"' not in html
    assert 'max_poll_last_error' in (root / "termokontur" / "app.py").read_text(encoding="utf-8")
    assert "tls_certificate" in js
