"""Real panel/API + simulated USB/P-256 board. Does not flash physical hardware."""
from pathlib import Path
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

import httpx
from playwright.sync_api import expect, sync_playwright

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
SIMULATED_USB=r'''(() => {
 const key=crypto.subtle.generateKey({name:'ECDSA',namedCurve:'P-256'},true,['sign','verify']);
 const hex=b=>Array.from(b,x=>x.toString(16).padStart(2,'0')).join('');
 const der=raw=>{const data=new Uint8Array(raw);const integer=part=>{let a=Array.from(part);while(a.length>1&&a[0]===0)a.shift();if(a[0]&128)a.unshift(0);return [2,a.length,...a];};const r=integer(data.slice(0,32)),s=integer(data.slice(32));return new Uint8Array([48,r.length+s.length,...r,...s]);};
 let port;
 Object.defineProperty(navigator,'serial',{value:{requestPort:async()=>{
  if(window.simCancel)throw new DOMException('Dismissed','NotFoundError');
  port={async open(){const self=this;this.readable=new ReadableStream({start(c){self.rx=c;}});this.writable=new WritableStream({async write(value){
   const req=JSON.parse(new TextDecoder().decode(value).trim().slice(6));let response={id:req.id};const pair=await key;
   const publicBytes=new Uint8Array(await crypto.subtle.exportKey('raw',pair.publicKey));const fp=hex(new Uint8Array(await crypto.subtle.digest('SHA-256',publicBytes)));
   if(req.cmd==='hello')Object.assign(response,{protocol:1,public_key:hex(publicBytes),fingerprint:window.simWrongFingerprint?'0'.repeat(64):fp,hardware_id:'0123456789ab',sensor_type:'sht4x',firmware:'0.1.5',pending_records:false});
   else if(req.cmd==='sign')response.signature=hex(der(await crypto.subtle.sign({name:'ECDSA',hash:'SHA-256'},pair.privateKey,new TextEncoder().encode(req.message))));
   else if(req.cmd==='configure'){window.savedUSB=req;response.saved=true;}
   else if(req.cmd==='test_measurement')Object.assign(response,{temperature:21.5,humidity:44.2,battery_mv:3950,sensor_error:''});
   else if(req.cmd==='setup_wifi')Object.assign(response,{ssid:'Termokontur-1234',password:' Wi-Fi <demo> & pass  '});
   else if(req.cmd==='arm_boot_debug'){response.duration_s=300;window.simBootArmed=true;}
   const bytes=new TextEncoder().encode('startup log\nSETUP password: masked-test-password\nTKRPC '+JSON.stringify(response)+'\n');self.rx.enqueue(bytes.slice(0,19));self.rx.enqueue(bytes.slice(19));
  } });},async setSignals(){},async close(){}};return port;
 }}});
 window.simPressBoot=()=>{
  if(!window.simBootArmed)throw Error('BOOT debug is inactive');
  window.simBootArmed=false;
  port.rx.enqueue(new TextEncoder().encode('BOOT: Сервер подтвердил получение замера.\n'));
 };
 window.radioConnect=async()=>{
  const pair=await key;const raw=new Uint8Array(await crypto.subtle.exportKey('raw',pair.publicKey));const ident={public_key:hex(raw),hardware_id:'0123456789ab',sensor_type:window.savedUSB.sensor_type,firmware:'0.1.5'};
  const post=async(path,body)=>{const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});if(!r.ok)throw Error(await r.text());return r.json();};
  const challenge=await post('/api/sensors/auth/challenge',ident);
  const signature=hex(der(await crypto.subtle.sign({name:'ECDSA',hash:'SHA-256'},pair.privateKey,new TextEncoder().encode(challenge.message))));
  const authorized=await post('/api/sensors/auth/prove',{...ident,challenge:challenge.challenge,signature});
  const record={seq:1,boot_id:'0000000000000001',uptime_s:60,measured_at:Math.floor(Date.now()/1000)-10,time_quality:'rtc',temperature:21.5,humidity:null,battery_mv:3950,interval_s:60,error:''};
  const reply=await fetch('/api/sensors/devices/'+authorized.device_id+'/sync',{method:'POST',headers:{'Content-Type':'application/json',Authorization:'Bearer '+authorized.token},body:JSON.stringify({model:'c3-sht40-v1',sensor_type:ident.sensor_type,firmware:'0.1.5',records:[record],next_seq:2,lost:0,rssi:-55,battery_mv:3950,config_version:window.savedUSB.config.version,boots:1})});
  if(!reply.ok)throw Error(await reply.text());return reply.json();
 };
})();'''

