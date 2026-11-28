"use strict";
// MAX staff assignments, pairing, token and diagnostics.
const selectedMaxRooms = new Set();
let activeMaxCodes = [], issuedMaxCodes = [];
let maxStaff = [], selectedMaxStaff = null;
const maxAssignmentRooms = new Set();

function maxUserName(user){
  return [user.first_name,user.last_name].filter(Boolean).join(" ") ||
    (user.username ? "@"+user.username : "MAX ID "+user.user_id);
}

function renderMaxStaff(){
  const list=$("max-users-list");list.replaceChildren();
  const needle=$("max-users-search").value.trim().toLocaleLowerCase("ru");
  const onlyUnassigned=$("max-users-unassigned").checked;
  const filtered=maxStaff.filter(user=>{
    const active=(user.rooms||[]).filter(r=>r.active);
    if(onlyUnassigned && active.length)return false;
    return [user.first_name,user.last_name,user.username,user.user_id,
      ...(user.rooms||[]).flatMap(r=>[r.name,r.building])]
      .some(value=>String(value||"").toLocaleLowerCase("ru").includes(needle));
  });
  $("max-users-count").textContent=maxStaff.length+" сотрудников · "+filtered.length+" показано";
  if(!filtered.length){
    list.append(el("p","max-codes-empty",
      maxStaff.length?"По выбранным условиям сотрудники не найдены.":
      "Сотрудники пока не обращались к боту MAX."));
    return;
  }
  for(const user of filtered){
    const card=el("div","max-staff-entry");
    const info=el("div","max-staff-info");
    const title=el("strong","",maxUserName(user));
    const subtitle=el("small","",[
      user.username?"@"+user.username:"",
      "MAX ID "+user.user_id,
      user.seen_at?"Был в боте: "+when(user.seen_at):""
    ].filter(Boolean).join(" · "));
    const active=(user.rooms||[]).filter(room=>room.active);
    const roomList=el("div","max-staff-rooms");
    if(!active.length)roomList.append(el("span","max-unbound","Кабинеты не назначены"));
    else for(const room of active){
      roomList.append(el("span","max-staff-room",
        room.building+" / "+room.name));
    }
    const inactive=(user.rooms||[]).filter(room=>!room.active);
    if(inactive.length)roomList.append(el("span","max-staff-room muted",
      "Отключённых: "+inactive.length));
    info.append(title,subtitle,roomList);
    const buttons=el("div","max-staff-actions");
    const update=el("button","btn secondary max-staff-refresh","Обновить ФИО");
    update.type="button";
    update.title="Повторно запросить имя и фамилию у MAX";
    onOperation(update,"Обновляем имя сотрудника…",()=>refreshMaxStaffProfile(user,update));
    const assign=el("button","btn secondary max-staff-assign","Назначить кабинеты");
    assign.type="button";
    assign.addEventListener("click",()=>openMaxAssignment(user));
    buttons.append(update,assign);
    card.append(info,buttons);
    list.append(card);
  }
}
async function refreshMaxStaffProfile(user,button){
  button.disabled=true;
  try{
    await api("/api/max/users/"+encodeURIComponent(user.user_id)+"/refresh","POST");
    await refresh();
    toast("Данные профиля MAX обновлены");
  }catch(error){toast(error.message,true);}
  finally{button.disabled=false;}
}
function filteredMaxAssignmentRooms(){
  const needle=$("max-assignment-search").value.trim().toLocaleLowerCase("ru");
  return rooms.filter(room=>[room.building,room.name,room.floor]
    .some(value=>String(value||"").toLocaleLowerCase("ru").includes(needle)));
}
function renderMaxAssignment(){
  const visible=filteredMaxAssignmentRooms(),list=$("max-assignment-list");
  list.replaceChildren();
  for(const room of visible){
    const label=el("label","max-codes-room");
    const checkbox=el("input");checkbox.type="checkbox";
    checkbox.checked=maxAssignmentRooms.has(room.id);
    checkbox.addEventListener("change",()=>{
      if(checkbox.checked)maxAssignmentRooms.add(room.id);
      else maxAssignmentRooms.delete(room.id);
      renderMaxAssignment();
    });
    const details=el("span","max-codes-room-text");
    details.append(el("strong","",room.building+" / "+room.name));
    if(room.floor)details.append(el("small","","Этаж: "+room.floor));
    label.append(checkbox,details);list.append(label);
  }
  if(!visible.length)list.append(el("p","max-codes-empty","Кабинеты не найдены."));
  const selectAll=$("max-assignment-all");
  const selectedVisible=visible.filter(room=>maxAssignmentRooms.has(room.id)).length;
  selectAll.checked=visible.length>0&&selectedVisible===visible.length;
  selectAll.indeterminate=selectedVisible>0&&selectedVisible<visible.length;
  $("max-assignment-count").textContent="Назначено "+maxAssignmentRooms.size+" из "+rooms.length;
}
function openMaxAssignment(user){
  selectedMaxStaff=user;
  maxAssignmentRooms.clear();
  for(const room of user.rooms||[])if(room.active)maxAssignmentRooms.add(room.id);
  $("max-assignment-profile").textContent=maxUserName(user)+" · MAX ID "+user.user_id;
  $("max-assignment-search").value="";
  $("max-assignment-notify").checked=false;
  renderMaxAssignment();
  $("max-user-rooms-dialog").showModal();
}
function closeMaxAssignment(){
  $("max-user-rooms-dialog").close();
  selectedMaxStaff=null;
}
async function loadMaxStaff(){
  maxStaff=await api("/api/max/users");
  renderMaxStaff();
}

