"use strict";
let sensorDevices=[],sensorSelected=null,sensorRecords=[],sensorConfig=null,sensorLoadId=0;
const sensorStatus={not_enrolled:"Ожидает настройки",waiting:"Ожидает первых данных",recent:"Данные получены",stale:"Давно нет связи",low_battery:"Низкое напряжение аккумулятора",disabled:"Подключение отключено"};
const sensorTime={unknown:"Дата неизвестна",sntp:"Сервер времени",server:"Сервер",rtc:"Часы устройства"};
const sensorConfigFields=[
  ["measure_s","Интервал измерений, с",60,86400],["send_s","Интервал передачи, с",600,604800],
  ["cool_s","Пауза после зарядки, с",300,86400],["wifi_budget_s","Время на подключение Wi-Fi, с",15,120],
  ["cutoff_mv","Порог отключения, мВ",3100,3800],["wifi_min_mv","Минимум для Wi-Fi, мВ",3300,4100],
  ["ota_min_mv","Минимум для обновления, мВ",3600,4200],["tx_qdbm","Мощность Wi-Fi, четверти dBm",8,78],
  ["adc_scale_ppm","Коэффициент делителя АЦП, ppm",1600000,2400000]
];
function sensorWhen(epoch){return epoch?when(new Date(epoch*1000).toISOString()):"—";}
function sensorButton(label,action){const b=el("button","btn secondary",label);b.type="button";b.addEventListener("click",action);return b;}
function sensorRoomOptions(select,chosen){select.replaceChildren();for(const r of rooms)option(select,String(r.id),r.building+" · "+r.name);select.value=String(chosen||rooms[0]?.id||"");}
async function refreshSensors(){
  $("sensor-state").textContent="Загружаем датчики…";
  try{
    const [devices,active,mqtt,claims]=await Promise.all([api("/api/sensors/devices"),api("/api/rooms"),api("/api/sensors/mqtt"),api("/api/sensors/claims")]);
    sensorDevices=devices;rooms=active;
    if(typeof renderSensorClaims==="function")renderSensorClaims(claims);
    if(typeof refreshInstaller==="function")refreshInstaller().catch(()=>{});
    const cards=$("sensor-cards");cards.replaceChildren();
    for(const d of devices){
      const card=el("article","panel sensor-card"),name=el("h2","",d.name);
      card.append(name,el("p","muted",d.building+" · "+d.room_name),el("strong","",sensorStatus[d.status]||d.status),
        el("p","",(d.battery_mv?Number(d.battery_mv/1000).toFixed(2)+" В":"Напряжение неизвестно")+" · "+sensorWhen(d.last_seen)),
        sensorButton("Открыть",()=>openSensor(d.id)));cards.append(card);
      if(d.next_sync_at&&d.status!=="stale")card.insertBefore(el("p","muted","Следующая связь около "+sensorWhen(d.next_sync_at)),card.lastChild);
    }
    $("sensor-state").textContent=devices.length?"Датчиков: "+devices.length+". Указано состояние последнего подключения.":"Подключите первый датчик по USB или по коду.";
    // Do not overwrite a partially edited MQTT form during refresh.
    if(!$("sensor-mqtt-form").contains(document.activeElement)){
      $("sensor-mqtt-enabled").checked=mqtt.enabled;$("sensor-mqtt-host").value=mqtt.host;$("sensor-mqtt-port").value=mqtt.port;
      $("sensor-mqtt-tls").checked=mqtt.tls;$("sensor-mqtt-user").value=mqtt.username;$("sensor-mqtt-prefix").value=mqtt.discovery_prefix;
    }
    $("sensor-mqtt-state").textContent=(mqtt.enabled?"MQTT включён":"MQTT выключен")+" · в очереди: "+mqtt.outbox.length+(mqtt.outbox.some(x=>x.error)?" · есть ошибки доставки":"");
  }catch(e){$("sensor-state").textContent="Ошибка: "+e.message;toast(e.message,true);}
}
async function openSensor(id){
  const request=++sensorLoadId;
  try{
    const [records,events,firmwares]=await Promise.all([api("/api/sensors/devices/"+id+"/records?limit=5000"),api("/api/sensors/devices/"+id+"/events"),api("/api/sensors/firmware")]);
    if(request!==sensorLoadId)return;
    const d=sensorDevices.find(x=>x.id===id);if(!d)return;
    sensorSelected=id;sensorRecords=records;sensorConfig={...d.config};
    $("sensor-detail").hidden=false;$("sensor-title").textContent=d.name;
    $("sensor-detail-state").textContent=sensorStatus[d.status]+" · "+d.id+" · "+(d.sensor_type||"sht4x").toUpperCase()+" · прошивка "+(d.firmware||"—")+" · настройки "+d.config_version+" / "+d.config.version+" · потеряно записей: "+d.lost;
    $("sensor-rekey").textContent=d.identity_fingerprint?"Отозвать доступ":"Перевыпустить ключ";
    $("sensor-edit-name").value=d.name;sensorRoomOptions($("sensor-edit-room"),d.room_id);$("sensor-enabled").checked=Boolean(d.enabled);
    const configForm=$("sensor-config-form");configForm.replaceChildren();
    for(const [key,label,min,max] of sensorConfigFields){const l=el("label","",label),input=el("input");input.type="number";input.name=key;input.min=min;input.max=max;input.value=d.config[key];input.required=true;l.append(input);configForm.append(l);}
    const enabled=el("label","archive-toggle",["ds18b20","bmp280"].includes(d.sensor_type)?"Измерять температуру":"Измерять температуру и влажность"),checkbox=el("input");checkbox.type="checkbox";checkbox.name="enabled";checkbox.checked=d.config.enabled;enabled.prepend(checkbox);configForm.append(enabled);
    const save=el("button","btn primary","Отправить настройки");configForm.append(save);
    const body=$("sensor-records");body.replaceChildren();
    for(const r of records.slice(0,100)){const tr=el("tr");cell(tr,r.seq);cell(tr,sensorWhen(r.measured_at));cell(tr,r.temperature===null?"—":r.temperature.toFixed(2));cell(tr,r.humidity===null?"—":r.humidity.toFixed(1));cell(tr,(r.battery_mv/1000).toFixed(2)+" В");cell(tr,(sensorTime[r.time_quality]||r.time_quality)+(r.error?" · "+r.error:""));body.append(tr);}
    $("sensor-events").replaceChildren(...events.map(x=>el("li","",sensorWhen(x.at)+" · "+x.kind+": "+x.detail)));
    const select=$("sensor-firmware");select.replaceChildren();option(select,"","Выберите версию");for(const fw of firmwares)option(select,String(fw.id),fw.version+" · "+Math.round(fw.size/1024)+" КиБ");select.value=String(d.target_firmware||"");
    $("sensor-ota-state").textContent="Установлено: "+(d.firmware||"неизвестно")+" · неудачных попыток: "+d.ota_failures+". Обновление начнётся после передачи данных при достаточном заряде.";
    drawSensorChart();
  }catch(e){toast(e.message,true);}
}
function drawSensorChart(){
  const canvas=$("sensor-chart");if(!sensorSelected||$("view-sensors").hidden)return;
  const points=sensorRecords.filter(r=>r.measured_at&&!r.error).reverse();const ctx=canvas.getContext("2d");
  const width=Math.max(280,canvas.parentElement.clientWidth-20),height=260,ratio=window.devicePixelRatio||1;canvas.width=width*ratio;canvas.height=height*ratio;ctx.scale(ratio,ratio);ctx.clearRect(0,0,width,height);
  const dark=document.documentElement.dataset.theme==="dark";ctx.fillStyle=dark?"#bbcad5":"#526271";ctx.font="12px system-ui";
  if(!points.length){ctx.fillText("Нет измерений с точной датой",20,40);return;}
  const first=points[0].measured_at,last=points.at(-1).measured_at,range=Math.max(1,last-first);
  const lo=Math.floor(Math.min(...points.map(x=>x.temperature))-1),hi=Math.ceil(Math.max(...points.map(x=>x.temperature))+1);
  const left=45,right=width-42,top=25,bottom=220;
  const hasHumidity=points.some(p=>Number.isFinite(p.humidity));
  ctx.fillText(lo+" °C",2,bottom);ctx.fillText(hi+" °C",2,top);
  if(hasHumidity){ctx.fillText("100%",right+3,top);ctx.fillText("0%",right+3,bottom);}
  ctx.fillText(sensorWhen(first),left,248);if(points.length>1)ctx.fillText(sensorWhen(last),Math.max(left,right-110),248);
  const series=[["temperature","#14a786",lo,hi]];
  if(hasHumidity)series.push(["humidity","#508de2",0,100]);
  for(const [key,color,min,max] of series){
    ctx.strokeStyle=color;ctx.lineWidth=2;ctx.beginPath();let previous=null;
    for(const p of points){if(p[key]===null||!Number.isFinite(p[key])){previous=null;continue;}const x=left+(p.measured_at-first)/range*(right-left),y=bottom-(p[key]-min)/(max-min)*(bottom-top);
      if(previous===null||p.seq!==previous.seq+1||p.measured_at-previous.measured_at>2*p.interval_s)ctx.moveTo(x,y);else ctx.lineTo(x,y);previous=p;
    }ctx.stroke();
  }
}
function revealSensorCode(reply){$("sensor-code-id").value=reply.device_id;$("sensor-code-value").value=reply.code;$("sensor-code-dialog").showModal();}
$("sensor-add").addEventListener("click",()=>{sensorRoomOptions($("sensor-new-room"));$("sensor-add-dialog").showModal();});
$("sensor-add-close").addEventListener("click",()=>$("sensor-add-dialog").close());
$("sensor-code-close").addEventListener("click",()=>{$("sensor-code-dialog").close();$("sensor-code-value").value="";});
$("sensor-code-copy").addEventListener("click",async()=>{try{await navigator.clipboard.writeText($("sensor-code-id").value+"\n"+$("sensor-code-value").value);toast("Данные скопированы");}catch{toast("Выделите и скопируйте поля вручную",true);}});
$("sensor-add-form").addEventListener("submit",async e=>{e.preventDefault();try{const reply=await api("/api/sensors/devices","POST",{name:$("sensor-new-name").value.trim(),room_id:Number($("sensor-new-room").value)});$("sensor-add-dialog").close();await refreshSensors();revealSensorCode(reply);}catch(e){toast(e.message,true);}});
$("sensor-refresh").addEventListener("click",async()=>{await refreshSensors();if(sensorSelected)await openSensor(sensorSelected);});
$("sensor-device-form").addEventListener("submit",async e=>{e.preventDefault();try{await api("/api/sensors/devices/"+sensorSelected,"PATCH",{name:$("sensor-edit-name").value.trim(),room_id:Number($("sensor-edit-room").value),enabled:$("sensor-enabled").checked});await refreshSensors();await openSensor(sensorSelected);toast("Изменения сохранены");}catch(e){toast(e.message,true);}});
$("sensor-rekey").addEventListener("click",async()=>{if(!confirm("Отозвать доступ датчика? Для восстановления настройте его по USB, подтвердите заявку или используйте новый код."))return;try{const reply=await api("/api/sensors/devices/"+sensorSelected+"/rekey","POST");if(reply.requires_authorization){await refreshSensors();toast("Доступ отозван. Настройте датчик по USB или подтвердите его новую заявку.");}else revealSensorCode(reply);}catch(e){toast(e.message,true);}});
$("sensor-config-form").addEventListener("submit",async e=>{e.preventDefault();try{const body={...sensorConfig};for(const [key] of sensorConfigFields)body[key]=Number(e.target.elements[key].value);body.enabled=e.target.elements.enabled.checked;await api("/api/sensors/devices/"+sensorSelected+"/config","PUT",body);await refreshSensors();await openSensor(sensorSelected);toast("Настройки применятся при следующем подключении");}catch(e){toast(e.message,true);}});
$("sensor-ota-form").addEventListener("submit",async e=>{e.preventDefault();if(!$("sensor-firmware").value)return;try{await api("/api/sensors/devices/"+sensorSelected+"/ota?firmware_id="+encodeURIComponent($("sensor-firmware").value),"PUT");await refreshSensors();await openSensor(sensorSelected);toast("Обновление назначено");}catch(e){toast(e.message,true);}});
$("sensor-ota-cancel").addEventListener("click",async()=>{try{await api("/api/sensors/devices/"+sensorSelected+"/ota","PUT");await refreshSensors();await openSensor(sensorSelected);toast("Обновление отменено");}catch(e){toast(e.message,true);}});
$("sensor-upload-form").addEventListener("submit",async e=>{e.preventDefault();const finish=beginRequest("/api/sensors/firmware","POST");try{const form=new FormData();form.append("file",$("sensor-image").files[0]);const r=await fetch("/api/sensors/firmware",{method:"POST",credentials:"same-origin",body:form});if(!r.ok){const error=await r.json();throw Error(typeof error.detail==="string"?error.detail:"Не удалось загрузить образ");}await openSensor(sensorSelected);toast("Прошивка загружена");}catch(e){toast(e.message,true);}finally{finish();}});
$("sensor-mqtt-form").addEventListener("submit",async e=>{e.preventDefault();try{await api("/api/sensors/mqtt","PUT",{enabled:$("sensor-mqtt-enabled").checked,host:$("sensor-mqtt-host").value.trim(),port:Number($("sensor-mqtt-port").value),tls:$("sensor-mqtt-tls").checked,username:$("sensor-mqtt-user").value,password:$("sensor-mqtt-pass").value||null,discovery_prefix:$("sensor-mqtt-prefix").value.trim()});$("sensor-mqtt-pass").value="";await refreshSensors();toast("Настройки MQTT сохранены");}catch(e){toast(e.message,true);}});
$("sensor-export").addEventListener("click",()=>{const keys=["seq","measured_at","received_at","time_quality","temperature","humidity","battery_mv","error"],lines=[keys.join(","),...sensorRecords.map(r=>keys.map(k=>JSON.stringify(r[k]??"")).join(","))];const url=URL.createObjectURL(new Blob(["\ufeff"+lines.join("\r\n")],{type:"text/csv;charset=utf-8"}));const a=el("a");a.href=url;a.download=sensorSelected+".csv";a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);});
window.addEventListener("resize",drawSensorChart);
document.addEventListener("termokontur:themechange",drawSensorChart);
