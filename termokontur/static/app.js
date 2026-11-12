"use strict";
const $ = (id) => document.getElementById(id);
let rooms = [], allRooms = [], overview = [], readings = [], daily = null, settingsCache = null, selectedTab = "overview", toastTimer;
const selectedMaxRooms = new Set();
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
function renderRooms(){
  const tbody=$("rooms-table");tbody.replaceChildren();
  const search=$("rooms-search").value.trim().toLocaleLowerCase("ru");
  const showInactive=$("show-inactive").checked;
  const matching=allRooms.filter(r=>(showInactive || r.active) &&
    [r.name,r.building,r.floor,r.kind].some(x=>String(x).toLocaleLowerCase("ru").includes(search)));
  for(const room of matching) {
    const row=el("tr", room.active ? "" : "archived-room");
    cell(row,room.name);
    const building=cell(row,room.building);
    if(!room.active)building.append(el("span","archived-label","Отключён"));
    cell(row,room.floor||"—");cell(row,room.kind);
    cell(row,room.min_temp+"–"+room.max_temp+" °C");
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
  try {
    const selected=$("reading-filter").value;
    const params=selected?"?room_id="+encodeURIComponent(selected)+"&limit=200":"?limit=200";
    const [dash,activeRooms,items,day,all]=await Promise.all([api("/api/dashboard"),api("/api/rooms"),api("/api/readings"+params),api("/api/daily"),api("/api/rooms?include_inactive=true")]);
    overview=dash.rooms;rooms=activeRooms;allRooms=all;readings=items;daily=day;
    renderFilters();renderOverview();renderDaily();renderRooms();renderReadings();
    $("updated-at").textContent="Обновлено: "+when(dash.generated_at);
  } catch(error) {toast(error.message,true);}
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
for(const nav of document.querySelectorAll("[data-tab]"))nav.addEventListener("click",()=>{setTab(nav.dataset.tab);if(nav.dataset.tab==="readings")refreshReadings();if(nav.dataset.tab==="settings")loadSettings();});
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

/* Settings are persisted on the server, not stored in browser or config files. */
async function loadSettings() {
  try {
    settingsCache=await api("/api/settings");
    $("setting-organization").value=settingsCache.organization_name;
    $("setting-timezone").value=settingsCache.timezone;
    $("setting-stale").value=settingsCache.stale_hours;
    $("setting-enabled").checked=settingsCache.reminders_enabled;
    $("setting-time").value=settingsCache.reminder_time;
    $("setting-window").value=settingsCache.reminder_window_minutes;
    for(const checkbox of document.querySelectorAll('[name="remind-day"]'))
      checkbox.checked=settingsCache.reminder_days.includes(Number(checkbox.value));
    $("max-status").textContent=settingsCache.max_token_configured?"Токен MAX настроен":"Токен MAX отсутствует";
    $("max-webhook-mode").textContent=settingsCache.max_mode==="webhook"?"MAX: webhook включён (производственный режим).":"MAX: тестовый режим Long Polling.";
    $("max-webhook-url").value=settingsCache.max_webhook_url || "";
    $("switch-polling").disabled=settingsCache.max_mode!=="webhook";
    $("activate-webhook").disabled=!settingsCache.max_token_configured;
    $("ingest-status").textContent=settingsCache.ingest_key_configured?"Ключ датчиков настроен":"Нет ключа для датчиков";
    document.title=settingsCache.organization_name+" — Термоконтур";
    await loadUpdateStatus();
  } catch(error){toast(error.message,true);}
}
$("general-settings-form").addEventListener("submit",async event=>{
  event.preventDefault();
  try{
    const data={
      organization_name:$("setting-organization").value,
      timezone:$("setting-timezone").value,
      stale_hours:Number($("setting-stale").value),
      reminders_enabled:$("setting-enabled").checked,
      reminder_time:$("setting-time").value,
      reminder_days:[...document.querySelectorAll('[name="remind-day"]:checked')].map(x=>Number(x.value)),
      reminder_window_minutes:Number($("setting-window").value)
    };
    settingsCache=await api("/api/settings","PUT",data);
    toast("Настройки сохранены");await refresh();
  }catch(error){toast(error.message,true);}
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
async function loadUpdateStatus(){
  try{
    const r=await api("/api/update/status");
    $("update-status-text").textContent=r.message;
    $("update-version").textContent=r.installed_revision?"Текущая ревизия: "+r.installed_revision.slice(0,12):"";
    $("install-update").disabled=!r.supported||["queued","installing"].includes(r.state);
  }catch(error){$("update-status-text").textContent="Ошибка проверки: "+error.message;}
}
$("check-update").addEventListener("click",async()=>{
  $("check-update").disabled=true;
  try{
    const r=await api("/api/update/check");
    $("update-status-text").textContent=r.message;
    $("install-update").disabled=!r.supported||r.available!==true;
  }catch(error){toast(error.message,true);}
  finally{$("check-update").disabled=false;}
});
$("install-update").addEventListener("click",async()=>{
  if(!confirm("Установить обновление с официального GitHub? Панель временно перезапустится."))return;
  try{const r=await api("/api/update/request","POST",{});toast(r.message);await loadUpdateStatus();}
  catch(error){toast(error.message,true);}
});

/* Bulk MAX onboarding: administrator selects rooms and downloads one-time codes. */
function maxCodeVisibleRooms(){
  const building=$("max-codes-building").value;
  const needle=$("max-codes-search").value.trim().toLocaleLowerCase("ru");
  return rooms.filter(r=>(!building||r.building===building) &&
    [r.building,r.floor,r.name,r.kind].some(value=>
      String(value).toLocaleLowerCase("ru").includes(needle)));
}
function renderMaxCodes(){
  const activeIds=new Set(rooms.map(room=>room.id));
  for(const id of selectedMaxRooms) if(!activeIds.has(id))selectedMaxRooms.delete(id);
  const visible=maxCodeVisibleRooms();
  const list=$("max-codes-list");list.replaceChildren();
  for(const room of visible){
    const label=el("label","max-codes-room");
    const check=el("input");check.type="checkbox";check.checked=selectedMaxRooms.has(room.id);
    check.addEventListener("change",()=>{
      if(check.checked){
        if(selectedMaxRooms.size>=250){
          check.checked=false;toast("Максимум 250 кабинетов за один выпуск",true);return;
        }
        selectedMaxRooms.add(room.id);
      }else selectedMaxRooms.delete(room.id);
      renderMaxCodes();
    });
    const info=el("span","max-codes-room-text");
    info.append(el("strong","",room.name));
    info.append(el("small","",room.building+(room.floor?" · этаж "+room.floor:"")));
    label.append(check,info);list.append(label);
  }
  if(!visible.length)list.append(el("p","max-codes-empty","Нет кабинетов для выбранного фильтра."));
  const selectAll=$("max-codes-select-all");
  const selectedVisible=visible.filter(room=>selectedMaxRooms.has(room.id)).length;
  selectAll.checked=!!visible.length&&selectedVisible===visible.length;
  selectAll.indeterminate=selectedVisible>0&&selectedVisible<visible.length;
  selectAll.disabled=!visible.length;
  $("max-codes-count").textContent=
    "Выбрано: "+selectedMaxRooms.size+" из "+rooms.length+" активных кабинетов. Отображается: "+visible.length+".";
  $("download-max-codes").disabled=selectedMaxRooms.size===0;
}
$("bulk-max-codes").addEventListener("click",()=>{
  selectedMaxRooms.clear();
  $("max-codes-search").value="";
  $("max-codes-building").replaceChildren();
  option($("max-codes-building"),"","Все корпуса");
  for(const building of [...new Set(rooms.map(room=>room.building))].sort())
    option($("max-codes-building"),building,building);
  $("max-codes-expiry").value="1440";
  $("max-codes-dialog").showModal();
  renderMaxCodes();
});
$("close-max-codes").addEventListener("click",()=>$("max-codes-dialog").close());
$("cancel-max-codes").addEventListener("click",()=>$("max-codes-dialog").close());
$("max-codes-building").addEventListener("change",renderMaxCodes);
$("max-codes-search").addEventListener("input",renderMaxCodes);
$("max-codes-clear").addEventListener("click",()=>{
  selectedMaxRooms.clear();renderMaxCodes();
});
$("max-codes-select-all").addEventListener("change",event=>{
  const visible=maxCodeVisibleRooms();
  if(event.target.checked){
    const toAdd=visible.filter(room=>!selectedMaxRooms.has(room.id));
    if(selectedMaxRooms.size+toAdd.length>250){
      toast("Выберите не более 250 кабинетов за один раз",true);
      renderMaxCodes();return;
    }
    for(const room of toAdd)selectedMaxRooms.add(room.id);
  }else for(const room of visible)selectedMaxRooms.delete(room.id);
  renderMaxCodes();
});
$("max-codes-form").addEventListener("submit",async event=>{
  event.preventDefault();
  if(selectedMaxRooms.size===0){toast("Выберите кабинеты",true);return;}
  const button=$("download-max-codes");
  button.disabled=true;
  try{
    const response=await fetch("/api/max/pairing-codes.csv",{
      method:"POST",credentials:"same-origin",cache:"no-store",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({
        room_ids:[...selectedMaxRooms],
        validity_minutes:Number($("max-codes-expiry").value)
      })
    });
    if(!response.ok){
      if(response.status===401)enterLogin();
      let reason="Ошибка выпуска кодов";
      try{const data=await response.json();reason=typeof data.detail==="string"?data.detail:reason;}
      catch{}
      throw Error(reason);
    }
    const csv=await response.blob();
    const url=URL.createObjectURL(csv);
    const link=document.createElement("a");
    link.href=url;link.download="termokontur-max-codes.csv";
    document.body.append(link);link.click();link.remove();
    setTimeout(()=>URL.revokeObjectURL(url),15000);
    $("max-codes-dialog").close();
    toast("Коды созданы. Храните файл в защищённом месте и выдавайте коды индивидуально.");
    selectedMaxRooms.clear();
  }catch(error){toast(error.message,true);}
  finally{renderMaxCodes();}
});

/* Configure a trustworthy public HTTPS webhook without editing server files. */
$("max-webhook-form").addEventListener("submit",async event=>{
  event.preventDefault();
  if(!confirm("Активировать HTTPS webhook в MAX? При этом тестовый Long Polling будет отключён."))return;
  try{
    await api("/api/settings/max-webhook","POST",{url:$("max-webhook-url").value.trim()});
    toast("Webhook зарегистрирован в MAX");
    await loadSettings();
  }catch(error){toast(error.message,true);}
});
$("switch-polling").addEventListener("click",async()=>{
  if(!confirm("Отключить webhook и вернуться к тестовому Long Polling?"))return;
  try{
    await api("/api/settings/max-polling","POST",{});
    toast("MAX переведён в тестовый режим");
    await loadSettings();
  }catch(error){toast(error.message,true);}
});
