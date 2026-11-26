"use strict";
const $ = (id) => document.getElementById(id);
let rooms = [], allRooms = [], overview = [], readings = [], daily = null, settingsCache = null, selectedTab = "overview", toastTimer, refreshBusy = false;
let historyData = null, historyRequestId = 0, readingRequestId = 0;
let buildingScope = "";
let panelOpened = false;
const sources = {manual:"Вручную",max:"MAX",ha:"Датчик"};
const statuses = {normal:"В норме",below:"Ниже нормы",above:"Выше нормы",missing:"Нет данных"};
const show = (id, yes) => { $(id).hidden = !yes; };
function setFeedback(id,message,state=""){
  const target=$(id);
  if(target.textContent!==message)target.textContent=message;
  if(state)target.dataset.state=state;
  else delete target.dataset.state;
}
const el = (tag, className = "", value = "") => {
  const item = document.createElement(tag);
  if(className) item.className = className;
  if(value !== null && value !== undefined) item.textContent = String(value);
  return item;
};
const navigationToggle = $("navigation-toggle");
function closeNavigation(restoreFocus=false) {
  if(navigationToggle.getAttribute("aria-expanded")!=="true")return;
  navigationToggle.setAttribute("aria-expanded","false");
  if(restoreFocus)navigationToggle.focus({preventScroll:true});
}
navigationToggle.addEventListener("click",()=>{
  const open=navigationToggle.getAttribute("aria-expanded")==="true";
  navigationToggle.setAttribute("aria-expanded",String(!open));
});
document.querySelector(".sidebar").addEventListener("keydown",event=>{
  if(event.key!=="Escape"||!navigationToggle.offsetWidth)return;
  closeNavigation(true);
});
for(const type of ["click","focusin"]){
  document.addEventListener(type,event=>{
    if(!event.target.closest(".sidebar"))closeNavigation();
  });
}
const sessionEndedMessage="Сеанс завершился. Войдите снова.";
const serverOfflineMessage="Нет связи с сервером. Проверьте подключение.";
const browserOfflineMessage="Нет сети. Проверьте подключение.";
let connectionProblem="";
const activeOperations=new Map();