async function pairing(room){
  try {
    const reply=await api("/api/rooms/"+room.id+"/pairing","POST");
    $("pairing-code").textContent=reply.code;
    $("pairing-dialog").showModal();
  } catch(error){toast(error.message,true);}
}
async function unbindMax(room,user){
  if(!confirm("Отвязать «"+maxUserName(user)+"» от помещения «"+room.name+"»? Сотрудник потеряет доступ к кабинету."))return;
  try{
    await api("/api/rooms/"+room.id+"/max-bindings/"+encodeURIComponent(user.user_id),"DELETE");
    await refresh();
    toast("Сотрудник отключён от помещения");
  }catch(error){toast(error.message,true);}
}

function initMaxStaff(){
  onOperation($("open-staff"),"Загружаем сотрудников…",async()=>{
    if(setTab("staff"))await loadMaxStaff();
  });
  onOperation($("staff-refresh"),"Обновляем список сотрудников…",async()=>{
    await loadMaxStaff();
    toast("Список сотрудников обновлён");
  });
  $("max-users-search").addEventListener("input",renderMaxStaff);
  $("max-users-unassigned").addEventListener("change",renderMaxStaff);
  $("max-assignment-close").addEventListener("click",closeMaxAssignment);
  $("max-assignment-cancel").addEventListener("click",closeMaxAssignment);
  $("max-assignment-search").addEventListener("input",renderMaxAssignment);
  $("max-assignment-all").addEventListener("change",event=>{
    for(const room of filteredMaxAssignmentRooms()){
      if(event.target.checked)maxAssignmentRooms.add(room.id);
      else maxAssignmentRooms.delete(room.id);
    }
    renderMaxAssignment();
  });
  onOperation($("max-user-rooms-form"),"Сохраняем назначения…",async()=>{
    if(!selectedMaxStaff)return;
    const button=$("max-assignment-save");button.disabled=true;
    const notify=$("max-assignment-notify").checked;
    try{
      const result=await api("/api/max/users/"+encodeURIComponent(selectedMaxStaff.user_id)+"/rooms",
        "PUT",{room_ids:[...maxAssignmentRooms],notify});
      closeMaxAssignment();
      await refresh();
      if(result.notification_queued)toast("Кабинеты назначены. Уведомление добавлено в очередь MAX.");
      else if(notify)toast("Изменения сохранены. Новых назначений нет — уведомление не отправлено.");
      else toast("Назначения кабинетов сохранены.");
    }catch(error){toast(error.message,true);}
    finally{button.disabled=false;}
  });
}

