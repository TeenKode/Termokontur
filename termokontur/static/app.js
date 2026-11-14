"use strict";
const $ = (id) => document.getElementById(id);
let rooms = [], allRooms = [], overview = [], readings = [], daily = null, settingsCache = null, selectedTab = "overview", toastTimer, refreshBusy = false;
const selectedMaxRooms = new Set();
let activeMaxCodes = [], issuedMaxCodes = [];
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
  target.textContent = message; target.className = "toast show" + (isError ? " error" : " success");
  target.setAttribute("role",isError?"alert":"status");
  target.setAttribute("aria-live",isError?"assertive":"polite");
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { target.className = "toast"; }, 4200);
}
/* Unified operation feedback. Background polling stays silent; user actions
   show an in-button spinner and a polite, accessible global progress message. */
let interactionButton=null, interactionAt=0, foregroundCount=0, networkFailed=false;
const buttonOperations=new WeakMap();
let operationRegion=null;
function operationStatus(message,kind="working"){
  if(!operationRegion){
    operationRegion=document.createElement("div");
    operationRegion.id="operation-status";
    operationRegion.className="operation-status";
    operationRegion.setAttribute("role","status");
    operationRegion.setAttribute("aria-live","polite");
    operationRegion.hidden=true;
    const topbar=document.querySelector(".topbar");
    (topbar?.parentNode||document.body).insertBefore(operationRegion,topbar?.nextSibling||null);
  }
  operationRegion.textContent=message;
  operationRegion.dataset.kind=kind;
  operationRegion.hidden=!message;
}
document.addEventListener("click",event=>{
  const button=event.target.closest("button");
  if(button){interactionButton=button;interactionAt=Date.now();}
},true);
document.addEventListener("submit",event=>{
  const button=event.submitter||event.target.querySelector('button[type="submit"]');
  if(button){interactionButton=button;interactionAt=Date.now();}
},true);
function beginRequest(path,method){
  const userAction=Date.now()-interactionAt<1200 && interactionButton?.isConnected;
  if(!userAction)return ()=>{};
  const button=interactionButton;
  const existing=buttonOperations.get(button);
  if(existing) {
    existing.count++;
  }else{
    buttonOperations.set(button,{count:1,disabled:button.disabled});
    button.classList.add("operation-busy");
    button.setAttribute("aria-busy","true");
    button.disabled=true;
  }
  foregroundCount++;
  const label=method==="GET"?"Загружаем данные…":method==="DELETE"?"Удаляем…":"Сохраняем изменения…";
  operationStatus(label);
  return ()=>{
    const current=buttonOperations.get(button);
    if(current&&--current.count===0){
      button.classList.remove("operation-busy");
      button.removeAttribute("aria-busy");
      button.disabled=current.disabled;
      buttonOperations.delete(button);
    }
    foregroundCount=Math.max(0,foregroundCount-1);
    if(!foregroundCount&&!networkFailed)operationStatus("");
  };
}
window.addEventListener("offline",()=>{
  networkFailed=true;operationStatus("Нет подключения к сети. Проверьте соединение и повторите действие.","error");
});
window.addEventListener("online",()=>{
  networkFailed=false;
  operationStatus(foregroundCount?"Восстанавливаем соединение…":"","working");
});
async function api(path, method = "GET", body) {
  const finish=beginRequest(path,method);
  try{
    const opts = {method, credentials:"same-origin", headers:{"Accept":"application/json"}};
    if(path.startsWith("/api/update/"))opts.cache="no-store";
    if(body !== undefined) { opts.headers["Content-Type"]="application/json"; opts.body=JSON.stringify(body); }
    let response;
    try {response=await fetch(path, opts);}
    catch(error){
      networkFailed=true;
      operationStatus("Не удалось связаться с сервером. Проверьте подключение и повторите действие.","error");
      throw new Error("Нет связи с сервером. Проверьте сеть и повторите действие.");
    }
    if(networkFailed){networkFailed=false;operationStatus("");}
    if(response.status === 401 && path !== "/api/login") { enterLogin(); throw new Error("Сеанс завершился. Войдите снова."); }
    if(!response.ok) {
      let detail = response.statusText;
      try { const error=await response.json(); detail=typeof error.detail === "string" ? error.detail : JSON.stringify(error.detail); } catch {}
      throw new Error(detail || "Ошибка запроса");
    }
    return response.json();
  } finally {finish();}
}
function enterLogin() {
  show("login-screen",true); show("app-root",false); $("password").value="";
}
function enterApp() {show("login-screen",false); show("app-root",true);}
function setTab(name) {
  selectedTab = name;
  for(const view of ["overview","rooms","readings","settings"]) {
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
function maxUserName(user){
  return [user.first_name,user.last_name].filter(Boolean).join(" ") ||
    (user.username ? "@"+user.username : "MAX ID "+user.user_id);
}
function renderRooms(){
  const tbody=$("rooms-table");tbody.replaceChildren();
  const search=$("rooms-search").value.trim().toLocaleLowerCase("ru");
  const showInactive=$("show-inactive").checked;
  const matching=allRooms.filter(r=>(showInactive || r.active) &&
    [r.name,r.building,r.floor,r.kind,...(r.max_users||[]).flatMap(u=>[u.first_name,u.last_name,u.username,u.user_id])]
      .some(x=>String(x).toLocaleLowerCase("ru").includes(search)));
  for(const room of matching) {
    const row=el("tr", room.active ? "" : "archived-room");
    cell(row,room.name);
    const building=cell(row,room.building);
    if(!room.active)building.append(el("span","archived-label","Отключён"));
    cell(row,room.floor||"—");cell(row,room.kind);
    cell(row,room.min_temp+"–"+room.max_temp+" °C");
    const maxCell=cell(row,"","max-users-cell");
    const boundUsers=room.max_users||[];
    if(!boundUsers.length)maxCell.append(el("span","max-unbound","Не привязан"));
    else{
      const list=el("div","max-user-list");
      for(const user of boundUsers){
        const entry=el("div","max-user-entry"),details=el("div","max-user-details");
        details.append(el("strong","",maxUserName(user)));
        details.append(el("small","",[
          user.username?"@"+user.username:"",
          "ID "+user.user_id
        ].filter(Boolean).join(" · ")));
        const remove=el("button","max-unbind","Отвязать");
        remove.type="button";
        remove.title="Отвязать пользователя "+maxUserName(user)+" от "+room.name;
        remove.addEventListener("click",()=>unbindMax(room,user));
        entry.append(details,remove);list.append(entry);
      }
      maxCell.append(list);
    }
    const actions=cell(row,"");
    const duplicate=el("button","","Копировать");
    duplicate.title="Создать новый кабинет с теми же параметрами";
    duplicate.addEventListener("click",()=>openRoom(room,true));
    actions.append(duplicate);
    if(room.active){
      const edit=el("button","","Изменить"),pair=el("button","","Код MAX"),disable=el("button","","Отключить");
      edit.addEventListener("click",()=>openRoom(room));
      pair.addEventListener("click",()=>pairing(room));
      disable.addEventListener("click",()=>disableRoom(room));
      actions.prepend(edit);actions.append(pair,disable);
    }else{
      const restore=el("button","","Восстановить");
      restore.addEventListener("click",()=>restoreRoom(room));
      actions.append(restore);
    }
    tbody.append(row);
  }
  show("rooms-empty",matching.length===0);
  if(!matching.length)$("rooms-empty").textContent=allRooms.length?"По заданному фильтру помещений нет.":"Добавьте первое помещение.";
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
  if(refreshBusy)return;
  refreshBusy=true;
  try {
    const selected=$("reading-filter").value;
    const params=selected?"?room_id="+encodeURIComponent(selected)+"&limit=200":"?limit=200";
    const [dash,activeRooms,items,day,all]=await Promise.all([api("/api/dashboard"),api("/api/rooms"),api("/api/readings"+params),api("/api/daily"),api("/api/rooms?include_inactive=true")]);
    overview=dash.rooms;rooms=activeRooms;allRooms=all;readings=items;daily=day;
    renderFilters();renderOverview();renderDaily();renderRooms();renderReadings();
    $("updated-at").textContent="Обновлено: "+when(dash.generated_at);
  } catch(error) {toast(error.message,true);}
  finally {refreshBusy=false;}
}
async function refreshReadings(){
  try{const room=$("reading-filter").value;readings=await api("/api/readings?limit=200"+(room?"&room_id="+encodeURIComponent(room):""));renderReadings();}
  catch(error){toast(error.message,true);}
}
function openRoom(room=null, duplicate=false){
  $("room-form").reset();
  $("room-dialog-title").textContent=duplicate?"Копировать помещение":room?"Изменить помещение":"Новое помещение";
  $("edit-room-id").value=room && !duplicate ? room.id : "";
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
async function unbindMax(room,user){
  if(!confirm("Отвязать «"+maxUserName(user)+"» от помещения «"+room.name+"»? После отвязки сотрудник не сможет отправлять показания для этого кабинета."))return;
  try{
    await api("/api/rooms/"+room.id+"/max-bindings/"+encodeURIComponent(user.user_id),"DELETE");
    await refresh();
    toast("Профиль MAX отвязан от помещения");
  }catch(error){toast(error.message,true);}
}
async function restoreRoom(room){
  try{
    await api("/api/rooms/"+room.id,"PATCH",{active:true});
    await refresh();toast("Помещение восстановлено, история измерений сохранена");
  }catch(error){toast(error.message,true);}
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
for(const nav of document.querySelectorAll("[data-tab]"))nav.addEventListener("click",()=>{setTab(nav.dataset.tab);if(nav.dataset.tab==="readings")refreshReadings();if(nav.dataset.tab==="settings"&&!settingsDirty())loadSettings();});
$("search-rooms").addEventListener("input",renderOverview);
$("building-filter").addEventListener("change",renderOverview);
$("reading-filter").addEventListener("change",refreshReadings);
$("quick-reading").addEventListener("click",()=>{$("reading-form").reset();$("reading-dialog").showModal();});
$("add-reading").addEventListener("click",()=>{$("reading-form").reset();$("reading-dialog").showModal();});
$("add-room").addEventListener("click",()=>openRoom());
$("rooms-search").addEventListener("input",renderRooms);
$("show-inactive").addEventListener("change",renderRooms);
$("export-rooms").addEventListener("click",()=>{window.location.href="/api/rooms/export.csv";});
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

/* Accessible settings workspace: categories, overview and independent saves. */
const settingsGroups = {
  general:["general-settings-form"],notifications:["reminders-settings-form","alerts-settings-form"],
  backups:["maintenance-form"]
};
let selectedSettingsCategory="summary",settingsLoaded=false;
function settingsDirty(){
  return !!document.querySelector('.settings-pane form[data-dirty="true"]');
}
function updateDirtyNavigation(){
  for(const [category,forms] of Object.entries(settingsGroups)){
    const dirty=forms.some(id=>$(id).dataset.dirty==="true");
    for(const button of document.querySelectorAll('.settings-nav-button[data-settings-target="'+category+'"]'))
      button.classList.toggle("is-dirty",dirty);
  }
}
function markSettingsSaved(form,message){
  form.dataset.dirty="false";
  const feedback=form.querySelector(".settings-save-feedback");
  if(feedback){feedback.textContent=message;feedback.dataset.state="saved";}
  updateDirtyNavigation();
}
function markSettingsDirty(form){
  form.dataset.dirty="true";
  const feedback=form.querySelector(".settings-save-feedback");
  if(feedback){feedback.textContent="Есть несохранённые изменения";feedback.dataset.state="unsaved";}
  updateDirtyNavigation();
}
for(const formIds of Object.values(settingsGroups))for(const id of formIds){
  const form=$(id);
  for(const type of ["input","change"])form.addEventListener(type,()=>markSettingsDirty(form));
}
function setSettingsCategory(category){
  if(!document.querySelector('.settings-pane[data-settings-view="'+category+'"]'))return;
  selectedSettingsCategory=category;
  for(const pane of document.querySelectorAll("[data-settings-view]"))
    pane.hidden=pane.dataset.settingsView!==category;
  for(const button of document.querySelectorAll(".settings-nav-button")){
    const active=button.dataset.settingsTarget===category;
    button.classList.toggle("active",active);
    if(active)button.setAttribute("aria-current","page");
    else button.removeAttribute("aria-current");
  }
  if(category==="backups")void loadBackups();
  if(category==="updates")void loadUpdateStatus();
  if(category==="max")void loadMaxDiagnostics();
  if(window.matchMedia("(max-width:900px)").matches){
    document.querySelector(".settings-content").scrollIntoView({
      block:"start",behavior:window.matchMedia("(prefers-reduced-motion:reduce)").matches?"auto":"smooth"
    });
  }
}
for(const button of document.querySelectorAll("[data-settings-target]")){
  button.addEventListener("click",()=>setSettingsCategory(button.dataset.settingsTarget));
}
function renderSettingsSummary(){
  if(!settingsCache)return;
  $("settings-summary-org").textContent=settingsCache.organization_name;
  $("settings-summary-timezone").textContent=settingsCache.timezone+" · "+settingsCache.stale_hours+" ч до устаревания";
  $("settings-summary-max").textContent=settingsCache.max_token_configured?"Подключён":"Не настроен";
  $("settings-summary-max-mode").textContent=settingsCache.max_token_configured
    ?(settingsCache.max_mode==="webhook"?"Webhook (устаревшая настройка)":"Long Polling")
    :"Токен MAX отсутствует";
  $("settings-summary-sensors").textContent=settingsCache.ingest_key_configured?"Ключ настроен":"Ключ отсутствует";
  $("settings-summary-backup").textContent=settingsCache.backup_interval_days
    ?"Автокопия каждые "+settingsCache.backup_interval_days+" дн.":"Автокопии выключены";
  $("settings-summary-reminders").textContent=settingsCache.reminders_enabled?"Включены":"Выключены";
  $("settings-summary-alerts").textContent=settingsCache.alerts_enabled
    ?"Предупреждения о температуре включены":"Предупреждения о температуре выключены";
}
function syncNotificationControls(){
  for(const [container,toggle] of [["reminder-options","setting-enabled"],["alert-options","setting-alerts-enabled"]]){
    const inactive=!$(toggle).checked;
    $(container).classList.toggle("is-inactive",inactive);
    for(const field of $(container).querySelectorAll("input,select,textarea"))
      field.disabled=inactive;
  }
}
$("setting-enabled").addEventListener("change",syncNotificationControls);
$("setting-alerts-enabled").addEventListener("change",syncNotificationControls);
async function loadSettings() {
  try {
    const pending=new Map();
    for(const id of Object.values(settingsGroups).flat()){
      const form=$(id);
      if(form.dataset.dirty==="true")pending.set(id,
        [...form.querySelectorAll("input,select,textarea")].map(input=>({value:input.value,checked:input.checked})));
    }
    settingsCache=await api("/api/settings");
    $("setting-organization").value=settingsCache.organization_name;
    $("setting-timezone").value=settingsCache.timezone;
    $("setting-stale").value=settingsCache.stale_hours;
    $("setting-enabled").checked=settingsCache.reminders_enabled;
    $("setting-time").value=settingsCache.reminder_time;
    $("setting-window").value=settingsCache.reminder_window_minutes;
    $("setting-backup-interval").value=settingsCache.backup_interval_days;
    $("setting-backup-keep").value=settingsCache.backup_keep;
    $("setting-retention").value=settingsCache.retention_days;
    $("setting-alerts-enabled").checked=settingsCache.alerts_enabled;
    $("setting-alert-cooldown").value=settingsCache.alert_cooldown_minutes;
    for(const checkbox of document.querySelectorAll('[name="remind-day"]'))
      checkbox.checked=settingsCache.reminder_days.includes(Number(checkbox.value));
    $("max-status").textContent=settingsCache.max_token_configured?"Токен MAX настроен":"Токен MAX отсутствует";
    $("max-webhook-mode").textContent=settingsCache.max_mode==="webhook"?"Обнаружен прежний режим webhook. Для получения сообщений через Long Polling отключите webhook кнопкой ниже.":"Long Polling включён. Публичный IP не требуется; служба MAX получает сообщения исходящими запросами.";
    $("switch-polling").hidden=settingsCache.max_mode!=="webhook";
    $("ingest-status").textContent=settingsCache.ingest_key_configured?"Ключ датчиков настроен":"Нет ключа для датчиков";
    document.title=settingsCache.organization_name+" — Термоконтур";
    settingsLoaded=true;
    for(const id of Object.values(settingsGroups).flat()){
      const form=$(id),values=pending.get(id);
      if(values){
        [...form.querySelectorAll("input,select,textarea")].forEach((input,i)=>{
          input.value=values[i].value;
          if(input.type==="checkbox"||input.type==="radio")input.checked=values[i].checked;
        });
      }else form.dataset.dirty="false";
    }
    syncNotificationControls();
    renderSettingsSummary();
    updateDirtyNavigation();
    await Promise.all([loadUpdateStatus(),loadBackups()]);
    if(selectedSettingsCategory==="max")void loadMaxDiagnostics();
  } catch(error){toast(error.message,true);}
}
/* Merge only the current form's fields into fresh settings from the server.
   Other categories and secrets are not inadvertently overwritten. */
async function saveSettingsPatch(form,patch,message){
  const button=form.querySelector('button[type="submit"]');
  if(button.disabled)return;
  button.disabled=true;
  const originalText=button.textContent;
  button.textContent="Сохраняем…";
  try{
    const latest=await api("/api/settings");
    const data={...latest,...patch};
    if(data.retention_days>0 && data.retention_days<30)
      throw Error("Срок хранения: минимум 30 дней или 0");
    if(!Array.isArray(data.reminder_days)||data.reminder_days.length===0)
      throw Error("Выберите хотя бы один день для расписания");
    settingsCache=await api("/api/settings","PUT",data);
    renderSettingsSummary();
    document.title=settingsCache.organization_name+" — Термоконтур";
    markSettingsSaved(form,message);
    toast(message);
    return true;
  }catch(error){
    const feedback=form.querySelector(".settings-save-feedback");
    if(feedback){feedback.textContent="Не сохранено: "+error.message;feedback.dataset.state="error";}
    toast(error.message,true);
    return false;
  }finally{
    button.disabled=false;
    button.textContent=originalText;
  }
}
$("general-settings-form").addEventListener("submit",async event=>{
  event.preventDefault();
  const saved=await saveSettingsPatch(event.currentTarget,{
    organization_name:$("setting-organization").value.trim(),
    timezone:$("setting-timezone").value.trim(),
    stale_hours:Number($("setting-stale").value)
  },"Параметры организации сохранены");
  if(saved)await refresh();
});
$("reminders-settings-form").addEventListener("submit",async event=>{
  event.preventDefault();
  const days=[...document.querySelectorAll('[name="remind-day"]:checked')].map(x=>Number(x.value));
  await saveSettingsPatch(event.currentTarget,{
    reminders_enabled:$("setting-enabled").checked,
    reminder_time:$("setting-time").value,
    reminder_days:days.length?days:($("setting-enabled").checked?[]:settingsCache.reminder_days),
    reminder_window_minutes:Number($("setting-window").value)
  },"Расписание напоминаний сохранено");
});
$("alerts-settings-form").addEventListener("submit",async event=>{
  event.preventDefault();
  await saveSettingsPatch(event.currentTarget,{
    alerts_enabled:$("setting-alerts-enabled").checked,
    alert_cooldown_minutes:Number($("setting-alert-cooldown").value)
  },"Температурные уведомления сохранены");
});
$("max-settings-form").addEventListener("submit",async event=>{
  event.preventDefault();
  const value=$("setting-max-token").value.trim();
  if(!value){toast("Введите токен",true);return;}
  try{await api("/api/settings/max-token","PUT",{value});$("setting-max-token").value="";await loadSettings();toast("Токен сохранён");}
  catch(error){toast(error.message,true);}
});
$("clear-max-token").addEventListener("click",async()=>{
  if(!confirm("Отключить MAX-бота? Привязки кабинетов останутся."))return;
  try{await api("/api/settings/max-token","PUT",{value:null});await loadSettings();toast("MAX отключён");}
  catch(error){toast(error.message,true);}
});
$("rotate-ingest").addEventListener("click",async()=>{
  if(!confirm("Заменить ключ? Старый перестанет работать во всех датчиках."))return;
  try{
    const r=await api("/api/settings/ingest-key","POST");
    $("ingest-value").textContent=r.key;show("ingest-reveal",true);
    await loadSettings();toast("Новый ключ создан. Скопируйте его.");
  }catch(error){toast(error.message,true);}
});
$("copy-ingest").addEventListener("click",async()=>{
  try{await navigator.clipboard.writeText($("ingest-value").textContent);toast("Ключ скопирован");}
  catch{toast("Скопируйте ключ вручную",true);}
});
// Password visibility is client-side only; password values are not logged.
for(const button of document.querySelectorAll("[data-password-target]")){
  button.addEventListener("click",()=>{
    const field=$(button.dataset.passwordTarget);
    if(!field)return;
    const visible=field.type==="password";
    field.type=visible?"text":"password";
    button.textContent=visible?"Скрыть":"Показать";
    button.setAttribute("aria-pressed",String(visible));
  });
}
function passwordFeedback(){
  const value=$("new-password").value,confirmValue=$("confirm-password").value;
  const hint=$("password-feedback");
  hint.classList.remove("valid","invalid");
  if(!value){hint.textContent="Минимум 8 символов. Спецсимволы не обязательны.";return;}
  if(value.length<8){
    hint.textContent="Недостаточная длина: "+value.length+" из 8 символов";
    hint.classList.add("invalid");
  }else if(confirmValue && value!==confirmValue){
    hint.textContent="Пароли не совпадают";
    hint.classList.add("invalid");
  }else if(confirmValue){
    hint.textContent="Пароль подходит, значения совпадают";
    hint.classList.add("valid");
  }else{
    hint.textContent="Длина подходит. Повторите пароль ниже.";
    hint.classList.add("valid");
  }
}
$("new-password").addEventListener("input",passwordFeedback);
$("confirm-password").addEventListener("input",passwordFeedback);
$("password-settings-form").addEventListener("submit",async event=>{
  event.preventDefault();
  const old=$("old-password").value,pass=$("new-password").value;
  if(pass!==$("confirm-password").value){toast("Новые пароли не совпадают",true);return;}
  try{
    await api("/api/settings/password","POST",{current_password:old,new_password:pass});
    $("password-settings-form").reset();
    passwordFeedback();
    enterLogin();
    $("login-error").textContent="Пароль изменён. Войдите с новым паролем.";
  }catch(error){toast(error.message,true);}
});
/* Paste ranges (101-120), commas, lines or spreadsheet cells. */
function bulkNames(){
  const prefix=$("bulk-prefix").value;
  const tokens=$("bulk-list").value.split(/[\n\r\t,;]+/).map(x=>x.trim()).filter(Boolean);
  const result=[];
  for(const raw of tokens){
    const range=raw.match(/^(.*?)(\d{1,5})\s*[-–]\s*(\d{1,5})$/);
    if(range){
      const start=Number(range[2]),end=Number(range[3]);
      if(end<start||end-start>249)throw Error("Неверный диапазон: "+raw);
      for(let i=start;i<=end;i++)result.push((range[1].trim()?range[1]:prefix)+String(i).padStart(range[2].length,"0"));
    } else result.push(/^\d{1,5}$/.test(raw)?prefix+raw:raw);
    if(result.length>250)throw Error("Максимум 250 помещений за один раз");
  }
  if(new Set(result).size!==result.length)throw Error("Есть повторяющиеся названия");
  if(result.some(x=>x.length>100))throw Error("Название слишком длинное");
  return result;
}
function previewBulk(){
  try{
    const names=bulkNames();
    $("bulk-preview").textContent=names.length?"Будет добавлено: "+names.length+" · "+names.slice(0,5).join(", ")+(names.length>5?"…":""):"Укажите кабинеты";
    $("bulk-preview").classList.remove("invalid");
  }catch(error){$("bulk-preview").textContent=error.message;$("bulk-preview").classList.add("invalid");}
}
$("bulk-rooms").addEventListener("click",()=>{
  $("bulk-form").reset();
  $("building-options").replaceChildren();
  for(const building of [...new Set(rooms.map(r=>r.building))])option($("building-options"),building,building);
  $("bulk-dialog").showModal();previewBulk();$("bulk-list").focus();
});
$("close-bulk").addEventListener("click",()=>$("bulk-dialog").close());
$("cancel-bulk").addEventListener("click",()=>$("bulk-dialog").close());
for(const id of ["bulk-list","bulk-prefix"])$(id).addEventListener("input",previewBulk);
$("bulk-form").addEventListener("submit",async event=>{
  event.preventDefault();
  try{
    const names=bulkNames();
    if(!names.length)throw Error("Укажите кабинеты");
    const r=await api("/api/rooms/bulk","POST",{
      building:$("bulk-building").value.trim(),
      floor:$("bulk-floor").value.trim(),
      kind:$("bulk-kind").value.trim(),
      min_temp:Number($("bulk-min").value),
      max_temp:Number($("bulk-max").value),
      names
    });
    $("bulk-dialog").close();await refresh();
    toast("Добавлено: "+r.created+"; уже существовали: "+r.skipped.length);
  }catch(error){toast(error.message,true);}
});
/* Version and update stage are server-side state, so they survive a panel reload. */
let updateAvailable=false, updateLatestRevision=null, updateLatestVersion=null;
let updateInstalledRevision=null, updateState="idle", updateChecking=false;
let updateRequesting=false, updatePolling=false, updateStartedHere=false;
const updatePhases=[
  ["preparing","Подготовка"],["downloading","Загрузка исходного кода"],
  ["dependencies","Установка зависимостей"],["backup","Резервная копия"],
  ["switching","Переключение версии"],["verifying","Проверка запуска"],
  ["finishing","Завершение"]
];
const updateStateLabels={
  idle:"Готово к проверке",queued:"Обновление в очереди",
  installing:"Выполняется обновление",success:"Обновление завершено",
  failed:"Ошибка обновления"
};
function renderUpdateStatus(r){
  const state=r.state||"idle", active=state==="queued"||state==="installing";
  updateState=state;
  if(!updateInstalledRevision&&r.installed_revision)updateInstalledRevision=r.installed_revision;
  if(Object.prototype.hasOwnProperty.call(r,"available")){
    updateAvailable=r.available===true;
  }
  if(r.latest_revision)updateLatestRevision=r.latest_revision;
  if(r.latest_version)updateLatestVersion=r.latest_version;
  if(updateLatestRevision&&r.installed_revision===updateLatestRevision)updateAvailable=false;
  const version=r.installed_version||r.version;
  $("update-current-version").textContent=version?"v"+version:"Версия неизвестна";
  $("settings-summary-version").textContent=version?"v"+version:"Версия неизвестна";
  $("settings-summary-build").textContent=r.installed_revision?"Сборка "+r.installed_revision.slice(0,12):"Сборка неизвестна";
  $("update-current-build").textContent=r.installed_revision?"Сборка "+r.installed_revision.slice(0,12):"Номер сборки неизвестен";
  if(updateLatestRevision){
    $("update-latest-version").textContent=updateLatestVersion?"v"+updateLatestVersion:"Сборка GitHub";
    $("update-latest-build").textContent=updateLatestRevision.slice(0,12)+
      (updateAvailable?" · доступна новая сборка":" · установлена");
  }
  $("update-state").dataset.state=state;
  $("update-state-label").textContent=updateStateLabels[state]||"Состояние обновления";
  $("update-state-symbol").textContent=({idle:"○",queued:"◷",installing:"↻",success:"✓",failed:"!"})[state]||"○";
  $("update-status-text").textContent=r.message||"Ожидаем сведения о системе.";
  $("update-status-time").textContent=r.updated_at?"Последнее событие: "+when(r.updated_at):"";
  const stepIndex=updatePhases.findIndex(([phase])=>phase===r.phase);
  const step=state==="queued"?0:Math.max(1,stepIndex+1);
  show("update-progress",active);
  $("update-step-label").textContent=state==="queued"?"Ожидаем запуска":stepIndex>=0?updatePhases[stepIndex][1]:"Подготавливаем обновление";
  $("update-step-count").textContent=state==="queued"?"В очереди":"Этап "+step+" из "+updatePhases.length;
  const track=$("update-progress").querySelector(".update-track");
  track.setAttribute("aria-valuenow",String(step));
  track.setAttribute("aria-valuetext",$("update-step-label").textContent);
  $("update-progress-fill").style.width=(step/updatePhases.length*100)+"%";
  const busy=active||updateRequesting;
  $("check-update").disabled=!r.supported||busy||updateChecking;
  $("install-update").disabled=!r.supported||!updateAvailable||busy||updateChecking;
  $("check-update").classList.toggle("busy",updateChecking);
  $("install-update").classList.toggle("busy",updateRequesting);
  $("check-update").textContent=updateChecking?"Проверяем…":"Проверить обновления";
  $("install-update").textContent=updateRequesting?"Отправляем запрос…":active?"Выполняется обновление…":"Установить обновление";
  show("reload-after-update",state==="success"&&updateStartedHere);
}
async function loadUpdateStatus(){
  if(updatePolling||updateChecking)return;
  updatePolling=true;
  try{
    const r=await api("/api/update/status");
    if(r.state==="success"&&updateInstalledRevision&&r.installed_revision!==updateInstalledRevision){
      updateAvailable=false;
    }
    renderUpdateStatus(r);
  }catch(error){
    $("update-status-text").textContent=
      ["queued","installing"].includes(updateState)
        ?"Связь с сервером временно потеряна. Продолжаем проверять состояние…"
        :"Не удалось получить состояние обновления: "+error.message;
  }finally{updatePolling=false;}
}
$("check-update").addEventListener("click",async()=>{
  if(updateChecking||updateRequesting||["queued","installing"].includes(updateState))return;
  updateChecking=true;
  updateAvailable=false;
  $("update-state-label").textContent="Проверяем GitHub";
  $("update-status-text").textContent="Получаем сведения о последней сборке…";
  $("check-update").disabled=true;
  $("install-update").disabled=true;
  $("check-update").classList.add("busy");
  $("check-update").textContent="Проверяем…";
  try{
    const r=await api("/api/update/check");
    renderUpdateStatus(r);
  }catch(error){
    $("update-state-label").textContent="Проверка не удалась";
    $("update-status-text").textContent=error.message;
    toast(error.message,true);
  }finally{
    updateChecking=false;
    await loadUpdateStatus();
  }
});
$("install-update").addEventListener("click",async()=>{
  if(!updateAvailable||updateRequesting||["queued","installing"].includes(updateState))return;
  if(!confirm("Установить обновление с GitHub? Панель временно перезапустится; данные будут сохранены."))return;
  updateRequesting=true;
  updateStartedHere=true;
  updateAvailable=false;
  renderUpdateStatus({supported:true,installed_version:$("update-current-version").textContent.replace(/^v/,""),
    installed_revision:updateInstalledRevision,state:"queued",phase:"preparing",
    message:"Отправляем запрос системной службе обновления…"});
  try{
    await api("/api/update/request","POST",{});
    await loadUpdateStatus();
  }catch(error){
    // Server restart can interrupt the POST after the request was accepted.
    $("update-status-text").textContent="Не удалось подтвердить запрос. Проверяем сервер: "+error.message;
  }finally{
    updateRequesting=false;
    await loadUpdateStatus();
  }
});
$("reload-after-update").addEventListener("click",()=>window.location.reload());
setInterval(()=>{
  if(!$("app-root").hidden&&(selectedTab==="settings"||
      updateState==="queued"||updateState==="installing"||updateRequesting)){
    void loadUpdateStatus();
  }
},3000);

/* MAX code management: safe metadata and one-time secret reveal. */
function maxCodeVisibleRooms(){
  const building=$("max-codes-building").value;
  const needle=$("max-codes-search").value.trim().toLocaleLowerCase("ru");
  const coded=new Set(activeMaxCodes.map(c=>c.room_id));
  return rooms.filter(r=>(!building||r.building===building) &&
    (!$("max-codes-unbound").checked||!(r.max_users||[]).length) &&
    (!$("max-codes-no-code").checked||!coded.has(r.id)) &&
    [r.building,r.floor,r.name,r.kind].some(v=>String(v).toLocaleLowerCase("ru").includes(needle)));
}
function renderMaxCodes(){
  const available=new Set(rooms.map(r=>r.id));
  for(const id of selectedMaxRooms)if(!available.has(id))selectedMaxRooms.delete(id);
  const visible=maxCodeVisibleRooms(),list=$("max-codes-list"),coded=new Set(activeMaxCodes.map(c=>c.room_id));
  list.replaceChildren();
  for(const room of visible){
    const label=el("label","max-codes-room");
    const check=el("input");check.type="checkbox";check.checked=selectedMaxRooms.has(room.id);
    check.addEventListener("change",()=>{
      if(check.checked){
        if(selectedMaxRooms.size>=250){check.checked=false;toast("Максимум 250 кабинетов за один выпуск",true);return;}
        selectedMaxRooms.add(room.id);
      }else selectedMaxRooms.delete(room.id);
      renderMaxCodes();
    });
    const info=el("span","max-codes-room-text");
    info.append(el("strong","",room.name));
    info.append(el("small","",[
      room.building,room.floor?"этаж "+room.floor:"",
      (room.max_users||[]).length?"MAX: "+room.max_users.map(maxUserName).join(", "):"Нет привязки MAX",
      coded.has(room.id)?"Код уже выпущен":""
    ].filter(Boolean).join(" · ")));
    label.append(check,info);list.append(label);
  }
  if(!visible.length)list.append(el("p","max-codes-empty","Кабинеты не найдены. Попробуйте снять ограничения фильтра."));
  const toggle=$("max-codes-select-all");
  const selectedVisible=visible.filter(r=>selectedMaxRooms.has(r.id)).length;
  toggle.checked=!!visible.length&&selectedVisible===visible.length;
  toggle.indeterminate=selectedVisible>0&&selectedVisible<visible.length;
  toggle.disabled=!visible.length;
  $("max-codes-count").textContent="Выбрано "+selectedMaxRooms.size+" из "+rooms.length+
    " активных · показано "+visible.length+
    (selectedMaxRooms.size>selectedVisible?" · скрыто фильтром "+(selectedMaxRooms.size-selectedVisible):"");
  $("download-max-codes").disabled=selectedMaxRooms.size===0;
}
function renderActiveMaxCodes(){
  const list=$("max-active-codes-list");list.replaceChildren();
  if(!activeMaxCodes.length){
    list.append(el("p","max-codes-empty","Действующих неиспользованных кодов нет."));
    return;
  }
  for(const item of activeMaxCodes){
    const record=el("div","max-code-record"),details=el("div","max-code-details");
    details.append(el("strong","",item.building+" / "+item.name));
    details.append(el("small","","Действует до "+when(item.expires_at)+" · значение не сохраняется"));
    const revoke=el("button","btn secondary","Отозвать");revoke.type="button";
    revoke.addEventListener("click",async()=>{
      if(!confirm("Отозвать код для «"+item.name+"»? Сотрудник больше не сможет использовать этот код."))return;
      revoke.disabled=true;
      try{
        await api("/api/rooms/"+item.room_id+"/pairing","DELETE");
        issuedMaxCodes=issuedMaxCodes.filter(c=>c.id!==item.room_id);
        renderIssuedMaxCodes();await loadMaxCodes();toast("Неиспользованный код отозван");
      }catch(error){revoke.disabled=false;toast(error.message,true);}
    });
    record.append(details,revoke);list.append(record);
  }
}
function renderIssuedMaxCodes(){
  show("max-codes-issued",issuedMaxCodes.length>0);
  const list=$("max-codes-issued-list");list.replaceChildren();
  for(const code of issuedMaxCodes){
    const record=el("div","max-code-record"),details=el("div","max-code-details");
    details.append(el("strong","",code.building+" / "+code.name));
    details.append(el("span","max-code-secret",code.code));
    details.append(el("small","","Истекает "+when(code.expires_at)));
    const copy=el("button","btn secondary","Копировать /bind");copy.type="button";
    copy.addEventListener("click",()=>copyMaxText("/bind "+code.code));
    record.append(details,copy);list.append(record);
  }
}
async function copyMaxText(value){
  try{
    if(navigator.clipboard?.writeText)await navigator.clipboard.writeText(value);
    else{
      const field=document.createElement("textarea");
      field.value=value;field.style.position="fixed";field.style.opacity="0";
      document.body.append(field);field.select();
      const copied=document.execCommand("copy");field.remove();
      if(!copied)throw Error("clipboard");
    }
    toast("Команда скопирована");
  }catch{toast("Не удалось скопировать. Выделите код вручную.",true);}
}
function csvMaxCell(value){
  let s=String(value??"");
  if(/^[\s\x00-\x1f]*[=+\-@]/.test(s))s="'"+s;
  return '"'+s.replace(/"/g,'""')+'"';
}
function downloadIssuedMaxCodes(){
  if(!issuedMaxCodes.length)return;
  const rows=[
    ["Корпус","Этаж","Кабинет","ID кабинета","Код MAX","Команда боту","Действует до (UTC)"],
    ...issuedMaxCodes.map(c=>[c.building,c.floor,c.name,c.id,c.code,"/bind "+c.code,c.expires_at])
  ];
  const csv="\uFEFF"+rows.map(r=>r.map(csvMaxCell).join(";")).join("\r\n")+"\r\n";
  const url=URL.createObjectURL(new Blob([csv],{type:"text/csv;charset=utf-8"}));
  const link=document.createElement("a");
  link.href=url;link.download="termokontur-max-codes.csv";
  document.body.append(link);link.click();link.remove();
  setTimeout(()=>URL.revokeObjectURL(url),15000);
  toast("CSV с кодами сформирован");
}
async function loadMaxCodes(){
  try{
    activeMaxCodes=await api("/api/max/pairing-codes");
    renderActiveMaxCodes();renderMaxCodes();
  }catch(error){
    $("max-active-codes-list").replaceChildren(el("p","max-codes-empty","Не удалось загрузить список: "+error.message));
    toast(error.message,true);
  }
}
$("bulk-max-codes").addEventListener("click",async()=>{
  selectedMaxRooms.clear();issuedMaxCodes=[];activeMaxCodes=[];
  $("max-codes-search").value="";
  $("max-codes-unbound").checked=true;$("max-codes-no-code").checked=true;
  $("max-codes-building").replaceChildren();option($("max-codes-building"),"","Все корпуса");
  for(const building of [...new Set(rooms.map(r=>r.building))].sort())
    option($("max-codes-building"),building,building);
  $("max-codes-expiry").value="1440";
  $("max-codes-dialog").showModal();
  $("max-active-codes-list").replaceChildren(el("p","max-codes-empty","Загрузка действующих кодов…"));
  renderIssuedMaxCodes();renderMaxCodes();await loadMaxCodes();
});
$("max-codes-dialog").addEventListener("close",()=>{
  issuedMaxCodes=[];selectedMaxRooms.clear();renderIssuedMaxCodes();
});
$("close-max-codes").addEventListener("click",()=>$("max-codes-dialog").close());
$("cancel-max-codes").addEventListener("click",()=>$("max-codes-dialog").close());
for(const id of ["max-codes-building","max-codes-unbound","max-codes-no-code"])
  $(id).addEventListener("change",renderMaxCodes);
$("max-codes-search").addEventListener("input",renderMaxCodes);
$("max-codes-reload").addEventListener("click",loadMaxCodes);
$("max-codes-clear").addEventListener("click",()=>{selectedMaxRooms.clear();renderMaxCodes();});
$("max-codes-select-all").addEventListener("change",event=>{
  const visible=maxCodeVisibleRooms();
  if(event.target.checked){
    const toAdd=visible.filter(r=>!selectedMaxRooms.has(r.id));
    if(selectedMaxRooms.size+toAdd.length>250){
      toast("Выберите не более 250 кабинетов",true);renderMaxCodes();return;
    }
    for(const room of toAdd)selectedMaxRooms.add(room.id);
  }else for(const room of visible)selectedMaxRooms.delete(room.id);
  renderMaxCodes();
});
$("max-codes-form").addEventListener("submit",async event=>{
  event.preventDefault();
  if(!selectedMaxRooms.size){toast("Выберите кабинеты",true);return;}
  const replacing=activeMaxCodes.filter(c=>selectedMaxRooms.has(c.room_id));
  if(replacing.length&&!confirm("Для "+replacing.length+" кабинетов уже есть действующие коды. Создать новые и отозвать старые?"))return;
  const assigned=rooms.filter(r=>selectedMaxRooms.has(r.id)&&(r.max_users||[]).length);
  if(assigned.length&&!confirm("В "+assigned.length+" выбранных кабинетах уже есть привязки MAX. Новые коды разрешат привязать ещё пользователей. Продолжить?"))return;
  const button=$("download-max-codes");button.disabled=true;
  try{
    const data=await api("/api/max/pairing-codes","POST",{
      room_ids:[...selectedMaxRooms],validity_minutes:Number($("max-codes-expiry").value)
    });
    issuedMaxCodes=data.codes;
    selectedMaxRooms.clear();renderIssuedMaxCodes();
    await loadMaxCodes();
    $("max-codes-issued").scrollIntoView({block:"nearest",behavior:"smooth"});
    toast("Коды созданы. Скопируйте их или скачайте CSV до закрытия окна.");
  }catch(error){toast(error.message,true);}
  finally{renderMaxCodes();}
});
$("max-codes-copy-all").addEventListener("click",()=>{
  if(issuedMaxCodes.length)copyMaxText(issuedMaxCodes.map(c=>
    c.building+" / "+c.name+": /bind "+c.code).join("\n"));
});
$("max-codes-download-issued").addEventListener("click",downloadIssuedMaxCodes);

/* Live MAX connectivity and queue status: never expose stored credentials. */
async function loadMaxDiagnostics() {
  const target=$("max-diagnostics");
  if(!settingsCache?.max_token_configured) {
    target.textContent="MAX: токен не задан. Сохраните токен, затем проверьте подключение.";
    return;
  }
  target.textContent="Проверка MAX: токен, служба и способ получения событий…";
  try {
    const state=await api("/api/max/diagnostics");
    const lines=[];
    if(state.token_valid===true) {
      lines.push("Токен действителен"+(state.bot_username?" (бот @"+state.bot_username+")":"")+".");
    } else if(state.token_valid===false) {
      lines.push("ОШИБКА: MAX отклонил токен (401). Проверьте, от какого бота он получен, и сохраните действующий токен.");
    } else if(state.api_check_error) {
      lines.push("Не удалось проверить токен через MAX API. Проверьте интернет, DNS и TLS-сертификаты на сервере.");
    }
    if(!state.worker_running) {
      lines.push("Служба обработки MAX не отвечает. На сервере проверьте: systemctl status termokontur-max и journalctl -u termokontur-max -n 60.");
    } else if(state.worker_mode==="waiting_for_token") {
      lines.push("Служба MAX работает, но ещё ожидает токен. Если токен только что сохранён, повторите проверку через несколько секунд.");
    } else {
      lines.push("Служба MAX работает ("+(state.worker_mode==="webhook"?"приём через webhook":"Long Polling")+").");
    }
    if(state.mode==="webhook") {
      if(state.webhook_check_error) lines.push("Не удалось проверить подписку webhook в MAX.");
      else if(state.webhook_registered) lines.push("Webhook зарегистрирован в MAX.");
      else if(state.token_valid===true) lines.push("ВНИМАНИЕ: webhook не найден в MAX. Проверьте публичный HTTPS-адрес.");
    } else {
      if(state.polling_blocked_by_webhook) {
        lines.push("ОШИБКА: для этого бота уже зарегистрирован webhook. MAX не передаёт события через Long Polling при активном webhook. Проверьте подписку другого проекта или используйте отдельного бота.");
      } else if(state.webhook_check_error) {
        lines.push("Не удалось проверить, нет ли конфликтующей webhook-подписки.");
      } else if(state.token_valid===true) {
        lines.push("Long Polling разрешён: конфликтующих webhook-подписок нет.");
      }
    }
    if(state.mode==="polling") {
      const errors={
        tls_certificate:"ОШИБКА TLS: Python не доверяет сертификату MAX. Установите необходимые CA в Debian и укажите SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt для служб termokontur и termokontur-max.",
        authorization:"ОШИБКА: Long Polling получает отказ авторизации. Проверьте токен.",
        rate_limit:"MAX временно ограничил количество запросов. Бот повторит попытку.",
        connection:"Нет связи с API MAX из службы опроса. Проверьте DNS, прокси и исходящий HTTPS.",
        api_error:"MAX возвращает ошибку API; проверьте журнал службы.",
        invalid_response:"MAX возвращает неожиданный ответ; проверьте журнал службы.",
        event_processing:"Предыдущее событие ещё обрабатывается; служба повторит попытку."
      };
      if(state.poll_last_error) lines.push(errors[state.poll_last_error]||"Ошибка опроса MAX, проверьте журнал службы.");
      if(state.poll_last_success_at) lines.push("Последний успешный опрос: "+when(state.poll_last_success_at)+".");
      else if(state.worker_running&&state.token_valid===true) lines.push("Успешных циклов опроса с текущим токеном пока нет.");
    }
    lines.push("Неотправленных ответов: "+state.undelivered+".");
    if(state.repeatedly_failed) lines.push("Повторных ошибок доставки (10+): "+state.repeatedly_failed+".");
    if(state.stalled_events) lines.push("Зависших входящих событий: "+state.stalled_events+".");
    target.textContent=lines.join(" ");
  }catch(error) { target.textContent="Ошибка диагностики MAX: "+error.message; }
}
$("check-max-diagnostics").addEventListener("click",loadMaxDiagnostics);

/* Keep live room status reasonably current without reloading the page. */
setInterval(()=>{
  if(!$("app-root").hidden && !document.hidden && !document.querySelector("dialog[open]"))
    refresh();
},30000);

$("maintenance-form").addEventListener("submit",async event=>{
  event.preventDefault();
  await saveSettingsPatch(event.currentTarget,{
    backup_interval_days:Number($("setting-backup-interval").value),
    backup_keep:Number($("setting-backup-keep").value),
    retention_days:Number($("setting-retention").value)
  },"Параметры резервного копирования и хранения сохранены");
});
const bytesToText=bytes=>bytes>1024*1024?(bytes/1024/1024).toFixed(1)+" МБ":Math.round(bytes/1024)+" КБ";
async function loadBackups(){
  try{
    const backups=await api("/api/backups");
    const view=$("backup-list");view.replaceChildren();
    $("settings-summary-last-backup").textContent=backups.length
      ?("Последняя копия: "+when(backups.reduce((last,item)=>item.created_at>last.created_at?item:last,backups[0]).created_at)):"Пока нет резервных копий";
    if(!backups.length){view.append(el("p","hint","Резервных копий пока нет."));return;}
    for(const item of backups){
      const row=el("div","backup-entry");
      const info=el("div","backup-entry-info");
      info.append(el("strong","",item.name),el("small","",when(item.created_at)+" · "+bytesToText(item.size)));
      const actions=el("div","backup-entry-actions");
      const download=el("button","btn secondary","Скачать");
      download.addEventListener("click",()=>{window.location.href="/api/backups/"+encodeURIComponent(item.name);});
      const restore=el("button","btn secondary","Восстановить");
      restore.addEventListener("click",()=>{
        $("restore-form").reset();
        $("restore-filename").value=item.name;
        $("restore-name").textContent=item.name;
        $("restore-confirm").checked=false;
        $("restore-dialog").showModal();
      });
      const remove=el("button","btn secondary","Удалить");
      remove.addEventListener("click",async()=>{
        if(!confirm("Безвозвратно удалить резервную копию "+item.name+"?"))return;
        try{await api("/api/backups/"+encodeURIComponent(item.name),"DELETE");await loadBackups();toast("Копия удалена");}
        catch(error){toast(error.message,true);}
      });
      actions.append(download,restore,remove);row.append(info,actions);view.append(row);
    }
  }catch(error){$("backup-list").textContent=error.message;}
}
$("backup-create").addEventListener("click",async()=>{
  $("backup-create").disabled=true;
  try{
    const r=await api("/api/backups","POST",{});
    await loadBackups();
    toast("Резервная копия сохранена: "+r.name);
  }catch(error){toast(error.message,true);}
  finally{$("backup-create").disabled=false;}
});
$("backup-import").addEventListener("click",()=>$("backup-upload-file").click());
$("backup-upload-file").addEventListener("change",async()=>{
  const file=$("backup-upload-file").files[0];
  if(!file)return;
  if(file.size>100*1024*1024){toast("Максимальный размер 100 МБ",true);return;}
  if(!confirm("Импортировать SQLite-файл "+file.name+" как резервную копию?"))return;
  try{
    const response=await fetch("/api/backups/import",{
      method:"POST",credentials:"same-origin",
      headers:{"Content-Type":"application/octet-stream"},
      body:file
    });
    if(!response.ok){
      const error=await response.json();
      throw Error(typeof error.detail==="string"?error.detail:"Ошибка импорта");
    }
    const result=await response.json();await loadBackups();
    toast("Копия импортирована: "+result.name);
  }catch(error){toast(error.message,true);}
  finally{$("backup-upload-file").value="";}
});
$("restore-close").addEventListener("click",()=>$("restore-dialog").close());
$("restore-cancel").addEventListener("click",()=>$("restore-dialog").close());
$("restore-form").addEventListener("submit",async event=>{
  event.preventDefault();
  if(!$("restore-confirm").checked){toast("Подтвердите восстановление",true);return;}
  try{
    const r=await api("/api/backups/restore","POST",{
      name:$("restore-filename").value,password:$("restore-password").value
    });
    $("restore-dialog").close();enterLogin();
    $("login-error").textContent="База восстановлена. Войдите снова.";
  }catch(error){toast(error.message,true);}
});