function syncFeedbackContainer(){
  const openDialogs=[...document.querySelectorAll("dialog[open]")];
  const container=openDialogs.find(dialog=>dialog.contains(document.activeElement))||openDialogs.at(-1)||document.body;
  const feedback=$("panel-feedback");
  if(feedback.parentElement!==container)container.append(feedback);
}
const dialogObserver=new MutationObserver(syncFeedbackContainer);
for(const dialog of document.querySelectorAll("dialog")){
  dialogObserver.observe(dialog,{attributes:true,attributeFilter:["open"]});
}
const toastQueue=[];
function hideToast(){
  clearTimeout(toastTimer);
  show("toast",false);
  if(toastQueue.length)showNextToast();
}
function showNextToast(){
  const next=toastQueue.shift();
  if(!next)return;
  syncFeedbackContainer();
  $("toast").dataset.kind=next.isError?"error":"success";
  $("toast-message").setAttribute("role",next.isError?"alert":"status");
  $("toast-message").textContent=next.message;
  show("toast",true);
  scheduleToast();
}
function scheduleToast(){
  clearTimeout(toastTimer);
  const target=$("toast");
  if(!target.hidden&&!target.matches(":hover, :focus-within")){
    toastTimer=setTimeout(hideToast,target.dataset.kind==="error"?10000:5000);
  }
}
$("toast-close").addEventListener("click",hideToast);
for(const type of ["pointerenter","pointerleave","focusin","focusout"]){
  $("toast").addEventListener(type,()=>queueMicrotask(scheduleToast));
}
function toast(message,isError=false){
  if(!message||message===sessionEndedMessage||isError&&message===connectionProblem)return;
  message=String(message);
  if(!$("toast").hidden&&$("toast-message").textContent===message&&$("toast").dataset.kind===(isError?"error":"success"))return;
  if(toastQueue.some(item=>item.message===message&&item.isError===isError))return;
  if(toastQueue.length>=8)toastQueue.splice(Math.max(0,toastQueue.findIndex(item=>!item.isError)),1);
  toastQueue.push({message,isError});
  if($("toast").hidden)showNextToast();
}
function renderOperationStatus(){
  let message=connectionProblem;
  if(!message){
    for(const operation of activeOperations.values()){
      if(operation.visible)message=operation.message;
    }
  }
  const region=$("operation-status");
  if(region.textContent!==message)region.textContent=message;
  region.dataset.kind=connectionProblem?"error":"working";
  show("operation-status",Boolean(message));
}
async function runOperation(button,message,action,owner=button){
  if(activeOperations.has(owner)||button?.disabled)return;
  const operation={message,visible:false};
  activeOperations.set(owner,operation);
  button?.classList.add("operation-busy");
  button?.setAttribute("aria-busy","true");
  button?.setAttribute("aria-disabled","true");
  const timer=setTimeout(()=>{
    operation.visible=true;
    syncFeedbackContainer();
    renderOperationStatus();
  },250);
  try{await action();}
  catch(error){toast(error.message,true);}
  finally{
    clearTimeout(timer);
    activeOperations.delete(owner);
    button?.classList.remove("operation-busy");
    button?.removeAttribute("aria-busy");
    button?.removeAttribute("aria-disabled");
    renderOperationStatus();
  }
}
function onOperation(control,message,action){
  const isForm=control.tagName==="FORM";
  control.addEventListener(isForm?"submit":"click",event=>{
    if(isForm&&event.submitter?.value==="cancel")return;
    if(isForm)event.preventDefault();
    const button=isForm
      ?event.submitter||control.querySelector('button[type="submit"],button:not([type])')
      :control;
    void runOperation(button,message,()=>action(event),control);
  });
}
window.addEventListener("offline",()=>{
  connectionProblem=browserOfflineMessage;
  renderOperationStatus();
});
window.addEventListener("online",()=>{void api("/health").catch(()=>{});});
function apiTimeout(path){
  if(path==="/api/backups/import")return 300000;
  if(path.includes("/firmware/")||path.includes("/installer")||path.startsWith("/api/backups"))return 120000;
  return 30000;
}
async function api(path,method="GET",body){
  const opts={method,credentials:"same-origin",headers:{"Accept":"application/json"}};
  if(path.startsWith("/api/update/"))opts.cache="no-store";
  if(body instanceof FormData)opts.body=body;
  else if(body instanceof Blob){
    opts.headers["Content-Type"]="application/octet-stream";
    opts.body=body;
  }
  else if(body!==undefined){
    opts.headers["Content-Type"]="application/json";
    opts.body=JSON.stringify(body);
  }
  if(typeof AbortSignal!=="undefined"&&typeof AbortSignal.timeout==="function")opts.signal=AbortSignal.timeout(apiTimeout(path));
  let response;
  try{response=await fetch(path,opts);}
  catch(error){
    if(error.name==="TimeoutError"||opts.signal?.aborted)throw Error(method==="GET"?"Сервер не ответил вовремя.":"Сервер не ответил вовремя. Проверьте, применилось ли действие, прежде чем повторить.");
    connectionProblem=navigator.onLine===false?browserOfflineMessage:serverOfflineMessage;
    renderOperationStatus();
    throw new Error(connectionProblem);
  }
  if(connectionProblem&&navigator.onLine!==false){
    connectionProblem="";
    renderOperationStatus();
  }
  if(response.status===401&&path!=="/api/login"){
    if(!$("app-root").hidden)enterLogin(sessionEndedMessage);
    throw new Error(sessionEndedMessage);
  }
  if(!response.ok){
    const error=await response.json().catch(()=>null);
    const detail=error?.detail;
    const message=typeof detail==="string"?detail:
      Array.isArray(detail)?"Проверьте поля формы и повторите попытку.":"Ошибка запроса ("+response.status+").";
    throw Object.assign(new Error(message),{status:response.status});
  }
  try{return await response.json();}
  catch(error){if(error.name==="TimeoutError"||opts.signal?.aborted)throw Error("Не удалось получить ответ вовремя. Обновите данные.");throw error;}
}
function enterLogin(message="") {
  closeNavigation();
  for(const dialog of document.querySelectorAll("dialog[open]"))dialog.close();
  toastQueue.length=0;hideToast();
  show("login-screen",true); show("app-root",false); $("password").value="";
  $("login-error").textContent=message;
}
function enterApp(session) {
  panelOpened=true;
  buildingScope=session.building||"";
  for(const element of document.querySelectorAll("[data-admin-only]"))element.hidden=Boolean(buildingScope);
  $("panel-scope").textContent=buildingScope;
  show("panel-scope",Boolean(buildingScope));
  show("login-screen",false);show("app-root",true);
}
// Move to the beginning of a different section before replacing long content.
// Otherwise the browser clamps/anchors scroll to the shorter new page and
// sticky navigation visibly jumps. Never animate this corrective scroll.
function scrollToSectionStart(section){
  const offset=parseFloat(getComputedStyle(document.documentElement).scrollPaddingTop)||12;
  const top=Math.max(0,section.getBoundingClientRect().top+window.scrollY-offset);
  if(window.scrollY>top+16)window.scrollTo({top,behavior:"instant"});
}
function setTab(name) {
  const target=$("view-"+name);
  if(!target || (selectedTab===name && !target.hidden))return false;
  scrollToSectionStart($("app-root"));
  selectedTab = name;
  for(const view of ["overview","rooms","staff","reports","readings","sensors","settings"]) {
    const active=view===name;
    show("view-"+view,active);
    const button=document.querySelector('[data-tab="'+view+'"]');
    button.classList.toggle("active",active);
    if(active)button.setAttribute("aria-current","page");
    else button.removeAttribute("aria-current");
  }
  if(name==="readings") drawChart();
  return true;
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
  const oldManualRoom=$("reading-room").value;
  for(const id of ["building-filter","reading-filter","reading-room"]) $(id).replaceChildren();
  option($("building-filter"),"","Все корпуса");
  const buildings=new Set(rooms.map(room=>room.building));
  if(buildingScope)buildings.add(buildingScope);
  for(const building of [...buildings].sort())option($("building-filter"),building,building);
  $("building-filter").value=buildingScope||(buildings.has(oldBuilding)?oldBuilding:"");
  $("building-filter").disabled=Boolean(buildingScope);
  option($("reading-filter"),"","Все помещения");
  option($("reading-room"),"","Выберите помещение");
  for(const room of rooms) {
    const label=room.building+" · "+room.name;
    option($("reading-filter"),String(room.id),label);
    option($("reading-room"),String(room.id),label);
  }
  $("reading-filter").value=oldReading;
  if(!$("reading-filter").value)$("reading-filter").value="";
  $("reading-room").value=oldManualRoom;
  if(!$("reading-room").value)$("reading-room").value="";
  show("reading-room-hint",rooms.length===0);
  $("reading-save").disabled=rooms.length===0;
}
function renderDaily() {
  if(!daily) return;
  const building=$("building-filter").value;
  const selected=daily.rooms.filter(r=>!building||r.building===building);
  const expected=selected.filter(r=>r.expected);
  const missing=selected.filter(r=>r.daily_status==="missing");
  const awaiting=selected.filter(r=>r.daily_status==="awaiting");
  $("daily-date").textContent=fullDay(daily.local_date)+" · "+daily.timezone;
  $("daily-counts").textContent=(expected.length-missing.length-awaiting.length)+" из "+expected.length;
  show("daily-status",!missing.length&&!awaiting.length);
  if(!$("daily-status").hidden){
    $("daily-status").textContent=expected.length?"Показания по графику собраны.":
      selected.length?"По календарю сегодня нет обязательных замеров.":"Нет помещений в выбранном корпусе.";
  }
  for(const [id,group,title] of [
    ["daily-missing-rooms",missing,"Нет измерений"],
    ["daily-awaiting-rooms",awaiting,"Ожидаем выгрузку датчика"],
  ]){
    const details=$(id);
    details.hidden=!group.length;
    details.querySelector("summary").textContent=title+" · "+group.length;
    const labels=group.map(room=>{
      const label=el("span","daily-tag",room.building+" · "+room.name);
      if(room.waiting_until)label.title="Окно выгрузки до "+when(new Date(room.waiting_until*1000).toISOString());
      return label;
    });
    details.querySelector(".daily-tags").replaceChildren(...labels);
  }
}
function renderAttention(selected){
  const list=$("tk-attention-items"),empty=$("tk-attention-empty"),count=$("tk-attention-count");
  if(!list||!empty||!count)return;
  const priority={above:0,below:1,missing:2};
  const issues=selected.filter(r=>Object.hasOwn(priority,r.status)).sort((a,b)=>priority[a.status]-priority[b.status]);
  count.textContent=String(issues.length);
  empty.hidden=Boolean(issues.length);
  empty.textContent=selected.length?"Нет помещений, требующих внимания.":"Помещения пока не добавлены.";
  list.replaceChildren();
  for(const room of issues.slice(0,8)){
    const item=el("li","tk-attention-item");
    const link=el("button","tk-attention-link");link.type="button";
    const label=room.status==="missing"&&room.last_at?"Устарело":statuses[room.status];
    const text=el("span","tk-attention-room");text.append(el("strong","",room.name),el("small","",room.building));
    const detail=el("span","tk-attention-value "+room.status);
    detail.append(el("strong","",room.status==="missing"?"—":temperature(room.last_temp)),el("small","",label));
    link.append(text,detail);
    link.addEventListener("click",()=>{
      $("overview-status").value=room.status==="missing"?"missing":"alert";
      $("search-rooms").value=room.name;renderOverview();
      $("latest-readings-title").focus({preventScroll:true});
      $("latest-readings").scrollIntoView({block:"start"});
    });
    item.append(link);list.append(item);
  }
  if(issues.length>8){const item=el("li","tk-attention-more"),button=el("button","btn secondary","Открыть список помещений");button.type="button";button.addEventListener("click",()=>document.querySelector('[data-overview-status=""]').click());item.append(button);list.append(item);}
}
function renderOverview() {
  const building=$("building-filter").value;
  const selected=overview.filter(r=>!building||r.building===building);
  const counts={normal:0,below:0,above:0,missing:0};
  for(const item of selected)counts[item.status]=(counts[item.status]||0)+1;
  $("count-total").textContent=selected.length;
  $("count-normal").textContent=counts.normal;
  $("count-alert").textContent=counts.below+counts.above;
  $("count-missing").textContent=counts.missing;
  renderAttention(selected);
  const search=$("search-rooms").value.trim().toLocaleLowerCase("ru");
  const status=$("overview-status").value;
  const filtered=selected.filter(room=>{
    const roomStatus=["below","above"].includes(room.status)?"alert":room.status;
    const name=(room.name+" "+room.building+" "+room.kind).toLocaleLowerCase("ru");
    return (!status||status===roomStatus)&&name.includes(search);
  });
  const tbody=$("overview-table");tbody.replaceChildren();
  for(const item of filtered){
    const tr=el("tr");
    cell(tr,item.name).append(el("small","table-building",item.building));
    const val=cell(tr,temperature(item.last_temp),"temp"+(item.status==="below"?" low":item.status==="above"?" high":""));
    val.dataset.sortValue=item.last_temp??"";
    val.title=item.last_source?"Источник: "+(sources[item.last_source]||item.last_source):"";
    const label=item.status==="missing"&&item.last_at?"Устарело":statuses[item.status];
    const statusTd=cell(tr,"");statusTd.append(el("span","status "+item.status,label||item.status));
    cell(tr,Number(item.min_temp).toLocaleString("ru-RU")+"–"+Number(item.max_temp).toLocaleString("ru-RU")+" °C");
    cell(tr,when(item.last_at)).dataset.sortValue=item.last_at||"";
    tbody.append(tr);
  }
  show("overview-empty",filtered.length===0);
  $("overview-empty").textContent=selected.length?"По выбранным фильтрам помещений нет.":
    building?"В этом корпусе пока нет помещений.":"Помещения пока не добавлены.";
}
function shortDay(iso){return iso?iso.slice(8,10)+"."+iso.slice(5,7):"—";}
function fullDay(iso){return iso?iso.slice(8,10)+"."+iso.slice(5,7)+"."+iso.slice(0,4):"—";}
function renderHistoryTable(){
  const tbody=$("history-table");tbody.replaceChildren();
  if(!historyData)return;
  for(const day of [...historyData.series].reverse()){
    const tr=el("tr");
    cell(tr,fullDay(day.date));cell(tr,temperature(day.average));
    cell(tr,temperature(day.minimum));cell(tr,temperature(day.maximum));
    cell(tr,day.rooms_measured+" из "+day.rooms_total);
    cell(tr,String(day.out_of_range));cell(tr,String(day.readings_count));
    tbody.append(tr);
  }
}
function renderHeatmap(){
  const head=$("heatmap-head"),body=$("heatmap-body");
  head.replaceChildren();body.replaceChildren();
  if(!historyData)return;
  const shown=historyData.series.slice(-Math.min(historyData.days,14)).map(x=>x.date);
  $("heatmap-description").textContent="Среднее за день по каждому кабинету · последние "+shown.length+" дней · "+historyData.timezone+".";
  const header=el("tr");header.append(el("th","","Корпус · кабинет"));
  for(const day of shown){
    const th=el("th","",shortDay(day));th.title=fullDay(day);header.append(th);
  }
  header.append(el("th","","Средняя за период"),el("th","","Дней с данными"));
  head.append(header);
  const search=$("heatmap-search").value.trim().toLocaleLowerCase("ru");
  const matching=historyData.rooms.filter(r=>(r.name+" "+r.building).toLocaleLowerCase("ru").includes(search));
  for(const room of matching){
    const tr=el("tr");
    const label=cell(tr,room.name,"heatmap-room");
    label.append(el("small","",room.building));
    for(const day of shown){
      const datum=room.daily[day];
      const value=datum?.average;
      const status=value==null?"no-data":value<room.min_temp?"low":value>room.max_temp?"high":"normal";
      const td=cell(tr,value==null?"—":(status==="low"?"↓ ":status==="high"?"↑ ":"")+Number(value).toLocaleString("ru-RU",{maximumFractionDigits:1}),"heatmap-value "+status);
      td.title=room.building+" · "+room.name+" · "+fullDay(day)+": "+temperature(value)+" · "+({low:"Ниже нормы",high:"Выше нормы",normal:"В норме","no-data":"Нет данных"})[status]+(datum?", измерений: "+datum.count:"");
    }
    cell(tr,temperature(room.average),"heatmap-summary");
    cell(tr,room.days_reported+" / "+historyData.days);
    body.append(tr);
  }
  show("heatmap-empty",matching.length===0);
}
function renderHistory(){
  if(!historyData)return;
  const series=historyData.series;
  const today=series[series.length-1],yesterday=series[series.length-2];
  $("avg-today").textContent=temperature(today.average);
  show("avg-today-desc",today.average==null);
  const change=$("avg-change");
  change.className="dashboard-change";
  change.hidden=today.average==null||yesterday?.average==null;
  if(!change.hidden){
    const diff=today.average-yesterday.average;
    change.textContent=(Math.abs(diff)<0.005?"Без изменений":(diff>0?"↑ ":"↓ ")+Math.abs(diff).toLocaleString("ru-RU",{maximumFractionDigits:2})+" °C")+" к предыдущему дню";
    if(diff>0.005)change.classList.add("rise");
    if(diff< -0.005)change.classList.add("fall");
  }
  $("today-coverage").textContent=today.coverage+"%";
  $("today-coverage-desc").textContent=today.rooms_measured+" из "+today.rooms_total+" помещений";
  $("coverage-progress").value=today.coverage;
  show("today-coverage-desc",true);
  show("coverage-progress",true);
  $("today-alerts").textContent="Вне нормы по средней: "+today.out_of_range;
  show("today-alerts",today.out_of_range>0);
  const hasReadings=series.some(x=>x.average!=null);
  show("history-empty",!hasReadings);
  show("history-chart-frame",hasReadings);
  show("history-legend",hasReadings);
  renderHistoryTable();
  renderBuildingSummary();
  renderBuildingMatrix();
  renderHistoryLegend();
  renderHeatmap();
  if(hasReadings)drawHistoryChart();
}
const buildingPalette=["#984516","#245f9e","#196f58","#72569f","#ac386d","#167c8a","#8b652a","#607526","#ad4545","#526b99"];
const darkBuildingPalette=["#F2A66D","#79B7F5","#57C9AD","#B6A2E8","#f18ebc","#66cad7","#d4b272","#b6cc6d","#fa9290","#a6b8e8"];
function buildingColor(index){const palette=document.documentElement.dataset.theme==="dark"?darkBuildingPalette:buildingPalette;return palette[index%palette.length];}
function renderHistoryLegend(){
  const legend=$("history-legend");legend.replaceChildren();
  if(!historyData)return;
  const buildings=historyData.buildings||[];
  buildings.forEach((building,index)=>{
    const item=el("span","history-key");
    const line=el("i","history-key-line");line.style.background=buildingColor(index);
    item.append(line,document.createTextNode(building.name));
    legend.append(item);
  });
  if(buildings.length!==1){
    const item=el("span","history-key");
    item.append(el("i","history-key-school"),document.createTextNode("Вся школа"));
    legend.append(item);
  }
  legend.append(el("span","legend-muted","Пропуски — дни без показаний"));
}
function renderBuildingSummary(){
  const target=$("building-summary");target.replaceChildren();
  if(!historyData)return;
  const buildings=historyData.buildings||[];
  if(!buildings.length){
    target.append(el("p","building-summary-empty","Здания пока не добавлены."));
    return;
  }
  buildings.forEach((building,index)=>{
    const today=building.series[building.series.length-1],yesterday=building.series[building.series.length-2];
    const card=el("article","building-summary-card");
    card.style.setProperty("--building-accent",buildingColor(index));
    const top=el("div","building-card-top");
    top.append(el("span","building-card-dot"),el("strong","",building.name));
    card.append(top);
    card.append(el("div","building-card-value",temperature(today.average)));
    const diff=today.average==null||yesterday?.average==null?null:today.average-yesterday.average;
    const change=el("span","building-card-change",
      diff==null?"Нет данных для сравнения":Math.abs(diff)<0.005?"Без изменений за сутки":
      (diff>0?"↑ ":"↓ ")+Math.abs(diff).toLocaleString("ru-RU",{maximumFractionDigits:2})+" °C за сутки");
    card.append(change);
    const meta=el("div","building-card-meta");
    meta.append(el("span","","Показания: "+today.rooms_measured+" из "+today.rooms_total),
      el("span","","Отклонения: "+today.out_of_range));
    card.append(meta);target.append(card);
  });
}
function renderBuildingMatrix(){
  const head=$("building-matrix-head"),body=$("building-matrix-body");
  head.replaceChildren();body.replaceChildren();
  if(!historyData)return;
  const series=historyData.series,buildings=historyData.buildings||[];
  const row=el("tr");row.append(el("th","","Здание"));
  for(const day of series)row.append(el("th","",shortDay(day.date)));
  row.append(el("th","","Дней с данными"));
  head.append(row);
  for(const building of buildings){
    const tr=el("tr"),name=cell(tr,building.name,"building-matrix-name");
    name.title="Всего кабинетов: "+building.rooms_total;
    for(const day of building.series){
      const hasData=day.average!=null;
      const status=!hasData?"empty":day.out_of_range?"with-alert":
        day.rooms_measured<day.rooms_total?"partial":"full";
      const td=cell(tr,hasData?Number(day.average).toLocaleString("ru-RU",{maximumFractionDigits:1}):"—",
        "building-day "+status);
      td.title=building.name+" · "+fullDay(day.date)+": "+temperature(day.average)+
        "; кабинетов "+day.rooms_measured+"/"+day.rooms_total+
        "; отклонений "+day.out_of_range+"; измерений "+day.readings_count;
    }
    cell(tr,building.series.filter(day=>day.average!=null).length+" / "+series.length);
    body.append(tr);
  }
  show("building-matrix-empty",buildings.length===0);
}
/* Shared canvas trends: one layout, readable scales, and gap-aware series. */
function drawHistoryChart(){
  const canvas=$("history-chart");
  if(!historyData||selectedTab!=="overview"||$("history-content").hidden||$("history-chart-frame").hidden)return;
  const {series,buildings=[]}=historyData,dark=document.documentElement.dataset.theme==="dark";
  const lines=buildings.map((b,i)=>({name:b.name,color:buildingColor(i),unit:"°C",values:b.series.map(d=>d.average)}));
  if(buildings.length!==1)lines.unshift({name:"Вся школа",color:dark?"#b8c8da":"#63768a",unit:"°C",dash:[7,5],values:series.map(d=>d.average)});
  renderTrend(canvas,{points:series.map(d=>({label:shortDay(d.date),title:fullDay(d.date)})),series:lines});
}

