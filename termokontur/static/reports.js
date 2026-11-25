"use strict";
// Reports UI and subscriptions. Loaded before app.js; helpers resolve when called.
let reportsData = null, reportRequestId = 0, selectedReportSection = "coverage";

function drawReportTrend(){
  const section=selectedReportSection;
  if(!reportsData||!["temperature","humidity"].includes(section)||$("report-pane-"+section).hidden)return;
  const humidity=section==="humidity",days=reportsData.daily,dark=document.documentElement.dataset.theme==="dark";
  const name=humidity?"Средняя влажность":"Средняя температура";
  renderTrend($("report-"+section+"-chart"),{
    points:days.map(d=>({label:shortDay(d.date),title:fullDay(d.date)})),
    series:[{name,unit:humidity?"%":"°C",color:humidity?(dark?"#87b7fc":"#356bad"):(dark?"#5bdac1":"#087b65"),values:days.map(d=>d[humidity?"humidity_average":"average"])}],
    empty:humidity?"Нет замеров влажности":"Нет замеров температуры"
  });
}

function reportNumber(value,suffix=""){
  return value==null?"—":Number(value).toLocaleString("ru-RU",{maximumFractionDigits:2})+suffix;
}
function reportTable(bodyId,items,render){
  const body=$(bodyId);body.replaceChildren();
  for(const item of items){
    const tr=el("tr");
    render(tr,item);
    body.append(tr);
  }
  if(!items.length){
    const tr=el("tr"),td=el("td","","Нет данных за выбранный период");
    td.colSpan=body.closest("table").tHead.rows[0].cells.length;
    tr.append(td);body.append(tr);
  }
}
function reportSection(name){
  selectedReportSection=name;
  for(const section of ["coverage","temperature","humidity","buildings","staff"]){
    show("report-pane-"+section,section===name);
    const button=document.querySelector('[data-report-section="'+section+'"]');
    button.classList.toggle("active",section===name);
    button.setAttribute("aria-pressed",String(section===name));
  }
  drawReportTrend();
}
function reportKpi(value,label){
  const box=el("div","report-kpi");
  box.append(el("strong","",value),el("small","",label));
  return box;
}
function renderReport(){
  const r=reportsData;
  show("report-results",Boolean(r));
  show("report-kpis",Boolean(r));
  for(const id of ["report-export-excel","report-export-pdf"])$(id).disabled=!r;
  if(!r)return;
  const s=r.summary;
  const kpis=$("report-kpis");kpis.replaceChildren(
    reportKpi(reportNumber(s.coverage,"%"),"Охват показаниями"),
    reportKpi(reportNumber(s.rooms_measured),"Кабинето-дней с замерами"),
    reportKpi(reportNumber(s.missing),"Пропущено"),
    reportKpi(reportNumber(s.average," °C"),"Средняя температура"),
    reportKpi(reportNumber(s.out_of_range),"Дней вне порогов")
  );
  $("report-status").textContent="Период "+fullDay(r.from)+" — "+fullDay(r.to)+
    " · "+(r.building||"Все здания")+" · "+r.timezone+
    " · "+r.rooms_count+" помещений"+(s.sync_pending?" · ожидаются пакеты датчиков":"");
  $("report-staff-note").textContent=r.note;
  reportTable("report-coverage-body",r.daily,(tr,d)=>{
    for(const item of [d.date,d.rooms_total,d.rooms_measured,d.missing,d.awaiting,
      reportNumber(d.coverage,"%"),d.out_of_range])cell(tr,item);
  });
  const submitted=r.room_daily.filter(d=>d.readings>0).reverse();
  $("report-submitted-count").textContent=submitted.length;
  reportTable("report-submitted-body",submitted.slice(0,300),(tr,d)=>{
    for(const item of [d.date,d.building,d.room,reportNumber(d.average," °C"),
      d.reporter_name||"—",d.measured_at?when(d.measured_at):"—"])cell(tr,item);
  });
  $("report-missing-count").textContent=r.missing.length;
  reportTable("report-missing-body",r.missing.slice(0,300),(tr,d)=>{
    for(const item of [d.date,d.building,d.room])cell(tr,item);
  });
  reportTable("report-trend-body",r.daily,(tr,d)=>{
    for(const item of [d.date,reportNumber(d.average," °C"),
      reportNumber(d.minimum," °C"),reportNumber(d.maximum," °C"),
      d.out_of_range])cell(tr,item);
  });
  for(const [id,rows,group,count] of [["report-humidity-body",r.daily,"date","humidity_rooms"],
    ["report-humidity-buildings",r.building_summary,"building","humidity_readings"]]){
    reportTable(id,rows,(tr,d)=>{
      for(const value of [d[group],reportNumber(d.humidity_average," %"),reportNumber(d.humidity_minimum," %"),
        reportNumber(d.humidity_maximum," %"),d[count]])cell(tr,value);
    });
  }
  reportTable("report-temperature-body",r.rooms,(tr,d)=>{
    for(const item of [d.building+" / "+d.room,d.days_measured,
      reportNumber(d.average," °C"),reportNumber(d.minimum," °C"),
      reportNumber(d.maximum," °C"),d.out_of_range_days])cell(tr,item);
  });
  reportTable("report-buildings-body",r.building_summary,(tr,d)=>{
    for(const item of [d.building,d.rooms,reportNumber(d.coverage,"%"),
      reportNumber(d.average," °C"),d.missing,d.out_of_range])cell(tr,item);
  });
  const bars=$("report-building-bars");bars.replaceChildren();
  for(const d of r.building_summary){
    const row=el("div","report-building-bar"),heading=el("div","report-building-heading");
    heading.append(el("strong","",d.building),el("span","",reportNumber(d.coverage,"%")+" · "+reportNumber(d.average," °C")));
    const track=el("div","report-bar-track"),fill=el("div","report-bar-fill");
    fill.style.width=Math.max(0,Math.min(100,Number(d.coverage)||0))+"%";
    track.append(fill);row.append(heading,track);bars.append(row);
  }
  reportTable("report-staff-body",r.staff,(tr,d)=>{
    for(const item of [d.name,d.rooms_count,reportNumber(d.coverage,"%"),
      d.missing_room_days,d.authored_readings,d.days_with_submission])cell(tr,item);
  });
  reportSection(selectedReportSection);
}
function reportDateDefaults(today){
  if($("report-start").value||$("report-end").value)return;
  const date=new Date(today+"T12:00:00Z");
  date.setUTCDate(date.getUTCDate()-6);
  $("report-start").value=date.toISOString().slice(0,10);
  $("report-end").value=today;
}
function reportBuildingOptions(){
  const buildings=[...new Set(rooms.map(r=>r.building))].sort();
  for(const id of ["report-building","report-sub-building"]){
    const select=$(id),chosen=select.value;
    select.replaceChildren();option(select,"","Все здания");
    for(const value of buildings)option(select,value,value);
    if(buildingScope&&!buildings.includes(buildingScope))option(select,buildingScope,buildingScope);
    select.value=buildingScope||(buildings.includes(chosen)?chosen:"");
    select.disabled=Boolean(buildingScope);
  }
}
function reportPersonOptions(){
  const select=$("report-sub-user"),old=select.value;
  select.replaceChildren();option(select,"","Выберите сотрудника MAX");
  for(const user of maxStaff)option(select,user.user_id,
    maxUserName(user)+" · MAX ID "+user.user_id);
  select.value=maxStaff.some(p=>p.user_id===old)?old:"";
}
function clearReport(message){
  reportsData=null;
  renderReport();
  $("report-status").textContent=message;
}
async function loadReport(){
  const request=++reportRequestId;
  const start=$("report-start").value,end=$("report-end").value;
  if(!start||!end){clearReport("Укажите обе даты отчёта.");return;}
  const query=new URLSearchParams({start,end,building:$("report-building").value});
  clearReport("Формируем отчёт…");
  try{
    const result=await api("/api/reports?"+query);
    if(request!==reportRequestId)return;
    reportsData=result;
    renderReport();
  }catch(error){
    if(request===reportRequestId)clearReport("Не удалось сформировать отчёт. "+error.message);
  }
}
function reportExport(type){
  if(!reportsData){toast("Сначала сформируйте отчёт",true);return;}
  const query=new URLSearchParams({
    start:reportsData.from,end:reportsData.to,
    building:reportsData.building
  });
  const link=el("a");
  link.href="/api/reports/export."+type+"?"+query;
  link.download="termokontur-report."+type;
  document.body.append(link);link.click();link.remove();
}
function renderReportSubscriptions(schedules){
  const list=$("report-schedules");list.replaceChildren();
  if(!schedules.length){
    list.append(el("p","hint","Рассылки не настроены. Добавьте первое расписание."));
    return;
  }
  const days=["Пн","Вт","Ср","Чт","Пт","Сб","Вс"];
  for(const item of schedules){
    const row=el("div","report-schedule-entry"),info=el("div","report-schedule-details");
    const title=item.recipient+" · "+(item.building||"Все здания");
    info.append(el("strong","",title));
    info.append(el("small","",(item.frequency==="daily"?"Ежедневно":"Еженедельно, "+days[item.weekday])+
      " · "+item.send_time+" · "+(item.enabled?"Включено":"Выключено")+
      " · Отправок: "+item.periods_queued+(item.periods_waiting?" · Ждут выгрузку: "+item.periods_waiting:"")));
    if(item.last_delivered_at)info.append(el("small","","Последнее принято MAX: "+when(item.last_delivered_at)));
    const actions=el("div","report-schedule-actions");
    const toggle=el("button","btn secondary",item.enabled?"Выключить":"Включить");
    toggle.type="button";onOperation(toggle,"Обновляем расписание…",async()=>{
      try{
        await api("/api/reports/subscriptions/"+item.id,"PATCH",{enabled:!item.enabled});
        await loadReportSubscriptions();toast("Расписание обновлено");
      }catch(error){toast(error.message,true);}
    });
    const remove=el("button","btn secondary","Удалить");
    remove.type="button";onOperation(remove,"Удаляем расписание…",async()=>{
      if(!confirm("Удалить расписание отчётов для «"+title+"»?"))return;
      try{
        await api("/api/reports/subscriptions/"+item.id,"DELETE");
        await loadReportSubscriptions();toast("Расписание удалено");
      }catch(error){toast(error.message,true);}
    });
    actions.append(toggle,remove);row.append(info,actions);list.append(row);
  }
}
async function loadReportSubscriptions(){
  const list=await api("/api/reports/subscriptions");
  renderReportSubscriptions(list);
}
async function loadReports(){
  if(!$("report-start").value&&!$("report-end").value){
    const today=daily?.local_date||(await api("/api/daily")).local_date;
    reportDateDefaults(today);
  }
  reportBuildingOptions();
  if(buildingScope)await loadReport();
  else{
    reportPersonOptions();
    await Promise.all([loadReport(),loadReportSubscriptions()]);
  }
}
