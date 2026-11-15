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
   if(req.cmd==='hello')Object.assign(response,{protocol:1,public_key:hex(publicBytes),fingerprint:window.simWrongFingerprint?'0'.repeat(64):fp,hardware_id:'0123456789ab',sensor_type:'sht4x',firmware:'0.1.4',pending_records:false});
   else if(req.cmd==='sign')response.signature=hex(der(await crypto.subtle.sign({name:'ECDSA',hash:'SHA-256'},pair.privateKey,new TextEncoder().encode(req.message))));
   else if(req.cmd==='configure'){window.savedUSB=req;response.saved=true;}
   const bytes=new TextEncoder().encode('startup log\nTKRPC '+JSON.stringify(response)+'\n');self.rx.enqueue(bytes.slice(0,19));self.rx.enqueue(bytes.slice(19));
  } });},async setSignals(){},async close(){}};return port;
 }}});
 window.radioConnect=async()=>{
  const pair=await key;const raw=new Uint8Array(await crypto.subtle.exportKey('raw',pair.publicKey));const ident={public_key:hex(raw),hardware_id:'0123456789ab',sensor_type:window.savedUSB.sensor_type,firmware:'0.1.4'};
  const post=async(path,body)=>{const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});if(!r.ok)throw Error(await r.text());return r.json();};
  const challenge=await post('/api/sensors/auth/challenge',ident);
  const signature=hex(der(await crypto.subtle.sign({name:'ECDSA',hash:'SHA-256'},pair.privateKey,new TextEncoder().encode(challenge.message))));
  const authorized=await post('/api/sensors/auth/prove',{...ident,challenge:challenge.challenge,signature});
  const record={seq:1,boot_id:'0000000000000001',uptime_s:60,measured_at:Math.floor(Date.now()/1000)-10,time_quality:'rtc',temperature:21.5,humidity:null,battery_mv:3950,interval_s:60,error:''};
  const reply=await fetch('/api/sensors/devices/'+authorized.device_id+'/sync',{method:'POST',headers:{'Content-Type':'application/json',Authorization:'Bearer '+authorized.token},body:JSON.stringify({model:'c3-sht40-v1',sensor_type:ident.sensor_type,firmware:'0.1.4',records:[record],next_seq:2,lost:0,rssi:-55,battery_mv:3950,config_version:window.savedUSB.config.version,boots:1})});
  if(!reply.ok)throw Error(await reply.text());return reply.json();
 };
})();'''

spec=importlib.util.spec_from_file_location('onboarding_fixture',ROOT/'tests/test_sensor_onboarding.py')
fixtures=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixtures)
with tempfile.TemporaryDirectory(prefix='sensor-onboard-ui-') as tmp:
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    env={**os.environ,'TERMOKONTUR_DB':str(Path(tmp)/'test.sqlite3'),
         'TERMOKONTUR_SECRET':'usb-browser-test-secret-at-least-32-characters',
         'TERMOKONTUR_ADMIN_PASSWORD':'browser-test-password'}
    with open(Path(tmp)/'server.log','w+') as log:
        server=subprocess.Popen([sys.executable,'-m','uvicorn','termokontur.app:app','--host','127.0.0.1','--port',str(port)],cwd=ROOT,env=env,stdout=log,stderr=log)
        base=f'http://127.0.0.1:{port}'
        try:
            for _ in range(100):
                try:
                    if httpx.get(base+'/health',trust_env=False).is_success:break
                except httpx.HTTPError:pass
                time.sleep(.1)
            else:log.seek(0);raise RuntimeError(log.read())
            with httpx.Client(base_url=base,trust_env=False) as api,sync_playwright() as pw:
                assert api.post('/api/login',json={'password':'browser-test-password'}).status_code==200
                room=api.post('/api/rooms',json={'building':'Демо: школа','name':'Кабинет 101'}).json()
                assert api.post('/api/sensors/installer',files={'file':('demo.zip',fixtures.kit())}).status_code==200
                browser=pw.chromium.launch(executable_path=shutil.which('chromium'),headless=True,args=['--no-sandbox'])
                page=browser.new_page(viewport={'width':1440,'height':1100},locale='ru-RU')
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
                # Import the actual vendored SDK under the application's CSP.
                assert page.evaluate("async()=>typeof (await import('/static/vendor/esptool.bundle.js')).ESPLoader")=='function'
                page.click('#sensor-usb-open');page.wait_for_selector('#sensor-usb-dialog',state='visible')
                page.fill('#usb-name','Демо: USB-датчик');page.select_option('#usb-type','ds18b20');page.fill('#usb-ssid','School-IoT-2.4G');page.fill('#usb-wifi-password','SchoolPassDemo2026');page.fill('#usb-server','https://monitor.school.example')
                page.locator('#sensor-usb-form details summary').click()
                page.fill('#sensor-usb-form input[name="measure_s"]','60');page.fill('#sensor-usb-form input[name="send_s"]','600');page.fill('#sensor-usb-form input[name="cool_s"]','300')
                page.locator('#sensor-usb-form details summary').click()
                page.check('#usb-new-board');page.locator('#sensor-usb-dialog').evaluate('el=>el.scrollTop=0');page.screenshot(path='/tmp/termokontur-usb-setup-desktop.png')
                if '--screenshots' in sys.argv:
                    page.screenshot(path=str(ROOT/'docs/sensors/images/10-usb-setup.jpg'),type='jpeg',quality=85)
                page.click('#usb-flash');expect(page.locator('#usb-status')).to_contain_text('Готово',timeout=30000)
                assert page.evaluate('window.simFlashed') is True
                saved=page.evaluate('window.savedUSB');assert saved['ssid']=='School-IoT-2.4G' and saved['password']=='SchoolPassDemo2026'
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
                assert not errors,errors
                browser.close()
            print('Browser/API passed: simulated ROM flash, real key proof, automatic USB approval, private USB settings, signed radio auth+ingest, pending fallback, desktop/mobile and local CSP bundle. Physical USB not tested.')
        finally:
            server.terminate();server.wait(timeout=10)
