"use strict";
let usbInstallers=[],usbDefaults=null,usbBusy=false,usbExpectedHardware=null,usbRegistrationTimer=null;
function stopUsbRegistrationWatch(){if(usbRegistrationTimer!==null)clearTimeout(usbRegistrationTimer);usbRegistrationTimer=null;}
function watchUsbRegistration(hardware,remaining=45){
  stopUsbRegistrationWatch();
  usbRegistrationTimer=setTimeout(async()=>{
    if(!$("sensor-usb-dialog").open||usbBusy||usbExpectedHardware!==hardware)return;
    try{
      const devices=await api("/api/sensors/devices");
      if(!$("sensor-usb-dialog").open||usbBusy||usbExpectedHardware!==hardware)return;
      if(devices.some(d=>d.hardware_id===hardware&&d.enabled&&d.identity_state==="approved")){
        usbStatus("Готово. Сервер подтвердил регистрацию по Wi-Fi. Для отладки коротко нажмите RESET. Для автономной работы включите питание от аккумулятора.");
        await refreshSensors();watchUsbRegistration(usbExpectedHardware);return;
      }
    }catch{}
    if(remaining>1)watchUsbRegistration(hardware,remaining-1);
    else usbStatus("Прошивка и настройки записаны. Регистрация пока не подтверждена. Проверьте Wi-Fi и питание или настройте без прошивки.");
  },2000);
}
function usbServerBootstrapVersion(version){const m=/^(\d+)\.(\d+)\.(\d+)$/.exec(version||"");return Boolean(m&&(Number(m[1])>0||Number(m[2])>1||(Number(m[2])===1&&Number(m[3])>=5)));}
function usbFactoryVersion(version){const [a,b,c]=String(version).split(".").map(Number);return a>1||(a===1&&(b>1||(b===1&&c>=5)));}
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
    usbInstallers.push({...bundle.factory,version:bundle.version,url});
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
  const target=$("sensor-claims"),rejected=$("sensor-rejected-claims");if(!target)return;target.replaceChildren();rejected.replaceChildren();
  const denied=claims.filter(c=>c.state==="rejected");
  $("sensor-claims-panel").hidden=!claims.some(c=>c.state==="pending");
  $("sensor-rejected-panel").hidden=!denied.length;
  $("sensor-rejected-title").textContent="Отклонённые заявки ("+denied.length+")";
  for(const claim of claims){
    const article=el("div","sensor-claim");
    article.append(el("strong","",claim.hardware_id+" · "+claim.sensor_type.toUpperCase()),el("p","hint","Ключ "+claim.fingerprint.slice(0,16).match(/.{4}/g).join("–")+" · "+(claim.state==="rejected"?"Отклонён":"Ожидает разрешения")+" · "+sensorWhen(claim.last_seen)));
    const form=el("form","sensor-form"),name=el("input"),room=el("select"),approve=el("button","btn primary","Разрешить"),reject=el("button","btn secondary","Отклонить");
    name.required=true;name.maxLength=100;name.value=claim.name||"";name.placeholder="Название датчика";name.setAttribute("aria-label","Название нового датчика");room.setAttribute("aria-label","Помещение нового датчика");sensorRoomOptions(room,claim.room_id);reject.type="button";
    form.append(name,room,approve);if(claim.state!=="rejected")form.append(reject);article.append(form);(claim.state==="rejected"?rejected:target).append(article);
    form.addEventListener("submit",async e=>{e.preventDefault();try{await api("/api/sensors/claims/"+claim.fingerprint+"/approve","POST",{name:name.value.trim(),room_id:Number(room.value)});await refreshSensors();toast("Доступ разрешён. Ожидаем подключения датчика.");}catch(error){toast(error.message,true);}});
    reject.addEventListener("click",async()=>{try{await api("/api/sensors/claims/"+claim.fingerprint+"/reject","POST");await refreshSensors();sensorSelected=null;sensorLoadId++;$("sensor-detail").hidden=true;toast("Заявка отклонена");}catch(error){toast(error.message,true);}});
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
  const dnsPrimary=$("usb-dns-primary").value.trim(),dnsSecondary=$("usb-dns-secondary").value.trim();
  const validDNS=value=>{
    if(!value)return true;
    const parts=value.split(".");
    return parts.length===4&&parts.every(p=>/^(0|[1-9][0-9]{0,2})$/.test(p)&&Number(p)<=255)&&
      Number(parts[0])<224&&Number(parts[0])!==127&&value!=="0.0.0.0";
  };
  if(!validDNS(dnsPrimary)||!validDNS(dnsSecondary))throw Error("DNS: укажите корректный IPv4-адрес или оставьте поле пустым.");
  const length=encoder.encode(password).length;
  if(password&&(length<8||length>63))throw Error("Проверьте длину пароля Wi-Fi: 8–63 байта");
  if(encoder.encode($("usb-ssid").value).length>32)throw Error("Название Wi-Fi превышает 32 байта");
  if(!$("usb-setup-random").checked&&$("usb-setup-password").value!==$("usb-setup-confirm").value)throw Error("Пароли сети настройки не совпадают");
  const config={...usbDefaults,enabled:$("usb-measure-enabled").checked};
  for(const [key] of sensorConfigFields)config[key]=Number(form.elements[key].value);
  if((config.wifi_min_mv!==0&&config.cutoff_mv>config.wifi_min_mv)||config.cutoff_mv>config.ota_min_mv)throw Error("Пороги Wi-Fi и OTA должны быть не ниже отключения. Для Wi-Fi можно указать 0.");
  return {name:$("usb-name").value.trim(),room_id:Number($("usb-room").value),sensor_type:$("usb-type").value,config,
    settings:{ssid:$("usb-ssid").value,password,server:url.origin,dns_primary:dnsPrimary,dns_secondary:dnsSecondary,
      setup_password:$("usb-setup-password").value,setup_password_confirm:$("usb-setup-confirm").value,random_setup_password:$("usb-setup-random").checked}};
}
async function usbRun(flash){
  if(usbBusy)return;
  let serial=null,flashed=false,approved=false,savedLocally=false;
  try{
    stopUsbRegistrationWatch();
    const data=usbSettings();if(!data)return;
    if(!usbSupported())throw Error("USB недоступен. Откройте панель по HTTPS в Chrome или Edge на компьютере.");
    if(flash&&!$("usb-new-board").checked)throw Error("Подтвердите удаление данных платы или нажмите «Настроить без прошивки».");
    const installer=usbInstallers.find(item=>item.url===$("usb-firmware").value);
    if(flash&&!installer)throw Error("Дождитесь загрузки комплекта панели или загрузите свой комплект USB");
    // Request permission while the click still has browser user activation.
    usbSetBusy(true);
    const port=await navigator.serial.requestPort({filters:[{usbVendorId:0x303a}]});
    const usb=await import("./sensor-usb.mjs");
    usbStatus("Проверяем время и сертификат сервера…");
    const bootstrapResponse=await fetch(data.settings.server+"/api/sensors/bootstrap",{credentials:"omit",cache:"no-store"});
    if(!bootstrapResponse.ok)throw Error("Не удалось получить время и сертификат сервера");
    const bootstrap=await bootstrapResponse.json();
    if(!Number.isInteger(bootstrap.time)||bootstrap.time<1700000000||bootstrap.time>4102444800||typeof bootstrap.ca!=="string"||new TextEncoder().encode(bootstrap.ca).length>8000)throw Error("Сервер вернул неверное время или сертификат");
    data.settings.epoch=bootstrap.time;data.settings.ca=bootstrap.ca;
    if(flash){
      usbStatus("Проверяем комплект и записываем прошивку. Не отключайте USB.");$("usb-progress").hidden=false;$("usb-progress").value=0;
      const response=await fetch(installer.url,{credentials:"same-origin"});
      if(!response.ok)throw Error("Не удалось получить комплект с сервера");
      const direct=usbFactoryVersion(installer.version);
      usbExpectedHardware=await usb.flashFactory(port,await response.arrayBuffer(),installer,{
        prepareSettings:direct?async hardware=>{
          usbStatus("Готовим одноразовое разрешение и записываем настройки вместе с прошивкой…");
          const auth=await api("/api/sensors/usb/prepare","POST",{hardware_id:hardware,name:data.name,room_id:data.room_id,sensor_type:data.sensor_type,config:data.config});
          if(!/^[a-f0-9]{64}$/.test(auth.ticket))throw Error("Сервер не выдал разрешение заводской настройки");
          return {...data.settings,hardware_id:hardware,factory_ticket:auth.ticket,device:"",code:"",sensor_type:data.sensor_type,config:{...data.config,version:1}};
        }:null,
        checkBoard:hardware=>api("/api/sensors/usb/check/"+hardware),progress:value=>{$("usb-progress").value=value;},
        log:line=>{$("usb-log").hidden=false;$("usb-log").textContent=($("usb-log").textContent+String(line)+"\n").slice(-4000);}});
      flashed=true;usbStatus("Прошивка записана. Ожидаем запуска датчика…");
      $("usb-new-board").checked=false;
      if(direct){
        usbClearSecrets();
        usbStatus("Готово: прошивка и настройки записаны. Датчик зарегистрируется по Wi-Fi самостоятельно. Дождитесь появления карточки; для отладки коротко нажмите RESET после регистрации. Если связи нет, проверьте Wi-Fi или настройте без прошивки.");
        await refreshSensors();return;
      }
    }
    const connection=await usb.connectSensor(port,{expectedHardware:usbExpectedHardware,getPorts:()=>navigator.serial.getPorts(),onAttempt:(n,total)=>usbStatus("Ожидаем режим настройки: попытка "+n+" из "+total+"…")});
    serial=connection.serial;const {hello,fingerprint:fp}=connection;
    if(!usbServerBootstrapVersion(hello.firmware))throw Error("Для времени и сертификата от сервера обновите прошивку датчика до 0.1.5 или новее");
    if(hello.pending_records&&hello.sensor_type!==data.sensor_type)throw Error("Перед сменой типа передайте накопленные измерения на сервер");
    $("usb-identity").textContent="Плата "+hello.hardware_id+" · ключ "+fp.slice(0,16).match(/.{4}/g).join("–");
    const identity={public_key:hello.public_key,hardware_id:hello.hardware_id,sensor_type:data.sensor_type,firmware:hello.firmware};
    const deferred=hello.verified_setup===true;
    const verifySaved=async(payload)=>{
      const saved=await serial.request("configure",{...payload,defer_finish:true});
      if(!saved.saved||!saved.verified)throw Error("Плата не подтвердила проверку записи в память");
      const result=await serial.request("verify_configuration");
      if(!result.verified||result.device!==payload.device||result.sensor_type!==payload.sensor_type||
         Object.keys(payload.config).some(key=>result.config?.[key]!==payload.config[key]))
        throw Error("Сохранённые настройки не совпадают с переданными");
    };
    const base={...data.settings,code:"",sensor_type:data.sensor_type};
    // Only a confirmed unconfigured board may be written before approval.
    // Older firmware does not report whether it already has settings.
    if(deferred&&hello.provisioned===false){
      usbStatus("Сохраняем параметры и проверяем память платы…");
      await verifySaved({...base,device:"",config:data.config});
      savedLocally=true;
    }else if(!deferred) usbStatus("Старая прошивка: проверка записи недоступна. Разрешаем подключение…");
    usbStatus("Проверяем подпись платы и разрешаем подключение…");
    const challenge=await api("/api/sensors/auth/challenge","POST",identity);
    const signed=await serial.request("sign",{message:challenge.message});
    const approval=await api("/api/sensors/onboard","POST",{...identity,challenge:challenge.challenge,signature:signed.signature,name:data.name,room_id:data.room_id,config:data.config});
    approved=true;
    usbStatus("Доступ выдан. Сохраняем окончательные настройки…");
    const final={...base,device:approval.device_id,config:approval.config};
    if(deferred){
      await verifySaved(final);
      usbStatus("Настройки сверены с памятью. Завершаем настройку…");
      const done=await serial.request("finish_setup");
      if(!done.finished)throw Error("Плата не подтвердила завершение настройки");
    }else{
      const saved=await serial.request("configure",final);
      if(!saved.saved)throw Error("Датчик не подтвердил сохранение настроек");
    }
    usbClearSecrets();await refreshSensors();
    usbStatus("Готово. Для отладки закройте мастер, оставьте USB платы подключённым и коротко нажмите RESET. Для автономной работы отсоедините USB платы и зарядки, включите питание от аккумулятора и проверьте первые данные в карточке.");
  }catch(error){usbStatus(error.name==="NotFoundError"?"Порт не выбран.":error.message+(savedLocally&&!approved?" Параметры записаны, доступ ещё не выдан.":"")+(approved?" Доступ выдан, завершение не подтверждено.":"")+(flashed||approved||savedLocally?" Продолжите кнопкой «Настроить без прошивки»; повторно стирать память не нужно.":""));}
  finally{if(serial)await serial.close();usbSetBusy(false);}
}
$("sensor-usb-form").addEventListener("submit",e=>{e.preventDefault();usbRun(true);});
$("usb-configure").addEventListener("click",()=>usbRun(false));
$("usb-close").addEventListener("click",()=>{if(!usbBusy){stopUsbRegistrationWatch();usbClearSecrets();$("sensor-usb-dialog").close();}});
$("sensor-usb-dialog").addEventListener("cancel",e=>{if(usbBusy)e.preventDefault();else{stopUsbRegistrationWatch();usbClearSecrets();}});
$("sensor-installer-form").addEventListener("submit",async e=>{
  e.preventDefault();const button=e.target.querySelector("button");button.disabled=true;
  try{const data=new FormData();data.append("file",$("sensor-installer-file").files[0]);const r=await fetch("/api/sensors/installer",{method:"POST",credentials:"same-origin",body:data});if(!r.ok){const error=await r.json();throw Error(typeof error.detail==="string"?error.detail:"Комплект не принят");}await refreshInstaller();if(sensorSelected)await openSensor(sensorSelected);toast("Комплект прошивки загружен");}catch(error){toast(error.message,true);}finally{button.disabled=false;}
});
setInterval(()=>{
  if($("view-sensors").hidden||usbBusy||$("sensor-usb-dialog").open)return;
  if(!$("sensor-claims-panel").contains(document.activeElement)&&!$("sensor-rejected-panel").contains(document.activeElement))api("/api/sensors/claims").then(renderSensorClaims).catch(()=>{});
  if($("sensor-firmware-downloads").hidden)refreshInstaller().catch(()=>{});
},20000);

$("usb-setup-random").addEventListener("change",()=>{for(const id of ["usb-setup-password","usb-setup-confirm"])$(id).disabled=$("usb-setup-random").checked;});
