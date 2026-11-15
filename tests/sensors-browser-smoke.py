"""Run against an isolated temporary server; requires Playwright + Chromium."""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time

import httpx
from playwright.sync_api import expect, sync_playwright

ROOT=Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory(prefix="termokontur-ui-") as tmp:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1",0));port=sock.getsockname()[1]
    env={**os.environ,"TERMOKONTUR_DB":str(Path(tmp)/"smoke.sqlite3"),
         "TERMOKONTUR_SECRET":"browser-smoke-secret-at-least-32-characters",
         "TERMOKONTUR_ADMIN_PASSWORD":"browser-smoke-password"}
    log=open(Path(tmp)/"server.log","w+")
    proc=subprocess.Popen([sys.executable,"-m","uvicorn","termokontur.app:app","--host","127.0.0.1","--port",str(port)],cwd=ROOT,env=env,stdout=log,stderr=log)
    base=f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                if httpx.get(base+"/health", trust_env=False).status_code==200:break
            except httpx.HTTPError:pass
            time.sleep(.1)
        else:
            log.seek(0);raise RuntimeError(log.read())
        with httpx.Client(base_url=base, trust_env=False) as api:
            assert api.post("/api/login",json={"password":"browser-smoke-password"}).status_code==200
            room=api.post("/api/rooms",json={"building":"Школа","name":"12"}).json()
            with sync_playwright() as pw:
                browser=pw.chromium.launch(executable_path=os.getenv("CHROMIUM_PATH") or shutil.which("chromium"),headless=True,args=["--no-sandbox"])
                page=browser.new_page(viewport={"width":1440,"height":1000})
                errors=[];page.on("pageerror",lambda e:errors.append(str(e)))
                page.goto(base);page.fill("#password","browser-smoke-password");page.click('#login-form button[type="submit"]')
                page.wait_for_selector("#app-root",state="visible")
                page.click('[data-tab="sensors"]');expect(page.locator('#sensor-state')).to_contain_text('Добавьте')
                page.click("#sensor-add");page.fill("#sensor-new-name","Датчик <тест>")
                with page.expect_response(lambda r:r.url.endswith('/api/sensors/devices') and r.request.method=='POST') as response:
                    page.click('#sensor-add-form button:not([type="button"])')
                created=response.value.json();page.wait_for_selector("#sensor-code-dialog",state="visible")
                assert page.input_value("#sensor-code-id")==created["device_id"]
                page.click("#sensor-code-close")
                enroll=api.post("/api/sensors/enroll",json={"device_id":created["device_id"],"code":created["code"],"model":"c3-sht40-v1","hardware_id":"0123456789ab"})
                assert enroll.status_code==200,enroll.text
                records=[{"seq":i,"boot_id":"0123456789abcdef","uptime_s":i*1800,"measured_at":int(time.time())-86400+i*1800,"time_quality":"rtc","temperature":20+i*.1,"humidity":40+i*.2,"battery_mv":3910,"interval_s":1800,"error":""} for i in range(1,49)]
                sync=api.post('/api/sensors/devices/'+created['device_id']+'/sync',headers={"Authorization":"Bearer "+enroll.json()["token"]},json={"model":"c3-sht40-v1","firmware":"0.1.0","records":records,"next_seq":49,"lost":0,"rssi":-65,"battery_mv":3910,"config_version":1,"boots":48})
                assert sync.status_code==200,sync.text
                page.click("#sensor-refresh");page.locator(".sensor-card button").first.click();page.wait_for_selector("#sensor-detail",state="visible")
                expect(page.locator("#sensor-records tr")).to_have_count(48)
                assert "Датчик <тест>"==page.text_content("#sensor-title")
                page.locator('#sensor-detail details summary').nth(0).click()
                page.fill('#sensor-config-form input[name="measure_s"]','3600')
                page.click('#sensor-config-form button')
                expect(page.locator('#sensor-detail-state')).to_contain_text('1 / 2')
                for width,height,theme in [(1440,1000,'light'),(390,844,'light'),(390,844,'dark')]:
                    page.set_viewport_size({"width":width,"height":height})
                    page.evaluate('(value)=>window.termokonturTheme.setPreference(value)',theme)
                    page.locator('#sensor-title').scroll_into_view_if_needed()
                    page.screenshot(path=f'/tmp/termokontur-sensors-{width}-{theme}.png')
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1'),f"horizontal overflow at {width}"
                assert not errors,errors
                browser.close()
        print("Browser smoke passed: login, create/enroll, 48 records, config delivery, desktop/mobile, light/dark, no JS errors or page overflow")
    finally:
        proc.terminate();proc.wait(timeout=10);log.close()
