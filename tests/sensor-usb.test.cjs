const {test}=require('node:test');
const assert=require('node:assert/strict');
const {webcrypto}=require('node:crypto');
if(!globalThis.crypto)globalThis.crypto=webcrypto;
const module_=()=>import('../termokontur/static/sensor-usb.mjs');
const enc=new TextEncoder();
function port(handler){
 let out;const p={closed:false,readable:new ReadableStream({start(c){out=c;}}),writable:new WritableStream({write(bytes){handler(JSON.parse(new TextDecoder().decode(bytes).trim().slice(6)),reply=>{const b=enc.encode('boot log\nTKRPC invalid-json\nTKRPC '+JSON.stringify(reply)+'\n');out.enqueue(b.slice(0,17));out.enqueue(b.slice(17));});}}),async open(){},async setSignals(){},async close(){this.closed=true;}};return p;
}
test('USB framing tolerates logs and fragmented responses, and releases the port',async()=>{
 const {SensorSerial}=await module_();const p=port((req,send)=>send({id:req.id,protocol:1}));
 const logs=[];const serial=new SensorSerial(p,100,line=>logs.push(line));
 await serial.open();assert.equal((await serial.request('hello')).protocol,1);assert.deepEqual(logs,['boot log']);
 await serial.close();assert.equal(p.closed,true);
});
test('USB errors and timeout do not report successful configuration',async()=>{
 const {SensorSerial}=await module_();let p=port((req,send)=>send({id:req.id,error:'write failed'}));let s=new SensorSerial(p,20);await s.open();await assert.rejects(s.request('configure'),/write failed/);await s.close();
 p=port(()=>{});s=new SensorSerial(p,20);await s.open();await assert.rejects(s.request('hello'),/Нет ответа платы/);await s.close();
});
test('USB identity rejects fingerprint substitution and a different physical board',async()=>{
 const {verifyIdentity}=await module_();const key=await crypto.subtle.generateKey({name:'ECDSA',namedCurve:'P-256'},true,['sign','verify']);const bytes=new Uint8Array(await crypto.subtle.exportKey('raw',key.publicKey));
 const hex=b=>Array.from(b,x=>x.toString(16).padStart(2,'0')).join('');const fp=hex(new Uint8Array(await crypto.subtle.digest('SHA-256',bytes)));
 const hello={protocol:1,public_key:hex(bytes),fingerprint:fp,hardware_id:'0123456789ab'};
 assert.equal(await verifyIdentity(hello,'0123456789ab'),fp);
 await assert.rejects(verifyIdentity({...hello,fingerprint:'0'.repeat(64)}),/Отпечаток/);
 await assert.rejects(verifyIdentity(hello,'1123456789ab'),/другой платы/);
});
function sdk(chip='ESP32-C3',flashSize=4096){
 const calls=[];return {calls,Transport:class{async disconnect(){calls.push('disconnect');}},ESPLoader:class{
 constructor(){this.chip={readMac:async()=> '01:23:45:67:89:ab'};}
 async main(){return chip;}async getFlashSize(){return flashSize;}async writeFlash(opts){calls.push('write');assert.equal(opts.eraseAll,true);assert.equal(opts.fileArray[0].address,0);assert.equal(opts.flashMode,'keep');assert.equal(opts.flashFreq,'keep');assert.equal(opts.flashSize,'keep');assert.deepEqual(Array.from(opts.fileArray[0].data,c=>c.charCodeAt(0)),Array.from(factoryBytes()));assert.equal(opts.calculateMD5Hash(opts.fileArray[0].data),'md5');opts.reportProgress(0,3,3);}
 async after(){calls.push('reset');}},md5:()=> 'md5'};
}
function factoryBytes(){
 const bytes=new Uint8Array(0x20000+256);
 bytes.set([0xe9,1,2,0x2f]);bytes[12]=5;bytes[0x20000]=0xe9;bytes[0x20000+12]=5;
 return bytes;
}
async function metadataFor(image){
 return {size:image.byteLength,sha256:Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',image)),b=>b.toString(16).padStart(2,'0')).join('')};
}
test('factory flasher verifies download, chip and existing-board policy before erase',async()=>{
 const {flashFactory}=await module_();const image=factoryBytes().buffer;
 const metadata=await metadataFor(image);
 let lib=sdk();let progress=0;
 assert.equal(await flashFactory({},image,metadata,{sdk:lib,checkBoard:async mac=>assert.equal(mac,'0123456789ab'),progress:n=>progress=n,log(){}}),'0123456789ab');
 assert.deepEqual(lib.calls,['write','reset','disconnect']);assert.equal(progress,100);
 lib=sdk();await assert.rejects(flashFactory({},image,{...metadata,sha256:'0'.repeat(64)},{sdk:lib}),/Контрольная/);assert.deepEqual(lib.calls,[]);
 lib=sdk('ESP32-S3');await assert.rejects(flashFactory({},image,metadata,{sdk:lib,log(){}}),/ESP32-C3/);assert.deepEqual(lib.calls,['disconnect']);
 lib=sdk('ESP32-C3',2048);await assert.rejects(flashFactory({},image,metadata,{sdk:lib,log(){}}),/Flash/);assert.deepEqual(lib.calls,['disconnect']);
 lib=sdk();await assert.rejects(flashFactory({},image,metadata,{sdk:lib,log(){},checkBoard:async()=>{throw Error('registered');}}),/registered/);assert.deepEqual(lib.calls,['disconnect']);
});

test('factory flasher rejects valid-checksum but invalid headers before connecting',async()=>{
 const {flashFactory}=await module_();
 for(const [offset,value,pattern] of [[0,0,/заводской образ/],[12,0,/заводской образ/],[2,0,/параметры Flash/],[3,0x20,/параметры Flash/],[0x20000,0,/заводской образ/]]){
  const bytes=factoryBytes();bytes[offset]=value;const image=bytes.buffer;const lib=sdk();
  await assert.rejects(flashFactory({},image,await metadataFor(image),{sdk:lib,log(){}}),pattern);
  assert.deepEqual(lib.calls,[]);
 }
});

test('USB reconnect retries a busy port and verifies the re-enumerated board',async()=>{
 const {connectSensor}=await module_();
 const bytes=Uint8Array.from({length:65},(_,i)=>i===0?4:i);
 const hex=b=>Array.from(b,x=>x.toString(16).padStart(2,'0')).join('');
 const fingerprint=hex(new Uint8Array(await crypto.subtle.digest('SHA-256',bytes)));
 const hello={protocol:1,public_key:hex(bytes),fingerprint,hardware_id:'0123456789ab'};
 const busy={async open(){throw Error('busy');}};
 const replacement=port((req,send)=>send({...hello,id:req.id}));
 const wrong=port((req,send)=>send({...hello,hardware_id:'1123456789ab',id:req.id}));
 let scans=0;
 const result=await connectSensor(busy,{expectedHardware:hello.hardware_id,getPorts:async()=>++scans===1?[]:[wrong,replacement],attempts:3,timeout:30,delay:1});
 assert.equal(result.hello.hardware_id,hello.hardware_id);assert.equal(scans,2);assert.equal(wrong.closed,true);
 await result.serial.close();
});

test('factory seed changes only reserved partition and contains CRC-protected JSON',async()=>{
 const {factorySeedImage}=await module_();const bytes=factoryBytes();bytes.fill(255,0x12000,0x16000);
 const v=new DataView(bytes.buffer);v.setUint16(0x8000,0x50aa,true);bytes[0x8002]=1;bytes[0x8003]=0x41;
 v.setUint32(0x8004,0x12000,true);v.setUint32(0x8008,0x4000,true);bytes.set(new TextEncoder().encode('factory_setup'),0x800c);
 const settings={ssid:'Тест',factory_ticket:'a'.repeat(64)},out=factorySeedImage(bytes,settings),head=new DataView(out.buffer);
 assert.equal(head.getUint32(0x12000,true),0x31534b54);
 const length=head.getUint32(0x12004,true),payload=out.slice(0x1200c,0x1200c+length);
 assert.deepEqual(JSON.parse(new TextDecoder().decode(payload)),settings);
 assert.deepEqual(out.slice(0,0x12000),bytes.slice(0,0x12000));assert.deepEqual(out.slice(0x16000),bytes.slice(0x16000));
 assert.equal(bytes[0x12000],255);assert.throws(()=>factorySeedImage(out,settings),/чистый раздел/);
});