function resetHistorySummary(){
  historyData=null;
  for(const id of ["avg-today","today-coverage"])$(id).textContent="—";
  for(const id of ["avg-today-desc","avg-change","today-coverage-desc","coverage-progress","today-alerts","building-panel","room-drilldown"])
    show(id,false);
}
async function refreshHistory({background=false}={}){
  const sequence=++historyRequestId,building=$("building-filter").value,days=$("history-period").value;
  const keep=background&&historyData&&(historyData.building||"")===building&&Number(historyData.days)===Number(days);
  if(historyData&&(historyData.building||"")!==building)resetHistorySummary();
  if(!keep){
    $("history-state").hidden=false;$("history-state").textContent="Загружаем средние температуры…";
    $("history-content").hidden=true;
  }
  $("history-content").setAttribute("aria-busy","true");
  try{
    const data=await api("/api/dashboard/history?days="+encodeURIComponent(days)+"&building="+encodeURIComponent(building));
    if(sequence!==historyRequestId)return;
    $("history-content").removeAttribute("data-stale");
    if(keep&&JSON.stringify(historyData)===JSON.stringify(data))return;
    historyData=data;
    $("history-state").hidden=true;$("history-content").hidden=false;
    show("building-panel",true);
    show("room-drilldown",true);
    renderHistory();
  }catch(error){
    if(sequence!==historyRequestId)return;
    if(keep){$("history-content").setAttribute("data-stale","true");return;}
    resetHistorySummary();
    $("history-state").textContent="Не удалось загрузить историю: "+error.message;
  }finally{
    if(sequence===historyRequestId)$("history-content").removeAttribute("aria-busy");
  }
}
function exportHistory(){
  if(!historyData){toast("Сначала загрузите данные",true);return;}
  const decimal=v=>v==null?"":String(v).replace(".",",");
  const rows=[["Дата","Здание","Средняя °C","Минимум °C","Максимум °C","Кабинетов с данными","Всего кабинетов","Отклонений","Измерений"]];
  const add=(label,series)=>{
    // Escape CSV cells to keep names and semicolons harmless in spreadsheets.
    const safe=String(label).replace(/^[=+\-@\t\r]/,"'$&").replace(/"/g,'""');
    const quoted='"'+safe+'"';
    for(const day of series)rows.push([day.date,quoted,decimal(day.average),decimal(day.minimum),decimal(day.maximum),day.rooms_measured,day.rooms_total,day.out_of_range,day.readings_count]);
  };
  add("Вся школа",historyData.series);
  for(const building of historyData.buildings||[])add(building.name,building.series);
  const csv="\ufeff"+rows.map(row=>row.join(";")).join("\r\n");
  const url=URL.createObjectURL(new Blob([csv],{type:"text/csv;charset=utf-8"}));
  const link=document.createElement("a");link.href=url;link.download="termokontur-buildings-"+historyData.days+"d.csv";
  document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);
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
    row.setAttribute("role","row");
    cell(row,room.name,"room-name-cell");
    const building=cell(row,room.building,"room-building-cell");
    if(!room.active)building.append(el("span","archived-label","Отключён"));
    const attributes=[
      ["Этаж",room.floor||"—"],["Тип",room.kind],
      ["Норма",room.min_temp+"–"+room.max_temp+" °C"],
    ];
    for(const [label,value] of attributes){
      const detail=cell(row,value,"room-attribute");
      const heading=el("span","room-attribute-label",label);
      heading.setAttribute("aria-hidden","true");
      detail.prepend(heading);
    }
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
        onOperation(remove,"Снимаем привязку MAX…",()=>unbindMax(room,user));
        entry.append(details,remove);list.append(entry);
      }
      maxCell.append(list);
    }
    const actions=cell(row,"","room-actions-cell");
    const duplicate=el("button","","Копировать");
    duplicate.title="Копировать параметры кабинета";
    duplicate.addEventListener("click",()=>openRoom(room,true));
    actions.append(duplicate);
    if(room.active){
      const edit=el("button","","Изменить"),pair=el("button","","Код MAX"),disable=el("button","","Отключить");
      edit.addEventListener("click",()=>openRoom(room));
      onOperation(pair,"Создаём код MAX…",()=>pairing(room));
      onOperation(disable,"Отключаем помещение…",()=>disableRoom(room));
      actions.prepend(edit);actions.append(pair,disable);
    }else{
      const restore=el("button","","Восстановить");
      onOperation(restore,"Восстанавливаем помещение…",()=>restoreRoom(room));
      actions.append(restore);
    }
    for(const detail of row.cells)detail.setAttribute("role","cell");
    tbody.append(row);
  }
  show("rooms-empty",matching.length===0);
  if(!matching.length)$("rooms-empty").textContent=allRooms.length?"Помещения не найдены.":"Добавьте первое помещение.";
}
function renderReadings(){
  const chartVisible=Boolean($("reading-filter").value);
  show("reading-chart-panel",chartVisible);
  show("reading-chart-hint",!chartVisible);
  const tbody=$("readings-table");tbody.replaceChildren();
  for(const row of readings) {
    const tr=el("tr");
    cell(tr,row.room_name).append(el("small","table-building",row.building));
    cell(tr,temperature(row.value),"temp").dataset.sortValue=row.value??"";
    cell(tr,when(row.observed_at)).dataset.sortValue=row.observed_at||"";
    cell(tr,sources[row.source]||row.source);
    cell(tr,row.reporter_display||row.reporter||"—");
    const actions=el("td");
    const history=el("button","btn secondary",
      row.corrections_count?"История ("+row.corrections_count+")":"Подробнее");
    history.type="button";
    history.setAttribute("aria-label","История показания: "+row.building+" / "+row.room_name);
    onOperation(history,"Загружаем историю показания…",()=>openReadingHistory(row));
    actions.append(history);tr.append(actions);tbody.append(tr);
  }
  show("readings-empty",readings.length===0);
  drawChart();
}
async function openReadingHistory(reading){
  try{
    const changes=await api("/api/readings/"+reading.id+"/corrections");
    const target=$("reading-history-content");target.replaceChildren();
    $("reading-history-subtitle").textContent=reading.building+" / "+reading.room_name;
    const original=changes.length?changes[0].old_value:reading.value;
    const created=changes.length?changes[0].old_observed_at:reading.observed_at;
    const first=el("div","reading-history-item");
    first.append(el("strong","","Первоначальное показание: "+temperature(original)),
      el("small","",when(created)+" · "+(reading.reporter_display||reading.reporter||"Автор неизвестен")));
    target.append(first);
    for(const correction of changes){
      const change=el("div","reading-history-item");
      change.append(el("strong","",temperature(correction.old_value)+" → "+temperature(correction.new_value)),
        el("small","",when(correction.corrected_at)+" · Исправил(а): "+correction.actor_display));
      target.append(change);
    }
    if(!changes.length)target.append(el("p","hint","Исправлений пока не было."));
    $("reading-history-dialog").showModal();
  }catch(error){toast(error.message,true);}
}
$("reading-history-close").addEventListener("click",()=>$("reading-history-dialog").close());
$("reading-history-done").addEventListener("click",()=>$("reading-history-dialog").close());
function drawChart(){
  const filter=$("reading-filter").value;
  if(!filter||$("view-readings").hidden)return;
  const data=readings.filter(r=>String(r.room_id)===filter).slice(0,40).reverse();
  const dark=document.documentElement.dataset.theme==="dark";
  renderTrend($("reading-chart"),{
    points:data.map(r=>({label:when(r.observed_at),title:when(r.observed_at),time:Date.parse(r.observed_at)})),
    series:[{name:"Температура",unit:"°C",color:dark?"#5bdac1":"#087b65",values:data.map(r=>r.value==null?null:Number(r.value))}]
  });
}

