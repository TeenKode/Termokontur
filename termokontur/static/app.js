"use strict";
const $ = (id) => document.getElementById(id);
let rooms = [], overview = [], readings = [], daily = null, selectedTab = "overview", toastTimer;
const sources = {manual:"Вручную",max:"MAX",ha:"Датчик"};
const statuses = {normal:"В норме",below:"Ниже нормы",above:"Выше нормы",missing:"Нет данных"};
const show = (id, yes) => { $(id).hidden = !yes; };
const el = (tag, className = "", value = "") => {
  const item = document.createElement(tag);
  if(className) item.className = className;
  if(value !== null && value !== undefined) item.textContent = String(value);
  return item;
};
function toast(message, isError = false) {
  const target = $("toast");
  target.textContent = message; target.className = "toast show" + (isError ? " error" : "");
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { target.className = "toast"; }, 4200);
}
async function api(path, method = "GET", body) {
  const opts = {method, credentials:"same-origin", headers:{"Accept":"application/json"}};
  if(body !== undefined) { opts.headers["Content-Type"]="application/json"; opts.body=JSON.stringify(body); }
  const response = await fetch(path, opts);
  if(response.status === 401 && path !== "/api/login") { enterLogin(); throw new Error("Сеанс завершился. Войдите снова."); }
  if(!response.ok) {
    let detail = response.statusText;
    try { const error=await response.json(); detail=typeof error.detail === "string" ? error.detail : JSON.stringify(error.detail); } catch {}
    throw new Error(detail || "Ошибка запроса");
  }
  return response.json();
}
function enterLogin() {
  show("login-screen",true); show("app-root",false); $("password").value="";
}
function enterApp() {show("login-screen",false); show("app-root",true);}
function setTab(name) {
  selectedTab = name;
  for(const view of ["overview","rooms","readings"]) {
    show("view-"+view,view===name);
    document.querySelector('[data-tab="'+view+'"]').classList.toggle("active",view===name);
  }
  if(name==="readings") drawChart();
}
function cell(row, text, className = "") { const td=el("td",className); td.textContent=text; row.append(td); return td; }
function when(iso) {
  if(!iso) return "—";
  const date=new Date(iso);
  return Number.isNaN(date.valueOf()) ? "—" : date.toLocaleString("ru-RU",{day:"2-digit",month:"2-digit",hour:"2-digit",minute:"2-digit"});
}
function temperature(value) {return value === null || value === undefined ? "—" : Number(value).toLocaleString("ru-RU",{maximumFractionDigits:1})+" °C";}
function option(select,value,label) {const o=el("option","",label);o.value=value;select.append(o);}
function renderFilters() {
  const oldBuilding=$("building-filter").value, oldReading=$("reading-filter").value;
  for(const id of ["building-filter","reading-filter","reading-room"]) $(id).replaceChildren();
  option($("building-filter"),"","Все корпуса");
  for(const building of [...new Set(rooms.map(r=>r.building))].sort()) option($("building-filter"),building,building);
  $("building-filter").value=oldBuilding;
  option($("reading-filter"),"","Все помещения");
  for(const room of rooms) {
    const label=room.building+" · "+room.name;
    option($("reading-filter"),String(room.id),label);
    option($("reading-room"),String(room.id),label);
  }
  $("reading-filter").value=oldReading;
}
function renderDaily() {
  if(!daily) return;
  $("daily-date").textContent = "На " + daily.local_date + " · " + daily.timezone;
  $("daily-counts").textContent = daily.counts.measured + " из " + daily.rooms.length;
  const missing = daily.rooms.filter(r=>r.daily_status === "missing");
  const area = $("daily-missing"); area.replaceChildren();
  if(!missing.length) {
    area.append(el("p","daily-complete",daily.rooms.length ? "Все помещения сдали показания за сегодня." : "Добавьте кабинеты, чтобы начать контроль."));
    return;
  }
  area.append(el("p","daily-heading","Нет измерений: " + missing.length));
  const list = el("div","daily-tags");
  for(const room of missing) list.append(el("span","daily-tag",room.building + " · " + room.name));
  area.append(list);
}
function renderOverview() {
  const counts={normal:0,below:0,above:0,missing:0};
  for(const item of overview) counts[item.status] = (counts[item.status]||0)+1;
  $("count-total").textContent=overview.length;
  $("count-normal").textContent=counts.normal;
  $("count-alert").textContent=counts.below+counts.above;
  $("count-missing").textContent=counts.missing;
  const search=$("search-rooms").value.toLocaleLowerCase("ru");
  const building=$("building-filter").value;
  const filtered=overview.filter(r=>(!building || r.building===building) &&
    (r.name+" "+r.building+" "+r.kind).toLocaleLowerCase("ru").includes(search));
  const tbody=$("overview-table");tbody.replaceChildren();
  for(const item of filtered){
    const tr=el("tr");cell(tr,item.name);cell(tr,item.building);
    const val=cell(tr,temperature(item.last_temp),"temp"+(item.status==="below"?" low":item.status==="above"?" high":""));
    val.title=item.last_source ? "Источник: "+(sources[item.last_source]||item.last_source) : "";
    cell(tr,Number(item.min_temp).toLocaleString("ru-RU")+"–"+Number(item.max_temp).toLocaleString("ru-RU")+" °C");
    cell(tr,when(item.last_at));
    const statusTd=cell(tr,"");statusTd.append(el("span","status "+item.status,statuses[item.status]||item.status));
    tbody.append(tr);
  }
  show("overview-empty", filtered.length===0);
}
function renderRooms(){
  const tbody=$("rooms-table");tbody.replaceChildren();
  for(const room of rooms) {
    const row=el("tr");cell(row,room.name);cell(row,room.building);
    cell(row,room.floor||"—");cell(row,room.kind);
    cell(row,room.min_temp+"–"+room.max_temp+" °C");
    const actions=cell(row,"");
    const edit=el("button","","Изменить"),pair=el("button","","Код MAX"),disable=el("button","","Отключить");
    edit.addEventListener("click",()=>openRoom(room));pair.addEventListener("click",()=>pairing(room));
    disable.addEventListener("click",()=>disableRoom(room));
    actions.append(edit,pair,disable);tbody.append(row);
  }
  show("rooms-empty",rooms.length===0);
}
function renderReadings(){
  const tbody=$("readings-table");tbody.replaceChildren();
  for(const row of readings) {
    const tr=el("tr");cell(tr,when(row.observed_at));cell(tr,row.building);
    cell(tr,row.room_name);cell(tr,temperature(row.value),"temp");
    cell(tr,sources[row.source]||row.source);tbody.append(tr);
  }
  show("readings-empty",readings.length===0);
  drawChart();
}
function drawChart(){
  const canvas=$("reading-chart"), parent=canvas.parentElement;
  const rectWidth=Math.max(200,parent.clientWidth-46), dpr=window.devicePixelRatio||1;
  const height=200;
  canvas.width=rectWidth*dpr;canvas.height=height*dpr;
  const ctx=canvas.getContext("2d");ctx.setTransform(dpr,0,0,dpr,0,0);
  ctx.clearRect(0,0,rectWidth,height);
  const filter=$("reading-filter").value;
  const data=readings.filter(r=>!filter||String(r.room_id)===filter).slice(0,40).reverse();
  const left=44,right=15,top=18,bottom=30,w=rectWidth-left-right,h=height-top-bottom;
  const values=data.map(x=>Number(x.value));
  const min=values.length?Math.floor(Math.min(...values)-1):16;
  const max=values.length?Math.ceil(Math.max(...values)+1):26;
  const span=Math.max(1,max-min);
  ctx.font="11px system-ui";ctx.textAlign="right";ctx.strokeStyle="#e5ebf1";ctx.fillStyle="#8492a7";ctx.lineWidth=1;
  for(let i=0;i<=4;i++){
    const y=top+h*i/4;ctx.beginPath();ctx.moveTo(left,y);ctx.lineTo(rectWidth-right,y);ctx.stroke();
    ctx.fillText((max-span*i/4).toFixed(1),left-8,y+4);
  }
  if(!data.length){ctx.textAlign="center";ctx.fillText("Нет данных для графика",rectWidth/2,top+h/2);return;}
  const px=i=>left+(data.length===1?w/2:i*w/(data.length-1));
  const py=v=>top+h-(v-min)/span*h;
  ctx.beginPath();data.forEach((x,i)=>{if(i===0)ctx.moveTo(px(i),py(x.value));else ctx.lineTo(px(i),py(x.value));});
  ctx.strokeStyle="#14a786";ctx.lineWidth=2.5;ctx.stroke();
  data.forEach((x,i)=>{ctx.beginPath();ctx.arc(px(i),py(x.value),3,0,Math.PI*2);ctx.fillStyle="#14a786";ctx.fill();});
  ctx.fillStyle="#8492a7";ctx.textAlign="center";
  ctx.fillText(when(data[0].observed_at),left+55,height-8);
  if(data.length>1)ctx.fillText(when(data[data.length-1].observed_at),rectWidth-right-55,height-8);
}
async function refresh() {
  try {
    const selected=$("reading-filter").value;
    const params=selected?"?room_id="+encodeURIComponent(selected)+"&limit=200":"?limit=200";
    const [dash,allRooms,items,day]=await Promise.all([api("/api/dashboard"),api("/api/rooms"),api("/api/readings"+params),api("/api/daily")]);
    overview=dash.rooms;rooms=allRooms;readings=items;daily=day;
    renderFilters();renderOverview();renderDaily();renderRooms();renderReadings();
    $("updated-at").textContent="Обновлено: "+when(dash.generated_at);
  } catch(error) {toast(error.message,true);}
}
async function refreshReadings(){
  try{const room=$("reading-filter").value;readings=await api("/api/readings?limit=200"+(room?"&room_id="+encodeURIComponent(room):""));renderReadings();}
  catch(error){toast(error.message,true);}
}
function openRoom(room=null){
  $("room-form").reset();
  $("room-dialog-title").textContent=room?"Изменить помещение":"Новое помещение";
  $("edit-room-id").value=room?room.id:"";
  if(room){for(const [name,value] of [["building",room.building],["name",room.name],["floor",room.floor],["kind",room.kind],["min",room.min_temp],["max",room.max_temp]]) $("room-"+name).value=value;}
  $("room-dialog").showModal();
}
async function pairing(room){
  try {
    const reply=await api("/api/rooms/"+room.id+"/pairing","POST");
    $("pairing-code").textContent=reply.code;
    $("pairing-dialog").showModal();
  } catch(error){toast(error.message,true);}
}
async function disableRoom(room) {
  if(!confirm("Отключить «"+room.name+"»? История измерений сохранится."))return;
  try {await api("/api/rooms/"+room.id,"PATCH",{active:false});await refresh();toast("Помещение отключено");}
  catch(error){toast(error.message,true);}
}
$("login-form").addEventListener("submit",async event=>{
  event.preventDefault();$("login-error").textContent="";
  try{await api("/api/login","POST",{password:$("password").value});enterApp();await refresh();}
  catch(error){$("login-error").textContent=error.message;}
});
$("logout").addEventListener("click",async()=>{try{await api("/api/logout","POST");}catch{}enterLogin();});
$("refresh").addEventListener("click",refresh);
for(const nav of document.querySelectorAll("[data-tab]"))nav.addEventListener("click",()=>{setTab(nav.dataset.tab);if(nav.dataset.tab==="readings")refreshReadings();});
$("search-rooms").addEventListener("input",renderOverview);
$("building-filter").addEventListener("change",renderOverview);
$("reading-filter").addEventListener("change",refreshReadings);
$("quick-reading").addEventListener("click",()=>{$("reading-form").reset();$("reading-dialog").showModal();});
$("add-reading").addEventListener("click",()=>{$("reading-form").reset();$("reading-dialog").showModal();});
$("add-room").addEventListener("click",()=>openRoom());
$("export-csv").addEventListener("click",()=>{window.location.href="/api/export.csv";});
$("close-pairing").addEventListener("click",()=>$("pairing-dialog").close());
$("copy-pairing").addEventListener("click",async()=>{
  try{await navigator.clipboard.writeText("/bind "+$("pairing-code").textContent);toast("Команда скопирована");}
  catch{toast("Скопируйте команду вручную",true);}
});
$("room-form").addEventListener("submit",async event=>{
  if(event.submitter?.value==="cancel")return;
  event.preventDefault();
  const roomId=$("edit-room-id").value;
  const data={building:$("room-building").value.trim(),floor:$("room-floor").value.trim(),
    name:$("room-name").value.trim(),kind:$("room-kind").value.trim(),
    min_temp:Number($("room-min").value),max_temp:Number($("room-max").value)};
  if(data.min_temp>=data.max_temp){toast("Минимум должен быть ниже максимума",true);return;}
  try{await api(roomId?"/api/rooms/"+roomId:"/api/rooms",roomId?"PATCH":"POST",data);
    $("room-dialog").close();await refresh();toast("Помещение сохранено");}
  catch(error){toast(error.message,true);}
});
$("reading-form").addEventListener("submit",async event=>{
  if(event.submitter?.value==="cancel")return;
  event.preventDefault();
  try{await api("/api/readings","POST",{room_id:Number($("reading-room").value),value:Number($("reading-value").value)});
    $("reading-dialog").close();await refresh();toast("Показание записано");}
  catch(error){toast(error.message,true);}
});
window.addEventListener("resize",()=>{if(selectedTab==="readings")drawChart();});
(async function boot(){try{const result=await api("/api/session");if(result.authenticated){enterApp();await refresh();}else enterLogin();}catch{enterLogin();}})();
