"use strict";
// MAX notification composition, recipients and delivery history.
let notificationPeople=[], notificationSelected=new Set();
let recipientState="loading", recipientFailure="", notificationSending=false;
let notificationHistoryBusy=false, renderedNotificationHistory="";
let lastNotificationDraft=null;
function notificationTargets(){
  const mode=$("notification-audience").value;
  const building=$("notification-building").value;
  return notificationPeople.filter(person=>{
    if(mode==="missing")return person.missing;
    if(mode==="building")return person.rooms.some(room=>room.building===building);
    if(mode==="selected")return notificationSelected.has(person.user_id);
    return true;
  });
}
function updateNotificationPreview(){
  const mode=$("notification-audience").value;
  show("notification-building-wrap",mode==="building");
  show("notification-selected-wrap",mode==="selected");
  const people=notificationTargets(), count=people.length, ready=recipientState==="ready";
  const draft=$("notification-text").value;
  let summary="Загружаем получателей…", state="working";
  if(recipientState==="error"){
    summary="Не удалось загрузить получателей. "+recipientFailure;
    state="error";
  }else if(ready){
    state=count===0||count>250?"warning":"ready";
    if(count>250)summary="Получателей: "+count+". Максимум за отправку — 250.";
    else if(count)summary="Получателей: "+count;
    else if(mode==="building"&&!$("notification-building").value)summary="Выберите корпус.";
    else if(mode==="selected")summary="Отметьте сотрудников в списке.";
    else summary="В этой группе нет получателей.";
  }
  setFeedback("notification-recipient-summary",summary,state);
  setFeedback("notification-recipient-names",ready&&count
    ?people.slice(0,8).map(person=>person.name).join(", ")+(count>8?" и ещё "+(count-8):"")
    :"");
  setFeedback("notification-text-count",draft.length+" / 2000");
  $("notification-draft").disabled=notificationSending;
  $("notification-refresh-recipients").disabled=notificationSending;
  $("notification-send").disabled=!settingsCache?.max_token_configured||!ready||
    count===0||count>250||!draft.trim()||notificationSending;
  show("notification-token-hint",settingsCache?.max_token_configured===false);
}
function renderNotificationPeople(){
  const buildings=[...new Set(notificationPeople.flatMap(person=>person.rooms.map(room=>room.building)))];
  buildings.sort((a,b)=>a.localeCompare(b,"ru"));
  const building=$("notification-building"),previous=building.value;
  building.replaceChildren();
  option(building,"","Выберите корпус");
  for(const name of buildings)option(building,name,name);
  building.value=buildings.includes(previous)?previous:"";
  const list=$("notification-selected-users");
  list.replaceChildren();
  for(const person of notificationPeople){
    const label=el("label","notification-user");
    const checkbox=el("input");
    checkbox.type="checkbox";
    checkbox.value=person.user_id;
    checkbox.checked=notificationSelected.has(person.user_id);
    checkbox.addEventListener("change",()=>{
      if(checkbox.checked)notificationSelected.add(person.user_id);
      else notificationSelected.delete(person.user_id);
      updateNotificationPreview();
    });
    const info=el("span","notification-user-info");
    info.append(el("strong","",person.name),
      el("small","",person.rooms.map(room=>room.building+" / "+room.name).join(" · ")));
    if(person.missing)info.append(el("small","notification-missing","Есть кабинеты без показаний за сегодня"));
    label.append(checkbox,info);
    list.append(label);
  }
}
let recipientsLoading=false;
async function loadNotificationRecipients(){
  if(recipientsLoading||notificationSending)return;
  recipientsLoading=true;
  recipientState="loading";
  updateNotificationPreview();
  try{
    notificationPeople=await api("/api/notifications/recipients");
    const ids=new Set(notificationPeople.map(person=>person.user_id));
    notificationSelected=new Set([...notificationSelected].filter(uid=>ids.has(uid)));
    renderNotificationPeople();
    recipientState="ready";
  }catch(error){
    recipientFailure=error.message;
    recipientState="error";
  }finally{
    recipientsLoading=false;
    updateNotificationPreview();
  }
}
function renderNotificationHistory(batches){
  const target=$("notification-history");
  const entries=batches.map(item=>{
    const entry=el("div","notification-history-item");
    const top=el("div","notification-history-top");
    top.append(el("strong","",when(item.created_at)),
      el("span","",item.silent?"Тихое уведомление":"Обычное уведомление"));
    const delivery=el("div","notification-delivery");
    if(item.delivered>0)delivery.append(el("span","notification-badge accepted","Принято MAX: "+item.delivered+" из "+item.recipients));
    const waiting=item.recipients-item.delivered-item.retrying;
    if(waiting>0)delivery.append(el("span","notification-badge queued","В очереди: "+waiting));
    if(item.retrying>0)delivery.append(el("span","notification-badge retrying","Повторная отправка: "+item.retrying));
    entry.append(top,el("p","notification-history-message",item.text),delivery);
    return entry;
  });
  target.replaceChildren(...entries);
}
async function loadNotificationHistory(){
  if(notificationHistoryBusy)return;
  notificationHistoryBusy=true;
  try{
    const batches=await api("/api/notifications/history");
    const snapshot=JSON.stringify(batches);
    if(snapshot!==renderedNotificationHistory){
      renderNotificationHistory(batches);
      renderedNotificationHistory=snapshot;
    }
    setFeedback("notification-history-status",batches.length?"":"Сообщений пока нет.");
  }catch(error){
    setFeedback("notification-history-status","Не удалось обновить историю отправок. "+error.message,"error");
  }finally{notificationHistoryBusy=false;}
}
async function loadNotificationCenter(){
  await Promise.all([loadNotificationRecipients(),loadNotificationHistory()]);
}

