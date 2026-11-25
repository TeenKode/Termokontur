const test=require("node:test");
const assert=require("node:assert/strict");
const {webcrypto}=require("node:crypto");
globalThis.crypto=webcrypto;
const module_=()=>import("../termokontur/static/firmware-test.mjs");
async function fake(samples){
  const key=await crypto.subtle.generateKey({name:"ECDSA",namedCurve:"P-256"},true,["sign","verify"]);
  const bytes=new Uint8Array(await crypto.subtle.exportKey("raw",key.publicKey));
  const hex=b=>Buffer.from(b).toString("hex");
  const hello={protocol:1,public_key:hex(bytes),fingerprint:hex(await crypto.subtle.digest("SHA-256",bytes)),hardware_id:"0123456789ab",firmware:"1.3.20",sensor_type:"sht4x",provisioned:true,password:"secret-never-export"};
  const commands=[];
  return {commands,request:async cmd=>{commands.push(cmd);if(cmd==="hello")return hello;return samples.shift();}};
}
test("bench tests only read identity and measurements, without exporting credentials",async()=>{
  const {runBenchTest}=await module_();
  const sensor=await fake(Array.from({length:5},()=>({temperature:21.2,humidity:44,battery_mv:3900})));
  const r=await runBenchTest(sensor,"1.3.20",{wait:async()=>{}});
  assert.deepEqual(sensor.commands,["hello",...Array(5).fill("test_measurement")]);
  assert.ok(r.steps.every(x=>x.status==="pass"));
  assert.equal(r.samples.length,5);
  assert.doesNotMatch(JSON.stringify(r),/secret-never-export|public_key|fingerprint/);
});
test("wrong version, sensor failure and bad ADC remain visible",async()=>{
  const {runBenchTest,assessSample}=await module_();
  const sensor=await fake([{temperature:21,humidity:101,battery_mv:0}]);
  const r=await runBenchTest(sensor,"1.3.21",{samples:1,wait:async()=>{}});
  assert.equal(r.steps.find(x=>x.id==="version").status,"warn");
  assert.equal(r.steps.find(x=>x.id==="sensor").status,"fail");
  assert.equal(r.steps.find(x=>x.id==="adc").status,"warn");
  assert.equal(assessSample({temperature:22,humidity:null},"bmp280").ok,true);
  assert.equal(assessSample({sensor_error:"sensor_crc"},"sht4x").ok,false);
});
test("cancellation prevents subsequent USB requests",async()=>{
  const {runBenchTest}=await module_();
  const sensor=await fake([{temperature:21,humidity:40,battery_mv:3800}]);
  let cancelled=false;
  const r=await runBenchTest(sensor,"1.3.20",{cancelled:()=>cancelled,wait:async()=>{cancelled=true;}});
  assert.equal(r.cancelled,true);
  assert.deepEqual(sensor.commands,["hello","test_measurement"]);
});