async function refresh({background=false}={}) {
  if(refreshBusy)return;
  refreshBusy=true;
  try {
    const adminData=buildingScope?[]:[api("/api/rooms"),
      api("/api/rooms?include_inactive=true"),api("/api/max/users")];
    const [dash,day,activeRooms,all,staff]=await Promise.all([api("/api/dashboard"),api("/api/daily"),...adminData]);
    const changed=(oldValue,newValue)=>!background||JSON.stringify(oldValue)!==JSON.stringify(newValue);
    const nextRooms=activeRooms||dash.rooms;
    const roomChange=changed(rooms,nextRooms),overviewChange=changed(overview,dash.rooms),dailyChange=changed(daily,day);
    overview=dash.rooms;rooms=nextRooms;daily=day;
    if(roomChange)renderFilters();
    if(roomChange||overviewChange)renderOverview();
    if(roomChange||dailyChange)renderDaily();
    if(!buildingScope){
      const allChange=changed(allRooms,all),staffChange=changed(maxStaff,staff);
      allRooms=all;maxStaff=staff;
      if(allChange)renderRooms();
      if(allChange||staffChange)renderMaxStaff();
    }
    await Promise.all([refreshHistory({background}),buildingScope?undefined:refreshReadings({background})]);
    const updated=$("updated-at");
    updated.dateTime=dash.generated_at;
    updated.textContent="Обновлено "+when(dash.generated_at);
    updated.hidden=false;
  } catch(error) {if(!background)toast(error.message,true);}
  finally {refreshBusy=false;}
}
async function refreshReadings({background=false}={}){
  const request=++readingRequestId,room=$("reading-filter").value;
  try{
    const result=await api("/api/readings?limit=200"+(room?"&room_id="+encodeURIComponent(room):""));
    if(request!==readingRequestId)return;
    if(background&&JSON.stringify(readings)===JSON.stringify(result))return;
    readings=result;
    renderReadings();
  }catch(error){if(request===readingRequestId&&!background)toast(error.message,true);}
}
function openReading({building="",room=""}={}){
  $("reading-form").reset();
  const choices=rooms.filter(room=>!building||room.building===building);
  $("reading-room").value=room||(choices.length===1?String(choices[0].id):"");
  show("reading-room-hint",rooms.length===0);
  $("reading-save").disabled=rooms.length===0;
  $("reading-dialog").showModal();
}
function openRoom(room=null, duplicate=false){
  $("room-form").reset();
  $("room-dialog-title").textContent=duplicate?"Копировать помещение":room?"Изменить помещение":"Новое помещение";
  $("edit-room-id").value=room && !duplicate ? room.id : "";
  if(room){for(const [name,value] of [["building",room.building],["name",room.name],["floor",room.floor],["kind",room.kind],["min",room.min_temp],["max",room.max_temp]]) $("room-"+name).value=value;}
  $("room-dialog").showModal();
}
async function restoreRoom(room){
  try{
    await api("/api/rooms/"+room.id,"PATCH",{active:true});
    await refresh();toast("Помещение восстановлено. История сохранена.");
  }catch(error){toast(error.message,true);}
}
async function disableRoom(room) {
  if(!confirm("Отключить «"+room.name+"»? История измерений сохранится."))return;
  try {await api("/api/rooms/"+room.id,"PATCH",{active:false});await refresh();toast("Помещение отключено");}
  catch(error){toast(error.message,true);}
}
onOperation($("login-form"),"Входим в панель…",async()=>{
  if($("password").disabled)return;
  $("login-error").textContent="";
  try{
    const session=await api("/api/login","POST",{username:$("login-name").value,password:$("password").value});
    if(panelOpened)window.location.reload();
    else{enterApp(session);await refresh();}
  }
  catch(error){$("login-error").textContent=error.message;}
});
onOperation($("logout"),"Завершаем сеанс…",async()=>{
  try{await api("/api/logout","POST");window.location.reload();}
  catch(error){toast(error.message,true);}
});
onOperation($("refresh"),"Обновляем данные панели…",()=>refresh());
for(const nav of document.querySelectorAll("[data-tab]"))nav.addEventListener("click",()=>{
  const name=nav.dataset.tab;
  const changed=setTab(name);
  if(navigationToggle.offsetWidth){
    closeNavigation(!changed);
    if(changed){
      const title=$("view-"+name).querySelector("h1");
      title.tabIndex=-1;
      title.focus({preventScroll:true});
    }
  }
  if(!changed)return; // Re-selecting an open tab must not reload or shift its contents.
  if(name==="overview")drawHistoryChart();
  if(name==="readings")refreshReadings();
  if(name==="staff")void loadMaxStaff().catch(error=>toast(error.message,true));
  if(name==="reports")void loadReports().catch(error=>toast(error.message,true));
  if(name==="sensors")refreshSensors();
  if(name==="settings"){
    if(!settingsDirty()||!settingsLoaded)void loadSettings();
    if(selectedSettingsCategory==="notifications")void loadNotificationCenter();
  }
});
$("search-rooms").addEventListener("input",renderOverview);
$("overview-status").addEventListener("change",renderOverview);
for(const button of document.querySelectorAll("[data-overview-status]")){
  button.addEventListener("click",()=>{
    $("overview-status").value=button.dataset.overviewStatus;
    $("search-rooms").value="";
    renderOverview();
    $("latest-readings-title").focus({preventScroll:true});
    $("latest-readings").scrollIntoView({block:"start"});
  });
}
$("building-filter").addEventListener("change",()=>{renderOverview();renderDaily();refreshHistory();});
$("history-period").addEventListener("change",refreshHistory);
$("heatmap-search").addEventListener("input",renderHeatmap);
$("history-export").addEventListener("click",exportHistory);
$("reading-filter").addEventListener("change",()=>{
  readings=[];
  renderReadings();
  void refreshReadings();
});
$("quick-reading").addEventListener("click",()=>openReading({building:$("building-filter").value}));
$("add-reading").addEventListener("click",()=>openReading({room:$("reading-filter").value}));
$("tk-empty-reading")?.addEventListener("click",()=>$("add-reading").click());
$("add-room").addEventListener("click",()=>openRoom());
$("rooms-search").addEventListener("input",renderRooms);
onOperation($("report-filter-form"),"Формируем отчёт…",loadReport);
$("report-filter-form").addEventListener("change",()=>{
  reportRequestId++;
  clearReport("Нажмите «Сформировать», чтобы обновить отчёт.");
});
$("report-export-excel").addEventListener("click",()=>reportExport("xlsx"));
$("report-export-pdf").addEventListener("click",()=>reportExport("pdf"));
for(const button of document.querySelectorAll("[data-report-section]")){
  button.addEventListener("click",()=>reportSection(button.dataset.reportSection));
}
$("report-sub-frequency").addEventListener("change",()=>{
  show("report-sub-weekday-wrapper",$("report-sub-frequency").value==="weekly");
});
onOperation($("report-sub-form"),"Сохраняем расписание отчётов…",async event=>{
  event.preventDefault();
  const user_id=$("report-sub-user").value;
  if(!user_id){toast("Выберите получателя MAX",true);return;}
  try{
    await api("/api/reports/subscriptions","POST",{
      user_id,building:$("report-sub-building").value,
      frequency:$("report-sub-frequency").value,
      weekday:Number($("report-sub-weekday").value),
      send_time:$("report-sub-time").value,enabled:true
    });
    await loadReportSubscriptions();toast("Расписание отчётов добавлено");
  }catch(error){toast(error.message,true);}
});
initMaxStaff();
$("show-inactive").addEventListener("change",renderRooms);
$("export-rooms").addEventListener("click",()=>{window.location.href="/api/rooms/export.csv";});
$("export-csv").addEventListener("click",()=>{
  const room=$("reading-filter").value;
  window.location.href="/api/export.csv"+(room?"?room_id="+encodeURIComponent(room):"");
});
initMaxPairing();
onOperation($("room-form"),"Сохраняем помещение…",async()=>{
  const roomId=$("edit-room-id").value;
  const data={building:$("room-building").value.trim(),floor:$("room-floor").value.trim(),
    name:$("room-name").value.trim(),kind:$("room-kind").value.trim(),
    min_temp:Number($("room-min").value),max_temp:Number($("room-max").value)};
  if(data.min_temp>=data.max_temp){toast("Минимум должен быть ниже максимума",true);return;}
  try{await api(roomId?"/api/rooms/"+roomId:"/api/rooms",roomId?"PATCH":"POST",data);
    $("room-dialog").close();await refresh();toast("Помещение сохранено");}
  catch(error){toast(error.message,true);}
});
onOperation($("reading-form"),"Записываем показание…",async()=>{
  try{await api("/api/readings","POST",{room_id:Number($("reading-room").value),value:Number($("reading-value").value)});
    $("reading-dialog").close();await refresh();toast("Показание записано");}
  catch(error){toast(error.message,true);}
});
function redrawPanelTrends(){if(selectedTab==="readings")drawChart();if(selectedTab==="overview"){renderHistoryLegend();renderBuildingSummary();drawHistoryChart();}if(selectedTab==="reports")drawReportTrend();}
window.addEventListener("resize",()=>{if(selectedTab==="readings")drawChart();if(selectedTab==="overview")drawHistoryChart();if(selectedTab==="reports")drawReportTrend();});
document.addEventListener("termokontur:themechange",redrawPanelTrends);
async function bootPanel(){
  // Do not race the initial session probe against an explicit login.
  try{
    const result=await api("/api/session");
    if(result.authenticated){enterApp(result);await refresh();}
    else enterLogin();
  }catch{enterLogin();}
  finally{
    for(const field of [$("login-name"),$("password"),$("login-form").querySelector('button[type="submit"]')])field.disabled=false;
  }
}

