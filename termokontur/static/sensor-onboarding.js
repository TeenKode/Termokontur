"use strict";
let usbInstallers=[],usbDefaults=null,usbBusy=false,usbExpectedHardware=null;
function usbServerBootstrapVersion(version){const m=/^(\d+)\.(\d+)\.(\d+)$/.exec(version||"");return Boolean(m&&(Number(m[1])>0||Number(m[2])>1||(Number(m[2])===1&&Number(m[3])>=5)));}
const usbSupported=()=>Boolean(window.isSecureContext&&navigator.serial);
const usbStatus=text=>{$("usb-status").textContent=text;};
function usbClearSecrets(){for(const id of ["usb-wifi-password","usb-setup-password","usb-setup-confirm"])$(id).value="";}
function usbSetBusy(value){usbBusy=value;for(const field of $("sensor-usb-form").querySelectorAll("input,select,textarea"))field.disabled=value||(["usb-setup-password","usb-setup-confirm"].includes(field.id)&&$("usb-setup-random").checked);for(const id of ["usb-flash","usb-configure","usb-close"])$(id).disabled=value||(!usbSupported()&&id!=="usb-close")||(id==="usb-flash"&&!usbInstallers.length);}
async function refreshInstaller(){
  const [bundle, uploaded]=await Promise.all([api(sensorFirmwareBase),api("/api/sensors/installer")]);
  const manual=uploaded&&usbServerBootstrapVersion(uploaded.version)?uploaded:null;
  $("sensor-bundled-status").textContent=bundle?"Доступна прошивка "+bundle.version+".":"Ожидаем комплект текущей версии.";
  $("sensor-firmware-downloads").hidden=!bundle;
  $("sensor-firmware-recovery").hidden=!!bundle;
  $("sensor-installer-status").textContent=manual?"Загружена версия "+manual.version+".":"Загрузите ZIP прошивки версии 0.1.5 или новее.";
  const select=$("usb-firmware"),previous=select.value;
  select.replaceChildren();
  usbInstallers=[];
  if(bundle){
    const base=sensorFirmwareBase+"/"+bundle.version;
    $("sensor-c3-ota-file").href=base+"/esp32-c3.bin";
    $("sensor-c3-factory-file").href=base+"/esp32-c3-factory.bin";
    const url=$("sensor-c3-factory-file").getAttribute("href");
    usbInstallers.push({...bundle.factory,url});
    option(select,url,bundle.version+" · из комплекта панели");
  }
  if(manual&&!(bundle?.version===manual.version&&bundle.factory.sha256===manual.sha256)){
    const url="/api/sensors/installer/"+manual.id+"/factory.bin";
    usbInstallers.push({...manual,url});
    option(select,url,manual.version+" · загружена вручную");
  }
  if($("sensor-usb-dialog").open&&usbInstallers.some(item=>item.url===previous))select.value=previous;
  $("usb-firmware-choice").hidden=!usbInstallers.length;
}
$("sensor-firmware-retry").addEventListener("click",async()=>{
  const button=$("sensor-firmware-retry");
  button.disabled=true;
  $("sensor-bundled-status").textContent="Загружаем комплект прошивки…";
  try {
    const result=await api(sensorFirmwareBase+"/retry","POST");
    if(result.status==="ready"){
      await refreshInstaller();
      if(sensorSelected)await openSensor(sensorSelected);
    }
  }catch(error){
    $("sensor-bundled-status").textContent=error.message;
    toast(error.message,true);
  }finally{
    button.disabled=false;
  }
});
function renderSensorClaims(claims){
  const target=$("sensor-claims");if(!target)return;target.replaceChildren();$("sensor-claims-panel").hidden=!claims.length;
  for(const claim of claims){
    const article=el("div","sensor-claim");
    article.append(el("strong","",claim.hardware_id+" · "+claim.sensor_type.toUpperCase()),el("p","hint","Ключ "+claim.fingerprint.slice(0,16).match(/.{4}/g).join("–")+" · "+(claim.state==="rejected"?"Отклонён":"Ожидает разрешения")+" · "+sensorWhen(claim.last_seen)));
    const form=el("form","sensor-form"),name=el("input"),room=el("select"),approve=el("button","btn primary","Разрешить"),reject=el("button","btn secondary","Отклонить");
    name.required=true;name.maxLength=100;name.value=claim.name||"";name.placeholder="Название датчика";name.setAttribute("aria-label","Название нового датчика");room.setAttribute("aria-label","Помещение нового датчика");sensorRoomOptions(room,claim.room_id);reject.type="button";
    form.append(name,room,approve,reject);article.append(form);target.append(article);
    form.addEventListener("submit",async e=>{e.preventDefault();try{await api("/api/sensors/claims/"+claim.fingerprint+"/approve","POST",{name:name.value.trim(),room_id:Number(room.value)});await refreshSensors();toast("Доступ разрешён. Ожидаем подключения датчика.");}catch(error){toast(error.message,true);}});
    reject.addEventListener("click",async()=>{try{await api("/api/sensors/claims/"+claim.fingerprint+"/reject","POST");await refreshSensors();}catch(error){toast(error.message,true);}});
  }
}
$("sensor-usb-open").addEventListener("click",async()=>{
  try{
    await refreshInstaller();usbDefaults=(await api("/api/sensors/usb/options")).config;
    sensorRoomOptions($("usb-room"));$("usb-server").value=location.origin;
    const fields=$("usb-config-fields");fields.replaceChildren();
    for(const [key,label,min,max] of sensorConfigFields){const wrap=el("label","",label),input=el("input");input.name=key;input.type="number";input.required=true;input.min=min;input.max=max;input.value=usbDefaults[key];wrap.append(input);fields.append(wrap);}
    $("sensor-usb-support").textContent=usbSupported()?"Настройки передаются подключённой плате по USB.":"Откройте панель по HTTPS в Chrome или Edge на компьютере. Для локальной разработки доступен localhost.";
    $("usb-identity").textContent="";$("usb-progress").hidden=true;$("usb-log").hidden=true;usbExpectedHardware=null;usbStatus("");usbSetBusy(false);$("sensor-usb-dialog").showModal();
  }catch(error){toast(error.message,true);}
});
function usbSettings(){
  const form=$("sensor-usb-form");if(!form.reportValidity())return null;
  const url=new URL($("usb-server").value);
  if(url.protocol!=="https:"||url.username||url.password||url.pathname!=="/"||url.search||url.hash)throw Error("Укажите HTTPS-адрес панели без пути и параметров: https://example.org");
  if(url.origin!==location.origin)throw Error("Для настройки откройте панель на указанном HTTPS-сервере");
  if(!$("usb-name").value.trim())throw Error("Укажите название датчика");
  const encoder=new TextEncoder(),password=$("usb-wifi-password").value;
  const length=encoder.encode(password).length;
  if(password&&(length<8||length>63))throw Error("Проверьте длину пароля Wi-Fi: 8–63 байта");
  if(encoder.encode($("usb-ssid").value).length>32)throw Error("Название Wi-Fi превышает 32 байта");
  if(!$("usb-setup-random").checked&&$("usb-setup-password").value!==$("usb-setup-confirm").value)throw Error("Пароли сети настройки не совпадают");
  const config={...usbDefaults,enabled:$("usb-measure-enabled").checked};
  for(const [key] of sensorConfigFields)config[key]=Number(form.elements[key].value);
  if(config.send_s<config.measure_s||config.cutoff_mv>config.wifi_min_mv||config.wifi_min_mv>config.ota_min_mv)throw Error("Передача должна быть не чаще измерений. Пороги напряжения: отключение ≤ Wi-Fi ≤ обновление.");
  return {name:$("usb-name").value.trim(),room_id:Number($("usb-room").value),sensor_type:$("usb-type").value,config,
    settings:{ssid:$("usb-ssid").value,password,server:url.origin,
      setup_password:$("usb-setup-password").value,setup_password_confirm:$("usb-setup-confirm").value,random_setup_password:$("usb-setup-random").checked}};
}
async function usbRun(flash){
  if(usbBusy)return;
  let serial=null,flashed=false;
  try{
    const data=usbSettings();if(!data)return;
    if(!usbSupported())throw Error("USB недоступен. Откройте панель по HTTPS в Chrome или Edge на компьютере.");
    if(flash&&!$("usb-new-board").checked)throw Error("Подтвердите удаление данных платы или нажмите «Настроить без прошивки».");
    const installer=usbInstallers.find(item=>item.url===$("usb-firmware").value);
    if(flash&&!installer)throw Error("Дождитесь загрузки комплекта панели или загрузите свой комплект USB");
    // Request permission while the click still has browser user activation.
    usbSetBusy(true);
    const port=await navigator.serial.requestPort({filters:[{usbVendorId:0x303a}]});
    const usb=await import("./sensor-usb.mjs");
    if(flash){
      usbStatus("Проверяем комплект и записываем прошивку. Не отключайте USB.");$("usb-progress").hidden=false;$("usb-progress").value=0;
      const response=await fetch(installer.url,{credentials:"same-origin"});
      if(!response.ok)throw Error("Не удалось получить комплект с сервера");
      usbExpectedHardware=await usb.flashFactory(port,await response.arrayBuffer(),installer,{
        checkBoard:hardware=>api("/api/sensors/usb/check/"+hardware),progress:value=>{$("usb-progress").value=value;},
        log:line=>{$("usb-log").hidden=false;$("usb-log").textContent=($("usb-log").textContent+String(line)+"\n").slice(-4000);}});
      flashed=true;usbStatus("Прошивка записана. Ожидаем запуска датчика…");
      await new Promise(resolve=>setTimeout(resolve,1500));
    }
    serial=new usb.SensorSerial(port);await serial.open();
    const hello=await serial.request("hello");
    const fp=await usb.verifyIdentity(hello,usbExpectedHardware);
    if(!usbServerBootstrapVersion(hello.firmware))throw Error("Для времени и сертификата от сервера обновите прошивку датчика до 0.1.5 или новее");
    if(hello.pending_records&&hello.sensor_type!==data.sensor_type)throw Error("Перед сменой типа передайте накопленные измерения на сервер");
    $("usb-identity").textContent="Плата "+hello.hardware_id+" · ключ "+fp.slice(0,16).match(/.{4}/g).join("–");
    const identity={public_key:hello.public_key,hardware_id:hello.hardware_id,sensor_type:data.sensor_type,firmware:hello.firmware};
    usbStatus("Проверяем плату и разрешаем подключение…");
    const challenge=await api("/api/sensors/auth/challenge","POST",identity);
    const signed=await serial.request("sign",{message:challenge.message});
    const approval=await api("/api/sensors/onboard","POST",{...identity,challenge:challenge.challenge,signature:signed.signature,name:data.name,room_id:data.room_id,config:data.config});
    usbStatus("Доступ выдан. Передаём настройки по USB…");
    const bootstrapResponse=await fetch(data.settings.server+"/api/sensors/bootstrap",{credentials:"omit",cache:"no-store"});
    if(!bootstrapResponse.ok)throw Error("Не удалось получить время и сертификат сервера");
    const bootstrap=await bootstrapResponse.json();
    if(!Number.isInteger(bootstrap.time)||bootstrap.time<1700000000||bootstrap.time>4102444800||typeof bootstrap.ca!=="string"||new TextEncoder().encode(bootstrap.ca).length>8000)throw Error("Сервер вернул неверное время или сертификат");
    data.settings.epoch=bootstrap.time;data.settings.ca=bootstrap.ca;
    const saved=await serial.request("configure",{...data.settings,device:approval.device_id,code:"",sensor_type:data.sensor_type,config:approval.config});
    if(!saved.saved)throw Error("Датчик не подтвердил сохранение настроек");
    usbClearSecrets();await refreshSensors();
    usbStatus("Готово. Для отладки закройте мастер, оставьте USB платы подключённым и коротко нажмите RESET. Для автономной работы отсоедините USB платы и зарядки, включите питание от аккумулятора и проверьте первые данные в карточке.");
  }catch(error){usbStatus(error.name==="NotFoundError"?"Порт не выбран или изменился после перезапуска. Нажмите «Настроить без прошивки» и выберите его снова.":error.message+(flashed?" Если USB переопределился после записи, продолжите кнопкой «Настроить без прошивки».":""));}
  finally{if(serial)await serial.close();usbSetBusy(false);}
}
$("sensor-usb-form").addEventListener("submit",e=>{e.preventDefault();usbRun(true);});
$("usb-configure").addEventListener("click",()=>usbRun(false));
$("usb-close").addEventListener("click",()=>{if(!usbBusy){usbClearSecrets();$("sensor-usb-dialog").close();}});
$("sensor-usb-dialog").addEventListener("cancel",e=>{if(usbBusy)e.preventDefault();else usbClearSecrets();});
$("sensor-installer-form").addEventListener("submit",async e=>{
  e.preventDefault();const button=e.target.querySelector("button");button.disabled=true;
  try{const data=new FormData();data.append("file",$("sensor-installer-file").files[0]);const r=await fetch("/api/sensors/installer",{method:"POST",credentials:"same-origin",body:data});if(!r.ok){const error=await r.json();throw Error(typeof error.detail==="string"?error.detail:"Комплект не принят");}await refreshInstaller();if(sensorSelected)await openSensor(sensorSelected);toast("Комплект прошивки загружен");}catch(error){toast(error.message,true);}finally{button.disabled=false;}
});
setInterval(()=>{
  if($("view-sensors").hidden||usbBusy||$("sensor-usb-dialog").open)return;
  if(!$("sensor-claims-panel").contains(document.activeElement))api("/api/sensors/claims").then(renderSensorClaims).catch(()=>{});
  if($("sensor-firmware-downloads").hidden)refreshInstaller().catch(()=>{});
},20000);

$("usb-setup-random").addEventListener("change",()=>{for(const id of ["usb-setup-password","usb-setup-confirm"])$(id).disabled=$("usb-setup-random").checked;});
