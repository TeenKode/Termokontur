"use strict";
let sensorDevices=[],sensorSelected=null,sensorRecords=[],sensorConfig=null,sensorLoadId=0,sensorRefreshId=0;
const sensorPageSize=100;
const sensorFirmwareBase="/api/sensors/firmware/bundled";
const sensorStatus={not_enrolled:"Ожидает настройки",waiting:"Ожидает первых данных",recent:"Данные получены",sync_incomplete:"Выгрузка не завершена",measurement_unavailable:"Последний замер не вошёл в отчёт",stale:"Давно нет связи",low_battery:"Низкое напряжение аккумулятора",disabled:"Подключение отключено",retired:"Архив",rejected:"Доступ отклонён",authorization_pending:"Требуется разрешение"};
const sensorSampleIssue={sensor_crc:"Ошибка CRC при измерении",sensor_io:"Измерительный модуль не ответил",unknown_time:"У замера нет достоверной даты",outside_report_range:"Температура вне допустимого диапазона отчёта"};
const sensorTime={unknown:"Дата неизвестна",sntp:"Сервер времени",server:"Сервер",rtc:"Часы устройства"};
const sensorConfigFields=[
  ["measure_s","Интервал измерений, с",1,604800],["send_s","Интервал передачи, с",1,604800],
  ["cool_s","Пауза после зарядки, с (0 — без паузы)",0,86400],["wifi_budget_s","Время на подключение Wi-Fi, с",1,600],
  ["cutoff_mv","Порог отключения, мВ",3000,4200],["wifi_min_mv","Минимум для Wi-Fi, мВ (0 — без отдельного порога)",0,4200],
  ["ota_min_mv","Минимум для OTA, мВ (0 — от 3300 мВ)",0,4200],["tx_qdbm","Мощность Wi-Fi, четверти dBm",8,78],
  ["adc_scale_ppm","Коэффициент делителя АЦП, ppm",1600000,2400000]
];
function renderSensorConfigFields(container,config){
  container.replaceChildren();
  for(const [key,label,min,max] of sensorConfigFields){
    const field=el("label","",label),input=el("input");
    input.type="number";input.name=key;input.min=min;input.max=max;input.required=true;input.value=config[key];
    field.append(input);container.append(field);
  }
}
function readSensorConfig(form,base,enabled){
  const config={...base,enabled};
  for(const [key] of sensorConfigFields)config[key]=Number(form.elements[key].value);
  if(config.wifi_min_mv)config.wifi_min_mv=Math.max(config.wifi_min_mv,config.cutoff_mv);
  config.ota_min_mv=Math.max(config.ota_min_mv,config.cutoff_mv,3300);
  return config;
}
function sensorWhen(epoch){return epoch?when(new Date(epoch*1000).toISOString()):"—";}
function sensorStatusText(device){
  const status=sensorStatus[device.status]||device.status;
  return device.sync_incomplete&&["stale","low_battery"].includes(device.status)
    ?status+" · "+sensorStatus.sync_incomplete:status;
}
function renderSensorSyncTimes(target,device){
  const reading=device.last_valid_at?temperature(device.last_valid_temperature)+" · "+sensorWhen(device.last_valid_at):"—";
  target.replaceChildren(
    el("dt","","Последняя связь"),el("dd","",sensorWhen(device.last_seen)),
    el("dt","","Полная выгрузка"),el("dd","",sensorWhen(device.last_complete_sync)),
    el("dt","","Последний корректный замер"),el("dd","",reading));
}
function sensorSyncNotes(device){
  const notes=[];
  if(sensorSampleIssue[device.measurement_issue])notes.push(sensorSampleIssue[device.measurement_issue]);
  if(device.config_pending&&device.enabled)notes.push("Настройки ожидают применения");
  return notes.join(" · ");
}
function sensorButton(label,action){const b=el("button","btn secondary",label);b.type="button";onOperation(b,"Загружаем карточку датчика…",action);return b;}
function sensorRoomOptions(select,chosen){select.replaceChildren();for(const r of rooms)option(select,String(r.id),r.building+" · "+r.name);select.value=String(chosen||rooms[0]?.id||"");}
function tkSensorSearchKey(d){return [d.name,d.building,d.room_name,d.id,d.firmware].filter(Boolean).join(" ").toLocaleLowerCase("ru");}
function filterSensorPresentation(){
  const input=$("tk-sensor-search"),empty=$("tk-sensor-no-match");
  if(!input||!empty)return;
  const query=input.value.trim().toLocaleLowerCase("ru");
  const cards=[...document.querySelectorAll("#sensor-cards [data-tk-search]")];
  for(const node of document.querySelectorAll("#sensor-cards [data-tk-search], #tk-sensor-rows [data-tk-search]"))node.hidden=!node.dataset.tkSearch.includes(query);
  empty.hidden=!query||!cards.length||cards.some(card=>!card.hidden);
}
$("tk-sensor-search")?.addEventListener("input",filterSensorPresentation);
async function refreshSensors(){
  const request=++sensorRefreshId;
  $("sensor-state").textContent="Загружаем датчики…";
  try{
    const [devices,active,mqtt,claims]=await Promise.all([api("/api/sensors/devices"),api("/api/rooms"),api("/api/sensors/mqtt"),api("/api/sensors/claims")]);
    if(request!==sensorRefreshId)return;
    sensorDevices=devices;rooms=active;
    if(typeof renderSensorClaims==="function")renderSensorClaims(claims);
    if(typeof refreshInstaller==="function")refreshInstaller().catch(()=>{});
    const cards=$("sensor-cards"),archive=$("sensor-archive-cards");cards.replaceChildren();archive.replaceChildren();
    const archived=d=>!d.enabled||["retired","rejected","authorization_pending"].includes(d.status);
    const activeDevices=devices.filter(d=>!archived(d));
    const rows=$("tk-sensor-rows");
    if(rows){rows.replaceChildren();for(const d of activeDevices){const tr=el("tr");tr.dataset.tkSearch=tkSensorSearchKey(d);for(const v of [d.name,d.building+" · "+d.room_name,sensorStatusText(d),d.last_valid_at?temperature(d.last_valid_temperature):"—",d.battery_mv?Number(d.battery_mv/1000).toFixed(2)+" В":"—",sensorWhen(d.last_seen),d.firmware||"—"])tr.append(el("td","",v));const action=el("td");action.append(sensorButton("Открыть",()=>openSensor(d.id,true)));tr.append(action);rows.append(tr);}if(!activeDevices.length){const tr=el("tr"),td=el("td","muted","Нет активных датчиков");td.colSpan=8;tr.append(td);rows.append(tr);}}
    const archivedDevices=devices.filter(archived);
    $("sensor-archive").hidden=!archivedDevices.length;
    $("sensor-archive-title").textContent="Архив и отключённые ("+archivedDevices.length+")";
    for(const d of devices){
      const card=el("article","panel sensor-card"),name=el("h2","",d.name);
      const times=el("dl","sensor-sync-times");renderSensorSyncTimes(times,d);
      card.dataset.tkSearch=tkSensorSearchKey(d);
      const telemetry=el("div","tk-sensor-telemetry");
      const temp=el("div","tk-sensor-metric");temp.append(el("small","","Последний корректный замер"),el("span","tk-sensor-temperature",d.last_valid_at?temperature(d.last_valid_temperature):"—"));
      const battery=el("div","tk-sensor-metric");battery.append(el("small","","Напряжение"),el("span","tk-sensor-voltage",d.battery_mv?Number(d.battery_mv/1000).toLocaleString("ru-RU",{minimumFractionDigits:2,maximumFractionDigits:2})+" В":"—"));
      telemetry.append(temp,battery);
      card.append(name,el("p","muted",d.building+" · "+d.room_name+(d.status==="retired"?"":" · ESP32-C3")),el("strong","",sensorStatusText(d)),
        telemetry,times,
        sensorButton("Открыть",()=>openSensor(d.id,true)));(archived(d)?archive:cards).append(card);
      const notes=sensorSyncNotes(d);
      if(notes)card.insertBefore(el("p","muted",notes),card.lastChild);
      if(d.next_sync_at&&d.status!=="stale")card.insertBefore(el("p","muted","Следующая связь около "+sensorWhen(d.next_sync_at)),card.lastChild);
    }
    filterSensorPresentation();
    $("sensor-state").textContent=activeDevices.length?"Датчиков: "+activeDevices.length:archivedDevices.length?"Нет активных датчиков. История доступна в архиве.":"Подключите первый датчик по USB или по коду.";
    // Do not overwrite a partially edited MQTT form during refresh.
    if($("sensor-mqtt-form").dataset.dirty!=="true"){
      $("sensor-mqtt-enabled").checked=mqtt.enabled;$("sensor-mqtt-host").value=mqtt.host;$("sensor-mqtt-port").value=mqtt.port;
      $("sensor-mqtt-tls").checked=mqtt.tls;$("sensor-mqtt-user").value=mqtt.username;$("sensor-mqtt-prefix").value=mqtt.discovery_prefix;
    }
    $("sensor-mqtt-state").textContent=(mqtt.enabled?"MQTT включён":"MQTT выключен")+" · в очереди: "+mqtt.outbox.length+(mqtt.outbox.some(x=>x.error)?" · есть ошибки доставки":"");
  }catch(e){if(request===sensorRefreshId){$("sensor-state").textContent="Ошибка: "+e.message;toast(e.message,true);}}
}
let tkSensorTrigger=null;
$("tk-sensor-close")?.addEventListener("click",()=>{
  sensorSelected=null;sensorLoadId++;$("sensor-detail").hidden=true;
  if(tkSensorTrigger?.isConnected&&!tkSensorTrigger.closest("[hidden]"))tkSensorTrigger.focus();else $("tk-sensor-view")?.focus();
});
async function openSensor(id,reveal=false){
  if(reveal)tkSensorTrigger=document.activeElement;
  const request=++sensorLoadId;
  try{
    const [records,events,firmwares,bundle]=await Promise.all([api("/api/sensors/devices/"+id+"/records?limit="+sensorPageSize),api("/api/sensors/devices/"+id+"/events"),api("/api/sensors/firmware"),api(sensorFirmwareBase)]);
    if(request!==sensorLoadId)return;
    const d=sensorDevices.find(x=>x.id===id);if(!d)return;
    sensorSelected=id;sensorRecords=records;sensorConfig={...d.config};
    $("sensor-detail").hidden=false;$("sensor-title").textContent=d.name;
    const retired=d.status==="retired";
    $("sensor-detail-state").textContent=sensorStatusText(d)+" · "+d.id+" · "+(retired?"Поддержка модели завершена":"ESP32-C3")+" · "+(d.sensor_type||"sht4x").toUpperCase()+" · прошивка "+(d.firmware||"—")+" · настройки "+d.config_version+" / "+d.config.version+" · потеряно записей: "+d.lost;
    renderSensorSyncTimes($("sensor-sync-times"),d);
    const notes=$("sensor-sync-notes");notes.textContent=sensorSyncNotes(d);notes.hidden=!notes.textContent;
    for(const id of ["sensor-device-form","sensor-config-details","sensor-ota-details"])$(id).hidden=retired;
    $("sensor-rekey").textContent=d.identity_fingerprint?"Отозвать доступ":"Перевыпустить ключ";
    $("sensor-edit-name").value=d.name;sensorRoomOptions($("sensor-edit-room"),d.room_id);$("sensor-enabled").checked=Boolean(d.enabled);
    const configForm=$("sensor-config-form");renderSensorConfigFields(configForm,d.config);
    const enabled=el("label","archive-toggle",["ds18b20","bmp280"].includes(d.sensor_type)?"Измерять температуру":"Измерять температуру и влажность"),checkbox=el("input");checkbox.type="checkbox";checkbox.name="enabled";checkbox.checked=d.config.enabled;enabled.prepend(checkbox);configForm.append(enabled);
    const save=el("button","btn primary","Отправить настройки");configForm.append(save);
    renderSensorRecords();
    $("sensor-records-more").hidden=records.length<sensorPageSize;
    $("sensor-events").replaceChildren(...events.map(x=>el("li","",sensorWhen(x.at)+" · "+x.kind+": "+x.detail)));
    const select=$("sensor-firmware");
    select.replaceChildren();option(select,"","Выберите версию");
    const available=firmwares.filter(f=>f.model===(d.model||"c3-sht40-v1"));
    const fromBundle=fw=>bundle&&fw.version===bundle.version&&fw.sha256===bundle.esp32.sha256;
    const bundledLabel=bundle?bundle.version+" · из комплекта панели":"";
    if(!retired&&bundle&&!available.some(fromBundle))option(select,"bundled:"+bundle.version,bundledLabel);
    for(const fw of available)option(select,String(fw.id),fromBundle(fw)?bundledLabel:fw.version+" · "+Math.round(fw.size/1024)+" КиБ");
    select.value=String(d.target_firmware||"");
    $("sensor-ota-assign").disabled=!select.value;
    $("sensor-ota-cancel").disabled=!d.target_firmware;
    $("sensor-ota-state").textContent="Установлено: "+(d.firmware||"неизвестно")+" · неудачных попыток: "+d.ota_failures+". Обновление начнётся после передачи данных при достаточном заряде.";
    drawSensorChart();
    if(reveal){
      $("sensor-title").focus({preventScroll:true});
      $("sensor-detail").scrollIntoView({block:"nearest"});
    }
  }catch(e){toast(e.message,true);}
}
function renderSensorRecords(){
  const body=$("sensor-records");body.replaceChildren();
  for(const r of sensorRecords){const tr=el("tr");cell(tr,r.seq);cell(tr,sensorWhen(r.measured_at));cell(tr,r.temperature===null?"—":r.temperature.toFixed(2));cell(tr,r.humidity===null?"—":r.humidity.toFixed(1));cell(tr,r.battery_mv?Number(r.battery_mv/1000).toFixed(2)+" В":"—");cell(tr,(sensorTime[r.time_quality]||r.time_quality)+(r.error?" · "+r.error:""));body.append(tr);}
}
onOperation($("sensor-records-more"),"Загружаем предыдущие измерения…",async()=>{
  if(!sensorSelected||!sensorRecords.length)return;
  const selected=sensorSelected,request=sensorLoadId;
  const page=await api("/api/sensors/devices/"+selected+"/records?limit="+sensorPageSize+"&before_seq="+sensorRecords.at(-1).seq);
  if(request!==sensorLoadId||selected!==sensorSelected)return;
  sensorRecords.push(...page);
  renderSensorRecords();
  $("sensor-records-more").hidden=page.length<sensorPageSize;
  drawSensorChart();
});
function drawSensorChart(){
  const canvas=$("sensor-chart");
  if(!sensorSelected||$("view-sensors").hidden)return;
  const points=sensorRecords.slice(0,5000).filter(r=>r.measured_at&&!r.error).slice().sort((a,b)=>a.measured_at-b.measured_at||a.seq-b.seq);
  const colors=getComputedStyle(canvas);
  const hasTemperature=points.some(p=>Number.isFinite(p.temperature)),hasHumidity=points.some(p=>Number.isFinite(p.humidity));
  show("sensor-temperature-key",hasTemperature);show("sensor-humidity-key",hasHumidity);
  renderTrend(canvas,{
    points:points.map(p=>({time:p.measured_at,label:sensorWhen(p.measured_at),title:sensorWhen(p.measured_at)})),
    series:[
      {name:"Температура",unit:"°C",color:colors.getPropertyValue("--sensor-temperature").trim(),values:points.map(p=>p.temperature)},
      {name:"Влажность",unit:"%",color:colors.getPropertyValue("--sensor-humidity").trim(),dash:[7,4],values:points.map(p=>p.humidity)}
    ],
    contiguous:i=>{const from=points[i-1],to=points[i];return Boolean(from&&to&&to.seq===from.seq+1&&to.measured_at-from.measured_at<=2*to.interval_s);},
    empty:"Нет измерений с точной датой"
  });
}

