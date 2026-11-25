"use strict";
// Native panel update status, progress, controls and polling.
let updateAvailable=false, updateLatestRevision=null, updateLatestVersion=null;
let updateInstalledRevision=null, updateState="idle", updateChecking=false;
let updateRequesting=false, updatePolling=false, updateStartedHere=false;
let updateSupported=false,updateActionRevision=0;
const updatePhases=[
  ["preparing","Подготовка"],["downloading","Загрузка исходного кода"],
  ["dependencies","Установка зависимостей"],["backup","Резервная копия"],
  ["switching","Переключение версии"],["verifying","Проверка запуска"],
  ["finishing","Завершение"]
];
function updateElapsed(seconds){
  if(!Number.isFinite(seconds)||seconds<0)return "";
  const minutes=Math.floor(seconds/60),hours=Math.floor(minutes/60);
  return hours?hours+" ч "+(minutes%60)+" мин":minutes?minutes+" мин "+(seconds%60)+" сек":seconds+" сек";
}
const updateStateLabels={
  idle:"Готово к проверке",queued:"Обновление в очереди",
  installing:"Выполняется обновление",success:"Обновление завершено",
  failed:"Ошибка обновления"
};
function renderUpdateStatus(r){
  const state=r.state||"idle", active=state==="queued"||state==="installing";
  updateState=state;
  updateSupported=r.supported;
  if(!updateInstalledRevision&&r.installed_revision)updateInstalledRevision=r.installed_revision;
  if(Object.prototype.hasOwnProperty.call(r,"available")){
    updateAvailable=r.available===true;
    updateLatestRevision=r.latest_revision||null;
    updateLatestVersion=r.latest_version||null;
  }
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
  }else{
    $("update-latest-version").textContent="Не проверено";
    $("update-latest-build").textContent="Проверьте наличие обновлений";
  }
  $("update-state").dataset.state=state;
  $("update-state-label").textContent=updateStateLabels[state]||"Состояние обновления";
  $("update-state-symbol").textContent=({idle:"○",queued:"◷",installing:"↻",success:"✓",failed:"!"})[state]||"○";
  setFeedback("update-status-text",r.message||"Проверьте наличие обновлений на GitHub.");
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
  const phases=$("update-phase-list");
  phases.replaceChildren();
  for(const [index,item] of updatePhases.entries()){
    const entry=el("li","update-phase-item",item[1]);
    const done=state==="success"||state==="installing"&&index<stepIndex;
    const current=active&&(state==="queued"?index===0:index===stepIndex);
    if(done)entry.classList.add("done");
    if(current)entry.classList.add("current");
    entry.setAttribute("aria-current",current?"step":"false");
    phases.append(entry);
  }
  const duration=r.duration_seconds;
  const haveMeta=Boolean(r.started_at||r.target_revision);
  show("update-meta",haveMeta);
  $("update-elapsed").textContent=r.started_at
    ?(active?"Выполняется: ":"Длительность: ")+updateElapsed(duration):"";
  $("update-target").textContent=r.target_revision?"Целевая сборка: "+r.target_revision.slice(0,12):"";
  const history=Array.isArray(r.history)?r.history:[];
  const historyPanel=$("update-history"),historyList=$("update-history-list");
  show("update-history",history.length>0);
  historyList.replaceChildren();
  for(const event of history){
    const row=el("li","update-history-entry");
    const stage=updatePhases.find(([key])=>key===event.phase);
    const name=stage?stage[1]:event.phase==="success"?"Успешно":event.phase==="failed"?"Ошибка":"Событие";
    const time=event.at?when(event.at):"";
    row.append(el("strong","",name),el("span","",event.message||""),el("small","",time));
    historyList.append(row);
  }
  if(state==="failed")historyPanel.open=true;
  renderUpdateControls();
  show("reload-after-update",state==="success"&&(
    updateStartedHere||!!(updateInstalledRevision&&r.installed_revision&&
      updateInstalledRevision!==r.installed_revision)
  ));
}
function renderUpdateControls(){
  const active=updateState==="queued"||updateState==="installing";
  const busy=active||updateRequesting||updateChecking;
  $("check-update").disabled=!updateSupported||busy;
  $("install-update").disabled=!updateSupported||!updateAvailable||busy;
}
async function loadUpdateStatus(){
  if(updatePolling||updateChecking||updateRequesting)return;
  const revision=updateActionRevision;
  updatePolling=true;
  try{
    const r=await api("/api/update/status");
    if(revision!==updateActionRevision)return;
    if(r.state==="success"&&updateInstalledRevision&&r.installed_revision!==updateInstalledRevision){
      updateAvailable=false;
    }
    if(r.state!=="idle"&&$("update-action-feedback").dataset.state==="queued"){
      setFeedback("update-action-feedback","");
    }
    renderUpdateStatus(r);
  }catch(error){
    if(revision!==updateActionRevision)return;
    $("update-status-text").textContent=
      ["queued","installing"].includes(updateState)
        ?"Нет связи с сервером. Ожидаем подключения…"
        :"Не удалось получить состояние обновления: "+error.message;
  }finally{updatePolling=false;}
}

function initUpdates(){
  onOperation($("check-update"),"Проверяем обновления на GitHub…",async()=>{
    if(updateChecking||updateRequesting||["queued","installing"].includes(updateState))return;
    updateChecking=true;
    updateActionRevision++;
    renderUpdateControls();
    setFeedback("update-action-feedback","Проверяем GitHub…","working");
    try{
      const r=await api("/api/update/check");
      const active=["queued","installing"].includes(r.state);
      renderUpdateStatus(active?r:{...r,message:""});
      setFeedback("update-action-feedback",active?"":r.message,r.available===null?"error":"saved");
    }catch(error){
      updateAvailable=false;
      updateLatestRevision=null;
      updateLatestVersion=null;
      $("update-latest-version").textContent="Не проверено";
      $("update-latest-build").textContent="Проверьте наличие обновлений";
      setFeedback("update-action-feedback","Проверка не удалась. "+error.message,"error");
    }finally{
      updateChecking=false;
      renderUpdateControls();
    }
  });
  onOperation($("install-update"),"Отправляем запрос на обновление…",async()=>{
    if(!updateAvailable||updateRequesting||["queued","installing"].includes(updateState))return;
    if(!confirm("Установить обновление с GitHub? Панель временно перезапустится; данные будут сохранены."))return;
    updateRequesting=true;
    updateActionRevision++;
    renderUpdateControls();
    setFeedback("update-action-feedback","Отправляем запрос на установку…","working");
    try{
      const result=await api("/api/update/request","POST",{});
      updateStartedHere=true;
      updateState=result.state;
      setFeedback("update-action-feedback",result.message,"queued");
    }catch(error){
      // Server restart can interrupt the POST after the request was accepted.
      setFeedback("update-action-feedback","Не удалось подтвердить запрос. "+error.message,"error");
    }finally{
      updateRequesting=false;
      renderUpdateControls();
      await loadUpdateStatus();
    }
  });
  $("reload-after-update").addEventListener("click",()=>window.location.reload());
  setInterval(()=>{
    if(!$("app-root").hidden&&!document.hidden&&((selectedTab==="settings"&&
        ["summary","updates"].includes(selectedSettingsCategory))||
        updateState==="queued"||updateState==="installing"||updateRequesting)){
      void loadUpdateStatus();
    }
  },3000);
}
