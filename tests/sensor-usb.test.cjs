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
 async main(){return chip;}async getFlashSize(){return flashSize;}async writeFlash(opts){calls.push('write');assert.equal(opts.eraseAll,true);assert.equal(opts.fileArray[0].address,0);assert.equal(opts.calculateMD5Hash(opts.fileArray[0].data),'md5');opts.reportProgress(0,3,3);}
 async after(){calls.push('reset');}},md5:()=> 'md5'};
}
test('factory flasher verifies download, chip and existing-board policy before erase',async()=>{
 const {flashFactory}=await module_();const image=Uint8Array.from([1,2,3]).buffer;
 const sha=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',image)),b=>b.toString(16).padStart(2,'0')).join('');const metadata={sha256:sha,size:3};
 let lib=sdk();let progress=0;
 assert.equal(await flashFactory({},image,metadata,{sdk:lib,checkBoard:async mac=>assert.equal(mac,'0123456789ab'),progress:n=>progress=n,log(){}}),'0123456789ab');
 assert.deepEqual(lib.calls,['write','reset','disconnect']);assert.equal(progress,100);
 lib=sdk();await assert.rejects(flashFactory({},image,{...metadata,sha256:'0'.repeat(64)},{sdk:lib}),/Контрольная/);assert.deepEqual(lib.calls,[]);
 lib=sdk('ESP32-S3');await assert.rejects(flashFactory({},image,metadata,{sdk:lib,log(){}}),/ESP32-C3/);assert.deepEqual(lib.calls,['disconnect']);
 lib=sdk('ESP32-C3',2048);await assert.rejects(flashFactory({},image,metadata,{sdk:lib,log(){}}),/Flash/);assert.deepEqual(lib.calls,['disconnect']);
 lib=sdk();await assert.rejects(flashFactory({},image,metadata,{sdk:lib,log(){},checkBoard:async()=>{throw Error('registered');}}),/registered/);assert.deepEqual(lib.calls,['disconnect']);
});