function revealSensorCode(reply){$("sensor-code-help").textContent="Подключитесь к Wi-Fi датчика и откройте http://192.168.4.1. Имя сети и пароль указаны в USB-мониторе.";$("sensor-code-id").value=reply.device_id;$("sensor-code-value").value=reply.code;$("sensor-code-dialog").showModal();}
$("sensor-add").addEventListener("click",()=>{
  $("sensor-add-form").reset();
  sensorRoomOptions($("sensor-new-room"));
  show("sensor-new-room-hint",rooms.length===0);
  $("sensor-add-submit").disabled=rooms.length===0;
  $("sensor-add-dialog").showModal();
});
$("sensor-add-close").addEventListener("click",()=>$("sensor-add-dialog").close());
$("sensor-code-close").addEventListener("click",()=>$("sensor-code-dialog").close());
$("sensor-code-dialog").addEventListener("close",()=>{
  for(const id of ["sensor-code-id","sensor-code-value"])$(id).value="";
});
$("sensor-code-copy").addEventListener("click",async()=>{try{await navigator.clipboard.writeText($("sensor-code-id").value+"\n"+$("sensor-code-value").value);toast("Данные скопированы");}catch{toast("Выделите и скопируйте поля вручную",true);}});
onOperation($("sensor-add-form"),"Создаём код подключения…",async e=>{e.preventDefault();try{const reply=await api("/api/sensors/devices","POST",{name:$("sensor-new-name").value.trim(),room_id:Number($("sensor-new-room").value)});$("sensor-add-dialog").close();await refreshSensors();revealSensorCode(reply);}catch(e){toast(e.message,true);}});
onOperation($("sensor-refresh"),"Обновляем список датчиков…",async()=>{await refreshSensors();if(sensorSelected)await openSensor(sensorSelected);});
onOperation($("sensor-device-form"),"Сохраняем датчик…",async e=>{e.preventDefault();try{const enabled=$("sensor-enabled").checked;await api("/api/sensors/devices/"+sensorSelected,"PATCH",{name:$("sensor-edit-name").value.trim(),room_id:Number($("sensor-edit-room").value),enabled});await refreshSensors();if(enabled)await openSensor(sensorSelected);else{sensorSelected=null;sensorLoadId++;$("sensor-detail").hidden=true;}toast(enabled?"Изменения сохранены":"Датчик перемещён в архив, история сохранена");}catch(e){toast(e.message,true);}});
onOperation($("sensor-rekey"),"Отзываем доступ датчика…",async()=>{if(!confirm("Отозвать доступ датчика? Для восстановления настройте его по USB, подтвердите заявку или используйте новый код."))return;try{const reply=await api("/api/sensors/devices/"+sensorSelected+"/rekey","POST");if(reply.requires_authorization){await refreshSensors();toast("Доступ отозван. Настройте датчик по USB или подтвердите его новую заявку.");}else revealSensorCode(reply);}catch(e){toast(e.message,true);}});
onOperation($("sensor-config-form"),"Сохраняем настройки датчика…",async e=>{e.preventDefault();try{const body=readSensorConfig(e.target,sensorConfig,e.target.elements.enabled.checked);await api("/api/sensors/devices/"+sensorSelected+"/config","PUT",body);await refreshSensors();await openSensor(sensorSelected);toast("Настройки применятся при следующем подключении");}catch(e){toast(e.message,true);}});
$("sensor-firmware").addEventListener("change",()=>{
  $("sensor-ota-assign").disabled=!$("sensor-firmware").value;
});
onOperation($("sensor-ota-form"),"Назначаем обновление прошивки…",async e=>{
  e.preventDefault();
  const choice=$("sensor-firmware").value;
  if(!choice)return;
  const query=choice.startsWith("bundled:")?{bundled_version:choice.slice("bundled:".length)}:{firmware_id:choice};
  try{await api("/api/sensors/devices/"+sensorSelected+"/ota?"+new URLSearchParams(query),"PUT");await refreshSensors();await openSensor(sensorSelected);toast("Обновление назначено");}catch(e){toast(e.message,true);}
});
onOperation($("sensor-ota-cancel"),"Отменяем обновление прошивки…",async()=>{try{await api("/api/sensors/devices/"+sensorSelected+"/ota","PUT");await refreshSensors();await openSensor(sensorSelected);toast("Обновление отменено");}catch(e){toast(e.message,true);}});
onOperation($("sensor-upload-form"),"Загружаем образ прошивки…",async event=>{
  await api("/api/sensors/firmware","POST",new FormData(event.currentTarget));
  await openSensor(sensorSelected);
  toast("Прошивка загружена");
});
$("sensor-mqtt-form").addEventListener("input",event=>{event.currentTarget.dataset.dirty="true";});
onOperation($("sensor-mqtt-form"),"Сохраняем настройки MQTT…",async event=>{
  event.preventDefault();
  const form=event.currentTarget;
  try{
    await api("/api/sensors/mqtt","PUT",{
      enabled:$("sensor-mqtt-enabled").checked,host:$("sensor-mqtt-host").value.trim(),
      port:Number($("sensor-mqtt-port").value),tls:$("sensor-mqtt-tls").checked,
      username:$("sensor-mqtt-user").value,password:$("sensor-mqtt-pass").value||null,
      discovery_prefix:$("sensor-mqtt-prefix").value.trim(),
    });
    $("sensor-mqtt-pass").value="";
    form.dataset.dirty="false";
    await refreshSensors();
    toast("Настройки MQTT сохранены");
  }catch(error){toast(error.message,true);}
});
$("sensor-export").addEventListener("click",()=>{if(!sensorSelected)return;const link=el("a");link.href="/api/sensors/devices/"+encodeURIComponent(sensorSelected)+"/records.csv";link.download=sensorSelected+".csv";link.click();});
window.addEventListener("resize",drawSensorChart);
document.addEventListener("termokontur:themechange",drawSensorChart);
