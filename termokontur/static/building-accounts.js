const buildingAccountForm = $("building-account-form");

function editBuildingAccount(account = null) {
  const name = $("building-account-name"), password = $("building-account-password");
  buildingAccountForm.reset();
  name.value = account?.username || "";
  name.readOnly = Boolean(account);
  password.required = !account;
  $("building-account-building").value = account?.building || "";
  $("building-account-max").value = account?.max_user_id || "";
  $("building-account-hint").textContent = account
    ? "Оставьте пароль пустым, чтобы сохранить действующий."
    : "Задайте пароль и передайте его ответственному.";
  show("building-account-reset", Boolean(account));
}

async function loadBuildingAccounts() {
  try {
    const [data, people] = await Promise.all([api("/api/building-accounts"),api("/api/max/users")]);
    const building = $("building-account-building"), recipient = $("building-account-max");
    building.replaceChildren();option(building,"","Выберите корпус");
    for(const name of data.buildings)option(building,name,name);
    recipient.replaceChildren();option(recipient,"","Без оповещений");
    for(const person of people)option(recipient,person.user_id,maxUserName(person));
    const list = $("building-account-list");
    list.replaceChildren();
    for(const account of data.accounts) {
      const row=el("div","report-schedule-entry");
      const info=el("div","report-schedule-details");
      info.append(el("strong","",account.username),el("small","",account.building));
      const actions=el("div","report-schedule-actions");
      const edit=el("button","btn secondary","Изменить");
      edit.type="button";
      edit.addEventListener("click",()=>editBuildingAccount(account));
      const remove=el("button","btn secondary","Удалить");
      remove.type="button";
      remove.addEventListener("click",async()=>{
        if(!confirm("Удалить доступ для "+account.username+"?"))return;
        try{
          await api("/api/building-accounts/"+encodeURIComponent(account.username),"DELETE");
          await loadBuildingAccounts();
          toast("Доступ отозван");
        }catch(error){toast(error.message,true);}
      });
      actions.append(edit,remove);row.append(info,actions);list.append(row);
    }
    editBuildingAccount();
  } catch(error){toast(error.message,true);}
}

$("building-account-reset").addEventListener("click",()=>editBuildingAccount());
buildingAccountForm.addEventListener("submit",async event=>{
  event.preventDefault();
  const username=$("building-account-name").value.trim();
  const payload={
    building:$("building-account-building").value,
    password:$("building-account-password").value||null,
    max_user_id:$("building-account-max").value||null,
  };
  try{
    await api("/api/building-accounts/"+encodeURIComponent(username),"PUT",payload);
    await loadBuildingAccounts();
    toast("Учётная запись сохранена");
  }catch(error){toast(error.message,true);}
});