function initMaxPairing(){
  $("close-pairing").addEventListener("click",()=>$("pairing-dialog").close());
  $("copy-pairing").addEventListener("click",async()=>{
    try{await navigator.clipboard.writeText("/bind "+$("pairing-code").textContent);toast("Команда скопирована");}
    catch{toast("Скопируйте команду вручную",true);}
  });
}

function initMaxToken(){
  onOperation($("max-settings-form"),"Сохраняем токен MAX…",async event=>{
    event.preventDefault();
    const value=$("setting-max-token").value.trim();
    if(!value){toast("Введите токен",true);return;}
    try{await api("/api/settings/max-token","PUT",{value});$("setting-max-token").value="";await loadSettings();toast("Токен сохранён");}
    catch(error){toast(error.message,true);}
  });
  onOperation($("clear-max-token"),"Отключаем MAX…",async()=>{
    if(!confirm("Отключить MAX-бота? Привязки кабинетов останутся."))return;
    try{await api("/api/settings/max-token","PUT",{value:null});await loadSettings();toast("MAX отключён");}
    catch(error){toast(error.message,true);}
  });
}

/* MAX code management: safe metadata and one-time secret reveal. */
function maxCodeVisibleRooms(){
  const building=$("max-codes-building").value;
  const needle=$("max-codes-search").value.trim().toLocaleLowerCase("ru");
  const coded=new Set(activeMaxCodes.map(c=>c.room_id));
  return rooms.filter(r=>(!building||r.building===building) &&
    (!$("max-codes-unbound").checked||!(r.max_users||[]).length) &&
    (!$("max-codes-no-code").checked||!coded.has(r.id)) &&
    [r.building,r.floor,r.name,r.kind].some(v=>String(v).toLocaleLowerCase("ru").includes(needle)));
}
function renderMaxCodes(){
  const available=new Set(rooms.map(r=>r.id));
  for(const id of selectedMaxRooms)if(!available.has(id))selectedMaxRooms.delete(id);
  const visible=maxCodeVisibleRooms(),list=$("max-codes-list"),coded=new Set(activeMaxCodes.map(c=>c.room_id));
  list.replaceChildren();
  for(const room of visible){
    const label=el("label","max-codes-room");
    const check=el("input");check.type="checkbox";check.checked=selectedMaxRooms.has(room.id);
    check.addEventListener("change",()=>{
      if(check.checked){
        if(selectedMaxRooms.size>=250){check.checked=false;toast("Максимум 250 кабинетов за один выпуск",true);return;}
        selectedMaxRooms.add(room.id);
      }else selectedMaxRooms.delete(room.id);
      renderMaxCodes();
    });
    const info=el("span","max-codes-room-text");
    info.append(el("strong","",room.name));
    info.append(el("small","",[
      room.building,room.floor?"этаж "+room.floor:"",
      (room.max_users||[]).length?"MAX: "+room.max_users.map(maxUserName).join(", "):"Нет привязки MAX",
      coded.has(room.id)?"Код уже выпущен":""
    ].filter(Boolean).join(" · ")));
    label.append(check,info);list.append(label);
  }
  if(!visible.length)list.append(el("p","max-codes-empty","Кабинеты не найдены. Измените фильтры."));
  const toggle=$("max-codes-select-all");
  const selectedVisible=visible.filter(r=>selectedMaxRooms.has(r.id)).length;
  toggle.checked=!!visible.length&&selectedVisible===visible.length;
  toggle.indeterminate=selectedVisible>0&&selectedVisible<visible.length;
  toggle.disabled=!visible.length;
  $("max-codes-count").textContent="Выбрано "+selectedMaxRooms.size+" из "+rooms.length+
    " активных · показано "+visible.length+
    (selectedMaxRooms.size>selectedVisible?" · скрыто фильтром "+(selectedMaxRooms.size-selectedVisible):"");
  $("download-max-codes").disabled=selectedMaxRooms.size===0;
}
function renderActiveMaxCodes(){
  const list=$("max-active-codes-list");list.replaceChildren();
  if(!activeMaxCodes.length){
    list.append(el("p","max-codes-empty","Действующих кодов нет."));
    return;
  }
  for(const item of activeMaxCodes){
    const record=el("div","max-code-record"),details=el("div","max-code-details");
    details.append(el("strong","",item.building+" / "+item.name));
    details.append(el("small","","Действует до "+when(item.expires_at)+" · значение не сохраняется"));
    const revoke=el("button","btn secondary","Отозвать");revoke.type="button";
    onOperation(revoke,"Отзываем код MAX…",async()=>{
      if(!confirm("Отозвать код для «"+item.name+"»? Сотрудник больше не сможет использовать этот код."))return;
      revoke.disabled=true;
      try{
        await api("/api/rooms/"+item.room_id+"/pairing","DELETE");
        issuedMaxCodes=issuedMaxCodes.filter(c=>c.id!==item.room_id);
        renderIssuedMaxCodes();await loadMaxCodes();toast("Код отозван");
      }catch(error){revoke.disabled=false;toast(error.message,true);}
    });
    record.append(details,revoke);list.append(record);
  }
}
function renderIssuedMaxCodes(){
  show("max-codes-issued",issuedMaxCodes.length>0);
  const list=$("max-codes-issued-list");list.replaceChildren();
  for(const code of issuedMaxCodes){
    const record=el("div","max-code-record"),details=el("div","max-code-details");
    details.append(el("strong","",code.building+" / "+code.name));
    details.append(el("span","max-code-secret",code.code));
    details.append(el("small","","Истекает "+when(code.expires_at)));
    const copy=el("button","btn secondary","Копировать /bind");copy.type="button";
    copy.addEventListener("click",()=>copyMaxText("/bind "+code.code));
    record.append(details,copy);list.append(record);
  }
}
async function copyMaxText(value){
  try{
    if(navigator.clipboard?.writeText)await navigator.clipboard.writeText(value);
    else{
      const field=document.createElement("textarea");
      field.value=value;field.style.position="fixed";field.style.opacity="0";
      document.body.append(field);field.select();
      const copied=document.execCommand("copy");field.remove();
      if(!copied)throw Error("clipboard");
    }
    toast("Команда скопирована");
  }catch{toast("Не удалось скопировать. Выделите код вручную.",true);}
}
function csvMaxCell(value){
  let s=String(value??"");
  if(/^[\s\x00-\x1f]*[=+\-@]/.test(s))s="'"+s;
  return '"'+s.replace(/"/g,'""')+'"';
}
function downloadIssuedMaxCodes(){
  if(!issuedMaxCodes.length)return;
  const rows=[
    ["Корпус","Этаж","Кабинет","ID кабинета","Код MAX","Команда боту","Действует до (UTC)"],
    ...issuedMaxCodes.map(c=>[c.building,c.floor,c.name,c.id,c.code,"/bind "+c.code,c.expires_at])
  ];
  const csv="\uFEFF"+rows.map(r=>r.map(csvMaxCell).join(";")).join("\r\n")+"\r\n";
  const url=URL.createObjectURL(new Blob([csv],{type:"text/csv;charset=utf-8"}));
  const link=document.createElement("a");
  link.href=url;link.download="termokontur-max-codes.csv";
  document.body.append(link);link.click();link.remove();
  setTimeout(()=>URL.revokeObjectURL(url),15000);
  toast("CSV с кодами сформирован");
}
async function loadMaxCodes(){
  try{
    activeMaxCodes=await api("/api/max/pairing-codes");
    renderActiveMaxCodes();renderMaxCodes();
  }catch(error){
    $("max-active-codes-list").replaceChildren(el("p","max-codes-empty","Не удалось загрузить список: "+error.message));
    toast(error.message,true);
  }
}

function initMaxCodes(){
  onOperation($("bulk-max-codes"),"Загружаем коды MAX…",async()=>{
    selectedMaxRooms.clear();issuedMaxCodes=[];activeMaxCodes=[];
    $("max-codes-search").value="";
    $("max-codes-unbound").checked=true;$("max-codes-no-code").checked=true;
    $("max-codes-building").replaceChildren();option($("max-codes-building"),"","Все корпуса");
    for(const building of [...new Set(rooms.map(r=>r.building))].sort())
      option($("max-codes-building"),building,building);
    $("max-codes-expiry").value="1440";
    $("max-codes-dialog").showModal();
    $("max-active-codes-list").replaceChildren(el("p","max-codes-empty","Загрузка действующих кодов…"));
    renderIssuedMaxCodes();renderMaxCodes();await loadMaxCodes();
  });
  $("max-codes-dialog").addEventListener("close",()=>{
    issuedMaxCodes=[];selectedMaxRooms.clear();renderIssuedMaxCodes();
  });
  $("close-max-codes").addEventListener("click",()=>$("max-codes-dialog").close());
  $("cancel-max-codes").addEventListener("click",()=>$("max-codes-dialog").close());
  for(const id of ["max-codes-building","max-codes-unbound","max-codes-no-code"])
    $(id).addEventListener("change",renderMaxCodes);
  $("max-codes-search").addEventListener("input",renderMaxCodes);
  onOperation($("max-codes-reload"),"Обновляем коды MAX…",loadMaxCodes);
  $("max-codes-clear").addEventListener("click",()=>{selectedMaxRooms.clear();renderMaxCodes();});
  $("max-codes-select-all").addEventListener("change",event=>{
    const visible=maxCodeVisibleRooms();
    if(event.target.checked){
      const toAdd=visible.filter(r=>!selectedMaxRooms.has(r.id));
      if(selectedMaxRooms.size+toAdd.length>250){
        toast("Выберите не более 250 кабинетов",true);renderMaxCodes();return;
      }
      for(const room of toAdd)selectedMaxRooms.add(room.id);
    }else for(const room of visible)selectedMaxRooms.delete(room.id);
    renderMaxCodes();
  });
  onOperation($("max-codes-form"),"Создаём коды MAX…",async event=>{
    event.preventDefault();
    if(!selectedMaxRooms.size){toast("Выберите кабинеты",true);return;}
    const replacing=activeMaxCodes.filter(c=>selectedMaxRooms.has(c.room_id));
    if(replacing.length&&!confirm("Для "+replacing.length+" кабинетов уже есть действующие коды. Создать новые и отозвать старые?"))return;
    const assigned=rooms.filter(r=>selectedMaxRooms.has(r.id)&&(r.max_users||[]).length);
    if(assigned.length&&!confirm("В "+assigned.length+" выбранных кабинетах уже есть привязки MAX. Новые коды разрешат привязать ещё пользователей. Продолжить?"))return;
    const button=$("download-max-codes");button.disabled=true;
    try{
      const data=await api("/api/max/pairing-codes","POST",{
        room_ids:[...selectedMaxRooms],validity_minutes:Number($("max-codes-expiry").value)
      });
      issuedMaxCodes=data.codes;
      selectedMaxRooms.clear();renderIssuedMaxCodes();
      await loadMaxCodes();
      $("max-codes-issued").scrollIntoView({block:"nearest",behavior:"smooth"});
      toast("Коды созданы. Скопируйте их или скачайте CSV до закрытия окна.");
    }catch(error){toast(error.message,true);}
    finally{renderMaxCodes();}
  });
  $("max-codes-copy-all").addEventListener("click",()=>{
    if(issuedMaxCodes.length)copyMaxText(issuedMaxCodes.map(c=>
      c.building+" / "+c.name+": /bind "+c.code).join("\n"));
  });
  $("max-codes-download-issued").addEventListener("click",downloadIssuedMaxCodes);
}

/* Live MAX connectivity and queue status: never expose stored credentials. */
async function loadMaxDiagnostics() {
  const target=$("max-diagnostics");
  if(!settingsCache?.max_token_configured) {
    target.textContent="Сохраните токен MAX и повторите проверку.";
    return;
  }
  target.textContent="Проверка MAX: токен, служба и способ получения событий…";
  try {
    const state=await api("/api/max/diagnostics");
    const lines=[];
    if(state.token_valid===true) {
      lines.push("Токен действителен"+(state.bot_username?" (бот @"+state.bot_username+")":"")+".");
    } else if(state.token_valid===false) {
      lines.push("MAX отклонил токен. Сохраните действующий токен бота.");
    } else if(state.api_check_error) {
      lines.push("MAX недоступен. Проверьте интернет и сертификаты сервера.");
    }
    if(!state.worker_running) {
      lines.push("Служба обработки MAX не отвечает. На сервере проверьте: systemctl status termokontur-max и journalctl -u termokontur-max -n 60.");
    } else if(state.worker_mode==="waiting_for_token") {
      lines.push("Служба MAX ожидает токен. Если он сохранён, повторите проверку через несколько секунд.");
    } else {
      lines.push("Служба MAX работает ("+(state.worker_mode==="webhook"?"приём через webhook":"Long Polling")+").");
    }
    if(state.mode==="webhook") {
      if(state.webhook_check_error) lines.push("Не удалось проверить подписку webhook в MAX.");
      else if(state.webhook_registered) lines.push("Webhook зарегистрирован в MAX.");
      else if(state.token_valid===true) lines.push("Webhook не найден. Проверьте публичный HTTPS-адрес.");
    } else {
      if(state.polling_blocked_by_webhook) {
        lines.push("У бота включён webhook. Отключите его или используйте отдельного бота для Long Polling.");
      } else if(state.webhook_check_error) {
        lines.push("Не удалось проверить webhook.");
      } else if(state.token_valid===true) {
        lines.push("Long Polling доступен.");
      }
    }
    if(state.mode==="polling") {
      const errors={
        tls_certificate:"Сервер не доверяет сертификату MAX. Установите корневые сертификаты по инструкции docs/max-long-polling.md.",
        authorization:"MAX отклонил подключение. Проверьте токен.",
        rate_limit:"MAX временно ограничил количество запросов. Бот повторит попытку.",
        connection:"Нет связи с API MAX из службы опроса. Проверьте DNS, прокси и исходящий HTTPS.",
        api_error:"MAX возвращает ошибку API; проверьте журнал службы.",
        invalid_response:"MAX возвращает неожиданный ответ; проверьте журнал службы.",
        event_processing:"Сообщение ещё обрабатывается. Повторная попытка — автоматически."
      };
      if(state.poll_last_error) lines.push(errors[state.poll_last_error]||"Ошибка опроса MAX, проверьте журнал службы.");
      if(state.poll_last_success_at) lines.push("Последний успешный опрос: "+when(state.poll_last_success_at)+".");
      else if(state.worker_running&&state.token_valid===true) lines.push("Сообщения с текущим токеном ещё не получены.");
    }
    lines.push("Неотправленных ответов: "+state.undelivered+".");
    if(state.parked_other_bots) lines.push("Ожидают подключения прежнего бота: "+state.parked_other_bots+".");
    if(state.repeatedly_failed) lines.push("Повторных ошибок доставки (10+): "+state.repeatedly_failed+".");
    if(state.permanently_failed) lines.push("Окончательно отклонено MAX: "+state.permanently_failed+". Проверьте доступность получателей.");
    if(state.stalled_events) lines.push("Зависших входящих событий: "+state.stalled_events+".");
    target.textContent=lines.join(" ");
  }catch(error) { target.textContent="Ошибка диагностики MAX: "+error.message; }
}

function initMaxDiagnostics(){
  onOperation($("check-max-diagnostics"),"Проверяем связь с MAX…",loadMaxDiagnostics);
}