initSettings();

/* Targeted MAX notifications use the same recipient snapshot for preview and sending. */
initNotifications();

initMaxToken();
onOperation($("rotate-ingest"),"Создаём ключ приёма показаний…",async()=>{
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
async function loadAudit(){
  const state=$("audit-state"),body=$("audit-records");
  state.textContent="Загружаем журнал…";
  try{
    const records=await api("/api/audit");
    body.replaceChildren();
    for(const item of records){const tr=el("tr");cell(tr,when(item.recorded_at));cell(tr,item.method+" "+item.route);cell(tr,item.target||"—");cell(tr,String(item.status));body.append(tr);}
    state.textContent=records.length?"Записей: "+records.length:"Пока нет действий";
  }catch(error){state.textContent=error.message;}
}
onOperation($("audit-refresh"),"Загружаем журнал…",loadAudit);
function passwordFeedback(){
  const value=$("new-password").value,confirmValue=$("confirm-password").value;
  const hint=$("password-feedback");
  hint.classList.remove("valid","invalid");
  if(!value){hint.textContent="От 8 до 256 символов.";return;}
  if(value.length<8){
    hint.textContent="Символов: "+value.length+" из 8 символов";
    hint.classList.add("invalid");
  }else if(confirmValue && value!==confirmValue){
    hint.textContent="Пароли не совпадают";
    hint.classList.add("invalid");
  }else if(confirmValue){
    hint.textContent="Пароли совпадают";
    hint.classList.add("valid");
  }else{
    hint.textContent="Повторите пароль ниже.";
    hint.classList.add("valid");
  }
}
$("new-password").addEventListener("input",passwordFeedback);
$("confirm-password").addEventListener("input",passwordFeedback);
onOperation($("password-settings-form"),"Меняем пароль…",async event=>{
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
onOperation($("bulk-form"),"Добавляем кабинеты…",async event=>{
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
initUpdates();

initMaxCodes();

initMaxDiagnostics();

/* Keep live room status reasonably current without reloading the page. */
setInterval(()=>{
  if(!$("app-root").hidden && !document.hidden && !document.querySelector("dialog[open]"))
    refresh({background:true});
},30000);

onOperation($("maintenance-form"),"Сохраняем настройки обслуживания…",async event=>{
  await saveSettingsPatch(event.currentTarget,{
    backup_interval_days:Number($("setting-backup-interval").value),
    backup_keep:Number($("setting-backup-keep").value),
    retention_days:Number($("setting-retention").value)
  },"Настройки копирования и хранения сохранены");
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
      onOperation(remove,"Удаляем резервную копию…",async()=>{
        if(!confirm("Безвозвратно удалить резервную копию "+item.name+"?"))return;
        try{await api("/api/backups/"+encodeURIComponent(item.name),"DELETE");await loadBackups();toast("Копия удалена");}
        catch(error){toast(error.message,true);}
      });
      actions.append(download,restore,remove);row.append(info,actions);view.append(row);
    }
  }catch(error){$("backup-list").textContent=error.message;}
}
onOperation($("backup-create"),"Создаём резервную копию…",async()=>{
  $("backup-create").disabled=true;
  try{
    const r=await api("/api/backups","POST",{});
    await loadBackups();
    toast("Резервная копия сохранена: "+r.name);
  }catch(error){toast(error.message,true);}
  finally{$("backup-create").disabled=false;}
});
$("backup-import").addEventListener("click",()=>$("backup-upload-file").click());
$("backup-upload-file").addEventListener("change",()=>{
  const file=$("backup-upload-file").files[0];
  $("backup-upload-file").value="";
  if(!file)return;
  if(!confirm("Импортировать SQLite-файл "+file.name+" как резервную копию?"))return;
  void runOperation($("backup-import"),"Импортируем резервную копию…",async()=>{
    const result=await api("/api/backups/import","POST",file);
    await loadBackups();
    toast("Копия импортирована: "+result.name);
  });
});
$("restore-close").addEventListener("click",()=>$("restore-dialog").close());
$("restore-cancel").addEventListener("click",()=>$("restore-dialog").close());
onOperation($("restore-form"),"Восстанавливаем базу…",async event=>{
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

// All modules and event handlers are initialized before probing the session.
void bootPanel();
