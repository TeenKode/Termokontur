"""Run against an isolated temporary server; requires Playwright + Chromium."""
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from zoneinfo import ZoneInfo

import httpx
from playwright.sync_api import expect, sync_playwright

ROOT=Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory(prefix="termokontur-ui-") as tmp:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1",0));port=sock.getsockname()[1]
    env={**os.environ,"TERMOKONTUR_DB":str(Path(tmp)/"smoke.sqlite3"),
         "TERMOKONTUR_SECRET":"browser-smoke-secret-at-least-32-characters",
         "TERMOKONTUR_ADMIN_PASSWORD":"browser-smoke-password",
         "TERMOKONTUR_TZ":"Asia/Vladivostok"}
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
                page=browser.new_page(viewport={"width":1440,"height":1000},timezone_id="America/Los_Angeles")
                errors=[];page.on("pageerror",lambda e:errors.append(str(e)))
                page.goto(base);page.fill("#password","browser-smoke-password");page.click('#login-form button[type="submit"]')
                page.wait_for_selector("#app-root",state="visible")
                page.click('[data-tab="sensors"]');expect(page.locator('#sensor-state')).to_contain_text('Подключите первый датчик')
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
                local = datetime.fromtimestamp(records[-1]["measured_at"], ZoneInfo("Asia/Vladivostok"))
                expect(page.locator("#sensor-records tr").first.locator("td").nth(1)).to_have_text(
                    local.strftime("%d.%m, %H:%M GMT+10"))
                assert "Датчик <тест>"==page.text_content("#sensor-title")
                page.locator('#sensor-detail details summary').nth(0).click()
                page.fill('#sensor-config-form input[name="measure_s"]','3600')
                page.click('#sensor-config-form button')
                expect(page.locator('#sensor-detail-state')).to_contain_text('1 / 2')
                # Remote debug is not offered on old firmware; upgrade the
                # simulated device, exercise queue/cancel and receipt rendering.
                page.locator("#sensor-boot-debug-details summary").click()
                expect(page.locator("#sensor-boot-debug-queue")).to_be_disabled()
                expect(page.locator("#sensor-boot-debug-state")).to_contain_text("1.5.34")
                endpoint='/api/sensors/devices/'+created['device_id']
                headers={"Authorization":"Bearer "+enroll.json()["token"]}
                payload={"model":"c3-sht40-v1","firmware":"1.5.34","records":[],
                         "next_seq":49,"lost":0,"rssi":-65,"battery_mv":3910,
                         "config_version":2,"boots":49,"boot_debug_supported":True}
                assert api.post(endpoint+'/sync',headers=headers,json=payload).status_code==200
                page.click("#sensor-refresh")
                expect(page.locator("#sensor-boot-debug-queue")).to_be_enabled()
                page.select_option("#sensor-boot-debug-duration","900")
                page.uncheck("#sensor-boot-debug-send")
                page.click("#sensor-boot-debug-queue")
                expect(page.locator("#sensor-boot-debug-state")).to_contain_text("Ожидает связи")
                command=api.get(endpoint+'/boot-debug').json()
                assert command["duration_s"]==900 and command["send_now"] is False
                page.click("#sensor-boot-debug-cancel")
                expect(page.locator("#sensor-boot-debug-state")).to_contain_text("Нет ожидающей")
                assert api.get(endpoint+'/boot-debug').json() is None
                page.click("#sensor-boot-debug-queue")
                expect(page.locator("#sensor-boot-debug-state")).to_contain_text("Ожидает связи")
                command=api.post(endpoint+'/sync',headers=headers,json=payload).json()["boot_debug"]
                confirmed=api.post(endpoint+'/sync',headers=headers,json={**payload,"boot_debug_ack":command["command_id"]})
                assert confirmed.status_code==200,confirmed.text
                page.click("#sensor-refresh")
                expect(page.locator("#sensor-boot-debug-state")).to_contain_text("Команда получена")
                expect(page.locator("#sensor-boot-debug-cancel")).to_be_disabled()
                for width,height,theme in [(1440,1000,'light'),(390,844,'light'),(390,844,'dark')]:
                    page.set_viewport_size({"width":width,"height":height})
                    page.evaluate('(value)=>window.termokonturTheme.setPreference(value)',theme)
                    page.locator('#sensor-title').scroll_into_view_if_needed()
                    page.screenshot(path=f'/tmp/termokontur-sensors-{width}-{theme}.png')
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1'),f"horizontal overflow at {width}"
                # BMP280 must display a real temperature without inventing humidity.
                bmp=api.post("/api/sensors/devices",json={"name":"BMP280 кабинет","room_id":room["id"]})
                assert bmp.status_code==200,bmp.text
                ident=bmp.json()
                enrollment=api.post("/api/sensors/enroll",json={
                    "device_id":ident["device_id"],"code":ident["code"],"model":"c3-sht40-v1",
                    "hardware_id":"aabbccddeeff","sensor_type":"bmp280",
                })
                assert enrollment.status_code==200,enrollment.text
                rec={"seq":1,"boot_id":"abcdef0123456789","uptime_s":1800,
                     "measured_at":int(time.time())-1800,"time_quality":"rtc",
                     "temperature":22.5,"humidity":None,"battery_mv":3880,"interval_s":1800,"error":""}
                sent=api.post('/api/sensors/devices/'+ident["device_id"]+'/sync',
                              headers={"Authorization":"Bearer "+enrollment.json()["token"]},
                              json={"model":"c3-sht40-v1","sensor_type":"bmp280",
                                    "firmware":"0.1.6","records":[rec],
                                    "next_seq":2,"lost":0,"rssi":-65,"battery_mv":3880,
                                    "config_version":1,"boots":1})
                assert sent.status_code==200,sent.text
                page.set_viewport_size({"width":1440,"height":1000})
                page.click("#sensor-refresh")
                expect(page.locator(".sensor-card")).to_have_count(2)
                page.locator(".sensor-card").filter(has_text="BMP280 кабинет").locator("button").click()
                expect(page.locator("#sensor-detail-state")).to_contain_text("BMP280")
                expect(page.locator("#sensor-records tr")).to_have_count(1)
                assert page.locator("#sensor-records tr").first.locator("td").nth(3).text_content()=="—"
                assert page.locator('#sensor-config-form .archive-toggle').inner_text().strip()=="Измерять температуру"
                labels=page.evaluate("""() => {
                    const context=document.getElementById("sensor-chart").getContext("2d");
                    const before=context.fillText, arc=context.arc, labels=[], points=[];
                    context.fillText=function(text,...args){ labels.push(String(text));return before.call(this,text,...args); };
                    context.arc=function(x,y,...args){ points.push([x,y]);return arc.call(this,x,y,...args); };
                    try{drawSensorChart();}finally{context.fillText=before;context.arc=arc;}
                    return {labels,points};
                }""")
                assert "100%" not in labels["labels"] and "0%" not in labels["labels"],labels
                assert len(labels["points"]) == 1,labels
                expect(page.locator("#sensor-temperature-key")).to_be_visible()
                expect(page.locator("#sensor-humidity-key")).to_be_hidden()
                page.click('[data-tab="settings"]')
                page.click('[data-settings-target="general"]')
                expect(page.locator("#setting-timezone")).to_have_value("Asia/Vladivostok")
                page.fill("#setting-timezone","UTC")
                page.click('#general-settings-form button[type="submit"]')
                expect(page.locator("#daily-date")).to_contain_text("UTC")
                page.click('[data-tab="sensors"]')
                page.click("#sensor-refresh")
                local = datetime.fromtimestamp(rec["measured_at"], ZoneInfo("UTC"))
                expect(page.locator("#sensor-records tr").first.locator("td").nth(1)).to_have_text(
                    local.strftime("%d.%m, %H:%M UTC"))
                assert not errors,errors
                browser.close()
        print("Browser smoke passed: login, create/enroll, 48 records, config delivery, desktop/mobile, light/dark, no JS errors or page overflow")
    finally:
        proc.terminate();proc.wait(timeout=10);log.close()
