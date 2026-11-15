"""Capture guide screenshots from an isolated demo database and firmware HTML.

Requires requirements-dev.txt, Playwright and Chromium. No physical sensor,
production database, credentials or USB connection is used.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time

import httpx
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs/sensors/images"
OUT.mkdir(exist_ok=True)


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def capture(page, name, locator=None):
    page.evaluate("document.fonts.ready")
    target = locator or page
    target.screenshot(path=str(OUT / (name + ".jpg")), type="jpeg", quality=78,
                      animations="disabled")
    print("Captured " + name, flush=True)


def checked(response):
    response.raise_for_status()
    return response.json()


with tempfile.TemporaryDirectory(prefix="sensor-guide-") as tmp:
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    env = {**os.environ, "TERMOKONTUR_DB": str(Path(tmp) / "demo.sqlite3"),
           "TERMOKONTUR_SECRET": "isolated-guide-demo-secret-at-least-32-characters",
           "TERMOKONTUR_ADMIN_PASSWORD": "guide-demo-password"}
    with open(Path(tmp) / "server.log", "w+") as log:
        proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "termokontur.app:app",
                                 "--host", "127.0.0.1", "--port", str(port)],
                                cwd=ROOT, env=env, stdout=log, stderr=log)
        try:
            for _ in range(100):
                try:
                    if httpx.get(base + "/health", trust_env=False).is_success:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(.1)
            else:
                log.seek(0)
                raise RuntimeError(log.read())
            with httpx.Client(base_url=base, trust_env=False) as api, sync_playwright() as pw:
                checked(api.post("/api/login", json={"password": "guide-demo-password"}))
                room = checked(api.post("/api/rooms", json={"building": "Демо: школа",
                                                           "name": "Кабинет 101"}))
                browser = pw.chromium.launch(
                    executable_path=os.getenv("CHROMIUM_PATH") or shutil.which("chromium"),
                    headless=True, args=["--no-sandbox"])
                page = browser.new_page(viewport={"width": 1280, "height": 960},
                                        device_scale_factor=1, locale="ru-RU")
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(base)
                page.fill("#password", "guide-demo-password")
                page.click('#login-form button[type="submit"]')
                page.wait_for_selector("#app-root", state="visible")
                page.evaluate("window.termokonturTheme.setPreference('light')")
                page.click('[data-tab="sensors"]')
                expect(page.locator("#sensor-state")).to_contain_text("Добавьте")
                capture(page, "01-sensors")
                page.click("#sensor-add")
                page.fill("#sensor-new-name", "Демо: датчик кабинета 101")
                capture(page, "02-create", page.locator("#sensor-add-dialog"))
                with page.expect_response(lambda r: r.url.endswith("/api/sensors/devices")
                                          and r.request.method == "POST") as response:
                    page.click('#sensor-add-form button:not([type="button"])')
                created = response.value.json()
                expect(page.locator("#sensor-code-dialog")).to_be_visible()
                # Mask the isolated demo's one-time code even though its DB is discarded.
                page.locator("#sensor-code-value").evaluate("e => e.value = 'ДЕМО — используйте свой код из панели'")
                capture(page, "03-code", page.locator("#sensor-code-dialog"))
                page.click("#sensor-code-close")
                token = checked(api.post("/api/sensors/enroll", json={
                    "device_id": created["device_id"], "code": created["code"],
                    "model": "c3-sht40-v1", "sensor_type": "sht4x",
                    "hardware_id": "000000000001"}))["token"]
                records = [{"seq": i, "boot_id": "0000000000000001", "uptime_s": i*1800,
                            "measured_at": int(time.time())-86400+i*1800,
                            "time_quality": "rtc", "temperature": 21.5 + (i % 6)*.1,
                            "humidity": 45.0 + (i % 6)*.2, "battery_mv": 3950,
                            "interval_s": 1800, "error": ""} for i in range(1, 49)]
                payload = {"model": "c3-sht40-v1", "sensor_type": "sht4x",
                           "firmware": "0.1.2", "records": records, "next_seq": 49,
                           "lost": 0, "rssi": -57, "battery_mv": 3950,
                           "config_version": 1, "boots": 48}
                checked(api.post(f"/api/sensors/devices/{created['device_id']}/sync",
                                 headers={"Authorization": "Bearer " + token}, json=payload))
                page.click("#sensor-refresh")
                page.locator(".sensor-card button").first.click()
                expect(page.locator("#sensor-title")).to_contain_text("101")
                expect(page.locator("#sensor-records tr")).to_have_count(48)
                # Capture the top of the real detail, including the graph and first rows.
                page.set_viewport_size({"width": 1280, "height": 1260})
                page.locator("#sensor-title").evaluate("e => e.scrollIntoView({block:'start'})")
                capture(page, "05-readings")
                page.set_viewport_size({"width": 1280, "height": 960})
                config = page.locator("#sensor-detail details").nth(0)
                config.locator("summary").click()
                page.fill('#sensor-config-form input[name="measure_s"]', "60")
                page.fill('#sensor-config-form input[name="send_s"]', "600")
                page.fill('#sensor-config-form input[name="cool_s"]', "300")
                capture(page, "06-config", config)
                # Separate simulated existing board to demonstrate a real OTA assignment.
                ota = checked(api.post("/api/sensors/devices", json={
                    "name": "Демо: обновление датчика", "room_id": room["id"]}))
                ota_token = checked(api.post("/api/sensors/enroll", json={
                    "device_id": ota["device_id"], "code": ota["code"],
                    "model": "c3-sht40-v1", "hardware_id": "000000000002"}))["token"]
                checked(api.post(f"/api/sensors/devices/{ota['device_id']}/sync",
                                 headers={"Authorization": "Bearer " + ota_token},
                                 json={**payload, "firmware": "0.1.1", "records": [],
                                       "next_seq": 1, "boots": 1}))
                page.click("#sensor-refresh")
                page.locator(".sensor-card").filter(has_text="Демо: обновление").locator("button").click()
                expect(page.locator("#sensor-title")).to_contain_text("обновление")
                update = page.locator("#sensor-detail details").nth(1)
                update.locator("summary").click()
                image = ROOT / "firmware/sensor/.pio/build/esp32-c3-supermini/firmware.bin"
                page.set_input_files("#sensor-image", str(image))
                with page.expect_response(lambda r: r.url.endswith("/api/sensors/firmware")
                                          and r.request.method == "POST") as upload:
                    page.locator("#sensor-upload-form button").click()
                assert upload.value.status == 200
                fw = checked(api.get("/api/sensors/firmware"))[0]
                expect(page.locator("#sensor-firmware option")).to_have_count(2)
                page.select_option("#sensor-firmware", str(fw["id"]))
                with page.expect_response(lambda r: "/ota?firmware_id=" in r.url):
                    page.locator("#sensor-ota-form button").first.click()
                expect(page.locator("#sensor-firmware")).to_have_value(str(fw["id"]))
                capture(page, "07-ota", update)
                page.locator("#sensor-rekey").scroll_into_view_if_needed()
                capture(page, "08-rekey", page.locator("#sensor-device-form"))
                # Render the exact HTML embedded in firmware. No ESP or Wi-Fi is involved.
                source = (ROOT / "firmware/sensor/src/main.cpp").read_text()
                html = re.search(r'R"HTML\((.*?)\)HTML"', source, re.S).group(1)
                setup = browser.new_page(viewport={"width": 440, "height": 940},
                                         device_scale_factor=1, locale="ru-RU")
                setup.set_content(html.replace("NONCE", "DEMO-NONCE"))
                setup.locator("h1").evaluate("e => e.insertAdjacentHTML('afterend',"
                    "'<p style=\"color:#ffd98e\">Демонстрация страницы прошивки. Плата не подключена.</p>')")
                setup.select_option('[name="sensor_type"]', "sht4x")
                for name, value in {"ssid": "School-IoT-2.4G", "password": "DEMO-ONLY",
                                    "server": "https://monitor.school.example",
                                    "device": created["device_id"],
                                    "code": "ДЕМО — вставьте свой код из панели"}.items():
                    setup.fill(f'[name="{name}"]', value)
                setup.screenshot(path=str(OUT / "04-phone-setup.jpg"), full_page=True,
                                 type="jpeg", quality=78)
                assert not errors, errors
                browser.close()
            print(f"Saved 8 screenshots to {OUT}; isolated demo DB removed on exit.")
        finally:
            proc.terminate()
            proc.wait(timeout=10)
