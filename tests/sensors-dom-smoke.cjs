"use strict";
// DOM/API integration without layout: use the real browser smoke for rendering.
const {JSDOM,VirtualConsole}=require("jsdom");
const assert=require("node:assert/strict");
const fs=require("node:fs"),os=require("node:os"),path=require("node:path");
const {spawn}=require("node:child_process");
const root=path.resolve(__dirname,".."),tmp=fs.mkdtempSync(path.join(os.tmpdir(),"sensor-dom-"));
const port=18766,base="http://127.0.0.1:"+port;
const server=spawn(process.env.TEST_PYTHON||"python3",["-m","uvicorn","termokontur.app:app","--host","127.0.0.1","--port",String(port)],{cwd:root,env:{...process.env,TERMOKONTUR_DB:path.join(tmp,"test.sqlite3"),TERMOKONTUR_SECRET:"temporary-dom-test-secret-at-least-32-characters",TERMOKONTUR_ADMIN_PASSWORD:"dom-test-password"},stdio:"ignore"});
let dom,cookie="";
const errors=[];
async function fetchWithCookies(url,opts={}){
 const response=await fetch(new URL(url,base),{...opts,headers:{...opts.headers,...(cookie?{Cookie:cookie}:{})}});
 const set=response.headers.get("set-cookie");if(set)cookie=set.split(";")[0];return response;
}
async function api(url,body){const response=await fetchWithCookies(url,{method:body===undefined?"GET":"POST",headers:{"Content-Type":"application/json"},body:body===undefined?undefined:JSON.stringify(body)});assert.ok(response.ok,await response.clone().text());return response.json();}
async function until(fn){for(let i=0;i<200;i++){if(fn())return;await new Promise(r=>setTimeout(r,25));}throw Error("DOM state timed out; errors="+errors.join(";")+"; toast="+(dom?.window.document.getElementById("toast")?.textContent||"")+"; detail="+(dom?.window.document.getElementById("sensor-detail-state")?.textContent||""));}
(async()=>{
 try{
  for(let i=0;i<100;i++){try{const h=await fetch(base+"/health");if(h.ok)break;}catch{}await new Promise(r=>setTimeout(r,100));}
  await api("/api/login",{password:"dom-test-password"});
  await api("/api/rooms",{building:"Школа",name:"12"});cookie="";
  const html=await(await fetch(base)).text();const console=new VirtualConsole();console.on("jsdomError",e=>errors.push(String(e)));
  dom=new JSDOM(html,{url:base,runScripts:"outside-only",pretendToBeVisual:true,virtualConsole:console});
  const w=dom.window,d=w.document;w.fetch=fetchWithCookies;w.AbortSignal=AbortSignal;w.confirm=()=>true;w.scrollTo=()=>{};w.matchMedia=()=>({matches:false,addEventListener(){}});
  w.HTMLElement.prototype.scrollIntoView=()=>{};
  const ctx=new Proxy({measureText:()=>({width:50})},{get:(o,k)=>k in o?o[k]:()=>{}});w.HTMLCanvasElement.prototype.getContext=()=>ctx;
  w.addEventListener("error",e=>errors.push(String(e.error)));
  const code=["theme.js","charts.js","reports.js","notifications.js","updates.js","settings.js","max-admin.js","app.js","school-calendar.js","building-accounts.js","sensors.js"].map(f=>fs.readFileSync(path.join(root,"termokontur/static",f),"utf8")).join("\n");w.eval(code);
  await until(()=>!d.getElementById("login-screen").hidden&&!d.getElementById("password").disabled);
  d.getElementById("password").value="dom-test-password";
  d.getElementById("login-form").dispatchEvent(new w.Event("submit",{bubbles:true,cancelable:true}));
  await until(()=>!d.getElementById("app-root").hidden);
  await until(()=>d.getElementById("reading-room").options.length===2);
  assert.equal(d.getElementById("reading-room").options[0].value,"");
  d.querySelector('[data-tab="sensors"]').click();await until(()=>d.getElementById("sensor-state").textContent.includes("Подключите первый датчик"));
  // jsdom has no native modal rendering; implement only dialog open state.
  for(const dialog of d.querySelectorAll("dialog")){
    dialog.showModal=()=>dialog.setAttribute("open","");
    dialog.close=()=>{dialog.removeAttribute("open");dialog.dispatchEvent(new w.Event("close"));};
  }
  d.getElementById("sensor-add").click();d.getElementById("sensor-new-name").value="Датчик <тест>";
  d.getElementById("sensor-add-form").dispatchEvent(new w.Event("submit",{bubbles:true,cancelable:true}));
  await until(()=>d.getElementById("sensor-code-value").value.length>30);
  const id=d.getElementById("sensor-code-id").value,key=d.getElementById("sensor-code-value").value;
  const enrollment=await api("/api/sensors/enroll",{device_id:id,code:key,hardware_id:"0123456789ab",model:"c3-sht40-v1"});
  const payload={model:"c3-sht40-v1",firmware:"0.1.0",next_seq:3,lost:0,rssi:-65,battery_mv:3910,config_version:1,boots:1,records:[{seq:1,boot_id:"0123456789abcdef",uptime_s:1800,measured_at:Math.floor(Date.now()/1000)-1800,time_quality:"rtc",temperature:21.5,humidity:45.0,battery_mv:3910,interval_s:1800,error:""}]};
  const ingested=await fetchWithCookies('/api/sensors/devices/'+id+'/sync',{method:"POST",headers:{"Content-Type":"application/json",Authorization:"Bearer "+enrollment.token},body:JSON.stringify(payload)});assert.ok(ingested.ok,await ingested.clone().text());
  d.getElementById("sensor-code-close").click();d.getElementById("sensor-refresh").click();
  await until(()=>d.querySelector(".sensor-card strong")?.textContent==="Выгрузка не завершена");d.querySelector(".sensor-card button").click();
  await until(()=>d.querySelectorAll("#sensor-records tr").length===1);
  const uploadTimes=d.getElementById("sensor-sync-times");
  assert.deepEqual(Array.from(uploadTimes.querySelectorAll("dt"),item=>item.textContent),["Последняя связь","Полная выгрузка","Последний корректный замер"]);
  assert.notEqual(uploadTimes.querySelector("dd").textContent,"—");
  assert.equal(uploadTimes.querySelectorAll("dd")[1].textContent,"—");
  assert.match(uploadTimes.querySelector("dd:last-child").textContent,/21,5 °C/);
  assert.equal(d.getElementById("sensor-title").textContent,"Датчик <тест>");
  assert.equal(d.getElementById("sensor-title").children.length,0);
  d.querySelector('#sensor-config-form input[name="measure_s"]').value="3600";
  d.getElementById("sensor-config-form").dispatchEvent(new w.Event("submit",{bubbles:true,cancelable:true}));
  await until(()=>d.getElementById("sensor-detail-state").textContent.includes("1 / 2"));
  assert.match(d.getElementById("sensor-sync-notes").textContent,/Настройки ожидают применения/);
  const rekey=await api("/api/sensors/devices/"+id+"/rekey",{});
  const ds=await api("/api/sensors/enroll",{device_id:id,code:rekey.code,hardware_id:"0123456789ab",model:"c3-sht40-v1",sensor_type:"ds18b20"});
  const dsPayload={...payload,sensor_type:"ds18b20",next_seq:3,records:[{...payload.records[0],seq:2,humidity:null}]};
  const dsResult=await fetchWithCookies('/api/sensors/devices/'+id+'/sync',{method:"POST",headers:{"Content-Type":"application/json",Authorization:"Bearer "+ds.token},body:JSON.stringify(dsPayload)});
  assert.ok(dsResult.ok,await dsResult.clone().text());
  d.getElementById("sensor-refresh").click();
  await until(()=>d.getElementById("sensor-detail-state").textContent.includes("DS18B20"));
  assert.equal(d.querySelector(".sensor-card strong").textContent,"Данные получены");
  assert.notEqual(uploadTimes.querySelectorAll("dd")[1].textContent,"—");
  assert.equal(d.querySelectorAll("#sensor-records tr").length,2);
  assert.equal(d.querySelector("#sensor-records tr").children[3].textContent,"—");
  assert.equal(d.querySelector("#sensor-new-model"),null);
  assert.ok(d.querySelector('#view-sensors a[href="/sensors/firmware-debug"]'));
  d.getElementById("sensor-enabled").checked=false;
  d.getElementById("sensor-device-form").dispatchEvent(new w.Event("submit",{bubbles:true,cancelable:true}));
  await until(()=>d.querySelectorAll("#sensor-archive-cards .sensor-card").length===1&&d.querySelectorAll("#sensor-cards .sensor-card").length===0&&d.getElementById("sensor-detail").hidden);
  d.querySelector("#sensor-archive-cards button").click();
  await until(()=>!d.getElementById("sensor-detail").hidden&&d.querySelectorAll("#sensor-records tr").length===2);
  w.termokonturTheme.setPreference("dark");assert.equal(d.documentElement.dataset.theme,"dark");
  d.querySelector('[data-tab="settings"]').click();
  d.querySelector('.settings-nav-button[data-settings-target="calendar"]').click();
  await until(()=>d.querySelectorAll("#calendar-weekly fieldset").length===1);
  d.getElementById("calendar-add").click();
  const [first,last]=d.querySelectorAll("#calendar-exceptions input");
  first.value="2030-01-01";last.value="2030-01-09";
  d.getElementById("calendar-form").dispatchEvent(new w.Event("submit",{bubbles:true,cancelable:true}));
  await until(()=>d.querySelector("#calendar-form [role=status]").textContent==="Календарь сохранён");
  d.querySelector('.settings-nav-button[data-settings-target="access"]').click();
  await until(()=>d.getElementById("building-account-building").options.length===2);
  d.getElementById("building-account-name").value="responsible";
  d.getElementById("building-account-building").value="Школа";
  d.getElementById("building-account-password").value="dom-viewer-password";
  d.getElementById("building-account-form").dispatchEvent(new w.Event("submit",{bubbles:true,cancelable:true}));
  await until(()=>d.getElementById("building-account-list").textContent.includes("responsible"));
  assert.deepEqual(errors,[]);
  process.stdout.write("DOM/API smoke passed: login, creation, code, enrollment, ingest, detail, safe text, config queue and theme event. Layout not tested.\n");
 }finally{if(dom)dom.window.close();server.kill("SIGTERM");fs.rmSync(tmp,{recursive:true,force:true});}
})().catch(e=>{console.error(e);process.exitCode=1;});