function initNotifications(){
  for(const id of ["notification-audience","notification-building","notification-text","notification-silent"]){
    $(id).addEventListener("input",()=>{
      if($("notification-send-feedback").dataset.state==="saved")setFeedback("notification-send-feedback","");
      updateNotificationPreview();
    });
  }
  onOperation($("notification-refresh-recipients"),"Обновляем список получателей…",loadNotificationRecipients);
  onOperation($("notification-refresh-history"),"Обновляем историю отправок…",loadNotificationHistory);
  onOperation($("notification-compose-form"),"Ставим сообщение в очередь MAX…",async()=>{
    if(notificationSending||recipientState!=="ready"||!settingsCache?.max_token_configured)return;
    const user_ids=notificationTargets().map(person=>person.user_id).sort();
    const text=$("notification-text").value.trim(), silent=$("notification-silent").checked;
    if(!text||!user_ids.length||user_ids.length>250)return;
    if(!confirm("Отправить сообщение через MAX для "+user_ids.length+" сотрудников?\n\n"+text.slice(0,240)))return;
    const signature=JSON.stringify([text,user_ids,silent]);
    if(lastNotificationDraft?.signature!==signature){
      const id=crypto.randomUUID?.()||("notify-"+Date.now().toString(36)+"-"+Math.random().toString(36).slice(2,12));
      lastNotificationDraft={signature,id};
    }
    notificationSending=true;
    updateNotificationPreview();
    setFeedback("notification-send-feedback","Сохраняем сообщение в очереди MAX…","working");
    let refreshRecipients=false;
    try{
      const result=await api("/api/notifications/send","POST",{
        request_id:lastNotificationDraft.id,text,user_ids,silent
      });
      setFeedback("notification-send-feedback",result.already_queued
        ?"Сервер уже принял эту рассылку. Статусы доступны в истории."
        :"Добавлено в очередь MAX. Получателей: "+result.queued+".","saved");
      $("notification-text").value="";
      lastNotificationDraft=null;
      await loadNotificationHistory();
    }catch(error){
      setFeedback("notification-send-feedback","Не удалось подтвердить постановку в очередь. "+error.message,"error");
      if(error.status===409){
        settingsCache.max_token_configured=false;
        renderSettingsSummary();
      }
      refreshRecipients=error.status===422;
    }finally{
      notificationSending=false;
      updateNotificationPreview();
    }
    if(refreshRecipients)await loadNotificationRecipients();
  });
  setInterval(()=>{
    if(selectedTab==="settings"&&selectedSettingsCategory==="notifications"&&
        !$("app-root").hidden&&!document.hidden)void loadNotificationHistory();
  },5000);
}