spec=importlib.util.spec_from_file_location('onboarding_fixture',ROOT/'tests/test_sensor_onboarding.py')
fixtures=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixtures)
with tempfile.TemporaryDirectory(prefix='sensor-onboard-ui-') as tmp:
    version = fixtures.__version__
    asset = Path(tmp) / 'firmware' / f'{version}.zip'
    asset.parent.mkdir()
    supplied_bundle = os.getenv('TEST_FIRMWARE_BUNDLE')
    asset.write_bytes(Path(supplied_bundle).read_bytes() if supplied_bundle else fixtures.kit(version))
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    env={**os.environ,'TERMOKONTUR_DB':str(Path(tmp)/'test.sqlite3'),
         'TERMOKONTUR_SECRET':'usb-browser-test-secret-at-least-32-characters',
         'TERMOKONTUR_ADMIN_PASSWORD':'browser-test-password'}
    spec_tls=importlib.util.spec_from_file_location('tls_fixture',ROOT/'tests/test_tls.py')
    tls_fixtures=importlib.util.module_from_spec(spec_tls);spec_tls.loader.exec_module(tls_fixtures)
    certificate,key=tls_fixtures.pair("localhost")
    cert_path,key_path=Path(tmp)/'cert.pem',Path(tmp)/'key.pem'
    cert_path.write_bytes(certificate);key_path.write_bytes(key)
    env['TERMOKONTUR_SENSOR_CA_FILE']=str(cert_path)
    with open(Path(tmp)/'server.log','w+') as log:
        server=subprocess.Popen([sys.executable,'-m','uvicorn','termokontur.app:app','--host','127.0.0.1','--port',str(port),'--ssl-certfile',str(cert_path),'--ssl-keyfile',str(key_path)],cwd=ROOT,env=env,stdout=log,stderr=log)
        base=f'https://127.0.0.1:{port}'
        try:
            for _ in range(100):
                try:
                    if httpx.get(base+'/health',trust_env=False,verify=False).is_success:break
                except httpx.HTTPError:pass
                time.sleep(.1)
            else:log.seek(0);raise RuntimeError(log.read())
            with httpx.Client(base_url=base,trust_env=False,verify=False) as api,sync_playwright() as pw:
                assert api.post('/api/login',json={'password':'browser-test-password'}).status_code==200
                room=api.post('/api/rooms',json={'building':'Демо: школа','name':'Кабинет 101'}).json()
                assert api.post('/api/sensors/installer',files={'file':('demo.zip',fixtures.kit('0.1.5'))}).status_code==200
                browser=pw.chromium.launch(executable_path=os.getenv('CHROMIUM_PATH') or shutil.which('chromium'),headless=True,args=['--no-sandbox'])
                page=browser.new_page(ignore_https_errors=True,viewport={'width':1440,'height':1100},locale='ru-RU')
                errors=[];requests=[]
                page.on('pageerror',lambda error:errors.append(str(error)))
                page.on('request',lambda r:requests.append((r.url,r.post_data)) if r.post_data else None)
                page.add_init_script(SIMULATED_USB)
                # The ROM transfer is simulated; the controller still downloads the real API image.
                real=(ROOT/'termokontur/static/sensor-usb.mjs').read_text()
                page.route('**/static/test-real-sensor-usb.mjs',lambda route:route.fulfill(content_type='text/javascript',body=real))
                wrapper='''export {SensorSerial,verifyIdentity} from './test-real-sensor-usb.mjs';
                export async function flashFactory(port,image,metadata,callbacks){
                  if(image.byteLength!==metadata.size)throw Error('size mismatch');
                  await callbacks.checkBoard('0123456789ab');callbacks.progress(100);window.simFlashed=true;return '0123456789ab';
                }'''
                page.route('**/static/sensor-usb.mjs',lambda route:route.fulfill(content_type='text/javascript',body=wrapper))
                page.goto(base);page.fill('#password','browser-test-password');page.click('#login-form button[type="submit"]')
                page.wait_for_selector('#app-root',state='visible');page.click('[data-tab="sensors"]')
                firmware_url = f'/api/sensors/firmware/bundled/{version}'
                expect(page.locator('#sensor-bundled-status')).to_contain_text(version)
                with page.expect_download() as download:
                    page.click('#sensor-c3-ota-file')
                assert download.value.suggested_filename == f'termokontur-esp32-c3-{version}.bin'
                # Import the actual vendored SDK under the application's CSP.
                assert page.evaluate("async()=>typeof (await import('/static/vendor/esptool.bundle.js')).ESPLoader")=='function'
                page.click('#sensor-usb-open');page.wait_for_selector('#sensor-usb-dialog',state='visible')
                expect(page.locator('#usb-firmware')).to_have_value(f'{firmware_url}/esp32-c3-factory.bin')
                expect(page.locator('#usb-firmware option')).to_have_count(2)
                page.fill('#usb-name','Демо: USB-датчик');page.select_option('#usb-type','ds18b20');page.fill('#usb-ssid','School-IoT-2.4G');page.fill('#usb-wifi-password','SchoolPassDemo2026')
                page.locator('#sensor-usb-form details summary').click()
                page.fill('#sensor-usb-form input[name="measure_s"]','60');page.fill('#sensor-usb-form input[name="send_s"]','600');page.fill('#sensor-usb-form input[name="cool_s"]','300')
                page.locator('#sensor-usb-form details summary').click()
                page.check('#usb-new-board')
                # The chooser is asynchronous: prevent a second setup before it closes.
                page.evaluate("""() => {
                    window.originalRequestPort = navigator.serial.requestPort;
                    window.portRequests = 0;
                    navigator.serial.requestPort = () => new Promise((resolve, reject) => {
                        window.portRequests++; window.rejectPort = reject;
                    });
                }""")
                page.click('#usb-flash')
                expect(page.locator('#usb-flash')).to_be_disabled()
                expect(page.locator('#usb-configure')).to_be_disabled()
                expect(page.locator('#usb-close')).to_be_disabled()
                page.evaluate('usbRun(true)')
                assert page.evaluate('window.portRequests') == 1
                page.evaluate("window.rejectPort(new DOMException('Dismissed', 'NotFoundError'))")
                expect(page.locator('#usb-flash')).to_be_enabled()
                expect(page.locator('#usb-close')).to_be_enabled()
                page.evaluate('navigator.serial.requestPort = window.originalRequestPort')
                page.locator('#sensor-usb-dialog').evaluate('el=>el.scrollTop=0');page.screenshot(path='/tmp/termokontur-usb-setup-desktop.png')
                if '--screenshots' in sys.argv:
                    page.screenshot(path=str(ROOT/'docs/sensors/images/10-usb-setup.jpg'),type='jpeg',quality=85)
                page.evaluate('window.originalDateNow=Date.now;Date.now=()=>window.originalDateNow()+315360000000')
                with page.expect_response(base + firmware_url + '/esp32-c3-factory.bin') as image:
                    page.click('#usb-flash')
                assert image.value.ok
                expect(page.locator('#usb-status')).to_contain_text('Готово',timeout=30000)
                expect(page.locator('#usb-status')).to_contain_text('коротко нажмите RESET')
                assert page.evaluate('window.simFlashed') is True
                saved=page.evaluate('window.savedUSB');assert abs(saved['epoch']-time.time())<30 and saved['ca']==certificate.decode('ascii')
                assert saved['ssid']=='School-IoT-2.4G' and saved['password']=='SchoolPassDemo2026'
                page.evaluate('Date.now=window.originalDateNow')
                assert saved['sensor_type']=='ds18b20' and saved['config']['measure_s']==60
                assert page.input_value('#usb-wifi-password')==''
                onboard=[json.loads(body) for url,body in requests if url.endswith('/api/sensors/onboard')]
                assert len(onboard)==1 and 'ssid' not in onboard[0] and 'password' not in onboard[0]
                assert api.get('/api/sensors/claims').json()==[]
                assert len(api.get('/api/sensors/devices').json())==1
                page.locator('#sensor-usb-dialog').evaluate('el=>el.scrollTop=0')
                page.screenshot(path='/tmp/termokontur-usb-ready-desktop.png')
                if '--screenshots' in sys.argv:
                    page.screenshot(path=str(ROOT/'docs/sensors/images/11-usb-authorized.jpg'),type='jpeg',quality=85)
                result=page.evaluate('window.radioConnect()');assert result['ack_seq']==2
                page.click('#usb-close');page.click('#sensor-refresh')
                expect(page.locator('.sensor-card strong')).to_contain_text('Данные получены')
                page.locator('.sensor-card').get_by_role('button', name='Открыть').click()
                page.locator('#sensor-detail summary').filter(has_text='Обновление прошивки').click()
                page.select_option('#sensor-firmware', f'bundled:{version}')
                with page.expect_response(lambda r: r.url.endswith(f'/ota?bundled_version={version}')) as ota:
                    page.get_by_role('button', name='Назначить обновление').click()
                assert ota.value.ok
                # A request from another self-keyed device appears as pending, not authorized.
                private,ident=fixtures.identity();ident['hardware_id']='1123456789ab'
                signed=fixtures.proof(api,private,ident)
                assert api.post('/api/sensors/auth/prove',json=signed).json()['status']=='pending'
                page.click('#sensor-refresh');expect(page.locator('#sensor-claims-panel')).to_be_visible()
                page.locator('#sensor-claims input').fill('Демо: заявка по сети');page.locator('#sensor-claims button').first.click()
                expect(page.locator('#sensor-claims-panel')).to_be_hidden()
                for width,height in [(1440,1100),(390,844)]:
                    page.set_viewport_size({'width':width,'height':height});page.click('#sensor-usb-open')
                    expect(page.locator('#sensor-usb-dialog')).to_be_visible();assert page.evaluate('document.documentElement.scrollWidth <= innerWidth+1')
                    assert page.locator('#sensor-usb-dialog').evaluate('el=>el.scrollWidth<=el.clientWidth+1')
                    page.screenshot(path=f'/tmp/termokontur-usb-{width}.png');page.click('#usb-close')
                page.get_by_role('link', name='Отладка прошивки').click()
                expect(page.get_by_role('heading', name='Отладка прошивки')).to_be_visible()
                page.click('#debug-connect')
                expect(page.locator('#debug-hardware')).to_have_text('0123456789ab')
                expect(page.locator('#debug-result')).to_contain_text(version)
                page.click('#debug-measure')
                expect(page.locator('#debug-temperature')).to_have_text('21,5 °C')
                expect(page.locator('#debug-humidity')).to_have_text('44,2 %')
                expect(page.locator('#debug-battery')).to_have_text('3,95 В')
                expect(page.locator('#debug-wifi-details')).to_be_hidden()
                page.click('#debug-wifi')
                expect(page.locator('#debug-wifi-details')).to_be_visible()
                expect(page.locator('#debug-wifi-ssid')).to_have_text('Termokontur-1234')
                assert page.locator('#debug-wifi-password').text_content() == ' Wi-Fi <demo> & pass  '
                expect(page.locator('#debug-log')).to_contain_text('SETUP password: [скрыт]')
                expect(page.locator('#debug-log')).not_to_contain_text('masked-test-password')
                with page.expect_download() as debug_log:
                    page.click('#debug-save')
                saved_log = debug_log.value.path().read_text(encoding='utf-8')
                assert 'Пробный замер' in saved_log
                assert 'masked-test-password' not in saved_log and 'Wi-Fi <demo> & pass' not in saved_log
                assert all('Wi-Fi <demo> & pass' not in body for _, body in requests)
                page.click('#debug-wifi')
                expect(page.locator('#debug-wifi-details')).to_be_hidden()
                expect(page.locator('#debug-wifi-password')).to_be_empty()
                page.click('#debug-wifi')
                expect(page.locator('#debug-wifi-details')).to_be_visible()
                page.click('#debug-boot')
                expect(page.locator('#debug-result')).to_contain_text('коротко нажмите BOOT в течение 5 мин.')
                expect(page.locator('#debug-measure')).to_be_disabled()
                expect(page.locator('#debug-wifi-details')).to_be_hidden()
                page.evaluate('window.simPressBoot()')
                expect(page.locator('#debug-result')).to_have_text('Сервер подтвердил получение замера.')
                expect(page.locator('#debug-log')).to_contain_text('BOOT: Сервер подтвердил получение замера.')
                page.click('#debug-disconnect')
                expect(page.locator('#debug-connect')).to_be_enabled()
                expect(page.locator('#debug-wifi-details')).to_be_hidden()
                expect(page.locator('#debug-wifi-password')).to_be_empty()
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth+1')
                assert not errors,errors
                browser.close()
            print('Browser/API passed: bundled firmware, simulated USB flash and diagnostics, authorization, signed ingest, and desktop/mobile layout. Physical USB not tested.')
        finally:
            server.terminate();server.wait(timeout=10)
