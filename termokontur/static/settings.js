"use strict";
// Settings workspace, form baselines, TLS/CA and conflict-safe saves.
/* Accessible settings workspace: categories, overview and independent saves. */
const settingsGroups = {
  general:["general-settings-form"],notifications:["reminders-settings-form","alerts-settings-form"],
  backups:["maintenance-form"],tls:["tls-form","sensor-ca-form"],calendar:["calendar-form"]
};
let selectedSettingsCategory="summary",settingsLoaded=false;
const settingsEditRevision=new WeakMap();
const settingsBaseline=new WeakMap();
let settingsCategory;
function settingsDirty(){
  return !!document.querySelector('.settings-pane form[data-dirty="true"]');
}
function updateDirtyNavigation(){
  for(const [category,forms] of Object.entries(settingsGroups)){
    const dirty=forms.some(id=>$(id).dataset.dirty==="true");
    const button=document.querySelector('.settings-nav-button[data-settings-target="'+category+'"]');
    const choice=settingsCategory.querySelector('option[value="'+category+'"]');
    button.classList.toggle("is-dirty",dirty);
    choice.textContent=button.textContent.trim()+(dirty?" · не сохранено":"");
  }
}
function markSettingsSaved(form,message){
  form.dataset.dirty="false";
  for(const field of form.querySelectorAll("[data-tk-edited]"))field.removeAttribute("data-tk-edited");
  const feedback=form.querySelector(".settings-save-feedback");
  if(feedback){feedback.textContent=message;feedback.dataset.state="saved";}
  updateDirtyNavigation();
}
function markSettingsDirty(form){
  settingsEditRevision.set(form,(settingsEditRevision.get(form)||0)+1);
  form.dataset.dirty="true";
  const feedback=form.querySelector(".settings-save-feedback");
  if(feedback){feedback.textContent="Есть несохранённые изменения";feedback.dataset.state="unsaved";}
  updateDirtyNavigation();
}
function setSettingsCategory(category){
  const target=document.querySelector('.settings-pane[data-settings-view="'+category+'"]');
  if(!target || (selectedSettingsCategory===category && !target.hidden))return;
  scrollToSectionStart(document.querySelector(".settings-layout"));
  selectedSettingsCategory=category;
  settingsCategory.value=category;
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
  if(category==="tls"){void loadTlsStatus();void loadSensorCa();}
  if(category==="notifications")void loadNotificationCenter();
  if(category==="calendar")void loadSchoolCalendar();
  if(category==="access")void loadBuildingAccounts();
  if(category==="security")void loadAudit();
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
async function loadSettings() {
  try {
    settingsCache=await api("/api/settings");
    const pending=new Map();
    for(const id of Object.values(settingsGroups).flat()){
      const form=$(id);
      if(form.dataset.dirty==="true")pending.set(id,
        [...form.querySelectorAll("input,select,textarea")].map(input=>({value:input.value,checked:input.checked})));
    }
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
    $("max-webhook-mode").textContent=settingsCache.max_mode==="webhook"?"Включён webhook. Отключите его кнопкой ниже для перехода на Long Polling.":"Long Polling включён. Публичный IP не требуется.";
    $("switch-polling").hidden=settingsCache.max_mode!=="webhook";
    $("ingest-status").textContent=settingsCache.ingest_key_configured?"Ключ датчиков настроен":"Нет ключа для датчиков";
    document.title=settingsCache.organization_name+" — Термоконтур";
    settingsLoaded=true;
    for(const id of Object.values(settingsGroups).flat()){
      const form=$(id),values=pending.get(id);
      if(values){
        [...form.querySelectorAll("input,select,textarea")].forEach((input,i)=>{
          if(input.type!=="file")input.value=values[i].value;
          if(input.type==="checkbox"||input.type==="radio")input.checked=values[i].checked;
        });
      }else {form.dataset.dirty="false";settingsBaseline.set(form,{...settingsCache});}
    }
    syncNotificationControls();
    renderSettingsSummary();
    updateDirtyNavigation();
    updateNotificationPreview();
    await Promise.all([loadUpdateStatus(),loadBackups()]);
    if(selectedSettingsCategory==="max")void loadMaxDiagnostics();
  } catch(error){toast(error.message,true);}
}
async function loadSensorCa(){
  try{
    const state=await api("/api/tls/sensor-ca");
    const label={upload:"CA из панели",environment:"CA из окружения",automatic:"Автоматический режим"};
    $("sensor-ca-status").textContent=state.error?"Ошибка: "+state.error:
      label[state.source]+(state.chain_matches===true?" · Подпись цепочки HTTPS подтверждена":
      state.chain_matches===false?" · CA не соответствует активному HTTPS: проверьте цепочку":" · Проверка активной цепочки недоступна");
    const lines=state.certificates.flatMap(c=>[c.subject+" · действует до "+new Date(c.expires_at).toLocaleDateString("ru-RU"),"Издатель: "+c.issuer,"SHA-256: "+c.sha256]);
    if(state.server_hosts.length)lines.push("Имена активного HTTPS: "+state.server_hosts.join(", "));
    $("sensor-ca-details").replaceChildren(...lines.map(line=>el("p","",line)));
    $("sensor-ca-remove").disabled=state.source!=="upload";
  }catch(error){$("sensor-ca-status").textContent=error.message;}
}
/* TLS certificate upload is multipart; never expose key material in JSON. */
let tlsCache=null;
async function loadTlsStatus(){
  try{
    const state=await api("/api/tls/status");
    tlsCache=state;
    $("tls-status").textContent=(
      state.error?"Ошибка: "+state.error:
      state.active?"Встроенный HTTPS работает":
      state.configured?"Сертификат сохранён, ожидает применения":"Встроенный HTTPS выключен"
    )+(state.restart_pending?" · Требуется перезапуск.":"");
    const details=[];
    if(state.details){
      const d=state.details;
      details.push("Кому выдан: "+d.subject);
      details.push("Кем выдан: "+d.issuer);
      details.push("Домены/IP: "+(d.hosts.length?d.hosts.join(", "):"не указаны"));
      details.push("Действует до: "+new Date(d.expires_at).toLocaleDateString("ru-RU")+
        " ("+d.days_left+" дн.)");
      details.push("Источник: "+(state.source==="upload"?"файлы из панели":"путь из окружения"));
    }
    $("tls-details").replaceChildren(...details.map(line=>el("p","",line)));
    $("tls-details").hidden=details.length===0;
    $("tls-apply").disabled=!state.managed||!state.restart_pending||!!state.error;
    $("tls-apply").textContent=state.configured?"Применить HTTPS (перезапуск)":"Применить HTTP (перезапуск)";
    $("tls-remove").disabled=state.source!=="upload";
    $("tls-proxy-warning").hidden=!(window.location.protocol==="https:"&&!state.active);
    const next=new URL(window.location.href);
    next.protocol=state.configured?"https:":"http:";
    next.port=state.configured?"":"8092";
    $("tls-next-link").href=next.href;
    $("tls-next-link").textContent=next.href;
    $("tls-next").hidden=!state.restart_pending;
  }catch(error){
    $("tls-status").textContent="Не удалось проверить HTTPS: "+error.message;
  }
}
/* Merge only the current form's fields into fresh settings from the server.
   Other categories and secrets are not inadvertently overwritten. */
async function saveSettingsPatch(form,patch,message){
  const revision=settingsEditRevision.get(form);
  const feedback=form.querySelector(".settings-save-feedback");
  setFeedback(feedback.id,"Сохраняем…","working");
  try{
    const baseline=settingsBaseline.get(form)||settingsCache;
    const data={...baseline,...patch};
    if(data.retention_days>0 && data.retention_days<30)
      throw Error("Срок хранения: минимум 30 дней или 0");
    if(!Array.isArray(data.reminder_days)||data.reminder_days.length===0)
      throw Error("Выберите хотя бы один день для расписания");
    const expected=Object.fromEntries(Object.keys(patch).map(key=>[key,baseline[key]]));
    settingsCache=await api("/api/settings","PATCH",{changes:patch,expected});
    // Only advance the saved form's baseline; other forms may still display
    // the values they originally loaded and must detect a concurrent edit.
    settingsBaseline.set(form,{...settingsCache});
    renderSettingsSummary();
    document.title=settingsCache.organization_name+" — Термоконтур";
    if(revision===settingsEditRevision.get(form)){
      markSettingsSaved(form,message);
      toast(message);
    }
    return true;
  }catch(error){
    setFeedback(feedback.id,"Не сохранено. "+error.message,"error");
    toast(error.message,true);
    return false;
  }
}

function initSettings(){
  settingsCategory=$("settings-category");
  for(const button of document.querySelectorAll(".settings-nav-button")){
    option(settingsCategory,button.dataset.settingsTarget,button.textContent.trim());
  }
  settingsCategory.addEventListener("change",()=>setSettingsCategory(settingsCategory.value));
  for(const formIds of Object.values(settingsGroups))for(const id of formIds){
    const form=$(id);
    for(const type of ["input","change"])form.addEventListener(type,event=>{if(event.target.matches("input,select,textarea"))event.target.dataset.tkEdited="true";markSettingsDirty(form);});
  }
  for(const button of document.querySelectorAll("[data-settings-target]")){
    button.addEventListener("click",()=>setSettingsCategory(button.dataset.settingsTarget));
  }
  $("setting-enabled").addEventListener("change",syncNotificationControls);
  $("setting-alerts-enabled").addEventListener("change",syncNotificationControls);
  onOperation($("sensor-ca-form"),"Проверяем сертификаты датчиков…",async event=>{
    const form=event.currentTarget;
    try{
      const data=await api("/api/tls/sensor-ca","POST",new FormData(form));
      form.reset();markSettingsSaved(form,"CA сохранены");await loadSensorCa();
      if(data.chain_matches===false){
        setFeedback("sensor-ca-feedback","Сертификаты сохранены. Цепочка текущего сервера отличается.","warning");
        toast("Сертификаты сохранены. Цепочка текущего сервера отличается.",true);
      }else toast("Сертификаты датчиков сохранены");
    }catch(error){setFeedback("sensor-ca-feedback",error.message,"error");toast(error.message,true);}
  });
  onOperation($("sensor-ca-remove"),"Удаляем сертификаты датчиков…",async()=>{
    if(!confirm("Удалить CA датчиков? Платы могут потерять связь после изменения цепочки."))return;
    try{await api("/api/tls/sensor-ca","DELETE");await loadSensorCa();toast("CA удалены");}catch(error){toast(error.message,true);}
  });
  onOperation($("tls-form"),"Загружаем и проверяем сертификат…",async event=>{
    const form=event.currentTarget;
    if(!$("tls-certificate").files.length&&!$("tls-key").files.length){
      toast("Выберите сертификат или ключ",true);return;
    }
    try{
      await api("/api/tls/certificate","POST",new FormData(form));
      form.reset();
      markSettingsSaved(form,"Сертификат проверен и сохранён");
      toast("Сертификат сохранён. Примените настройки.");
      await loadTlsStatus();
    }catch(error){
      $("tls-feedback").textContent="Не сохранено: "+error.message;
      $("tls-feedback").dataset.state="error";
      toast(error.message,true);
    }
  });
  onOperation($("tls-remove"),"Удаляем сертификат…",async()=>{
    if(!confirm("Убрать загруженный сертификат? После применения сайт может переключиться на HTTP."))return;
    try{
      await api("/api/tls/certificate","DELETE");
      await loadTlsStatus();
      toast("Сертификат удалён. Примените настройки.");
    }catch(error){toast(error.message,true);}
  });
  onOperation($("tls-apply"),"Применяем настройки HTTPS…",async()=>{
    if(!tlsCache?.restart_pending)return;
    const mode=tlsCache.configured?"HTTPS":"HTTP";
    if(!confirm("Перезапустить панель с протоколом "+mode+"? Адрес страницы может измениться."))return;
    try{
      await api("/api/tls/apply","POST");
      $("tls-status").textContent="Веб-панель перезапускается. Используйте новый адрес ниже.";
      $("tls-apply").disabled=true;
      $("tls-next").hidden=false;
      toast("Перезапуск запрошен. Откройте панель по ссылке в настройках.");
    }catch(error){toast(error.message,true);}
  });
  onOperation($("general-settings-form"),"Сохраняем настройки организации…",async event=>{
    const saved=await saveSettingsPatch(event.currentTarget,{
      organization_name:$("setting-organization").value.trim(),
      timezone:$("setting-timezone").value.trim(),
      stale_hours:Number($("setting-stale").value)
    },"Настройки организации сохранены");
    if(saved)await refresh();
  });
  onOperation($("reminders-settings-form"),"Сохраняем напоминания…",async event=>{
    const days=[...document.querySelectorAll('[name="remind-day"]:checked')].map(x=>Number(x.value));
    const saved=await saveSettingsPatch(event.currentTarget,{
      reminders_enabled:$("setting-enabled").checked,
      reminder_time:$("setting-time").value,
      reminder_days:days,
      reminder_window_minutes:Number($("setting-window").value)
    },"Расписание напоминаний сохранено");
    if(saved)await refresh();
  });
  onOperation($("alerts-settings-form"),"Сохраняем температурные предупреждения…",async event=>{
    await saveSettingsPatch(event.currentTarget,{
      alerts_enabled:$("setting-alerts-enabled").checked,
      alert_cooldown_minutes:Number($("setting-alert-cooldown").value)
    },"Температурные уведомления сохранены");
  });
}
