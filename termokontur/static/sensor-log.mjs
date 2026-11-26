export function createSerialLog(output, endButton, maxLength) {
  const text = output.ownerDocument.createTextNode("");
  output.replaceChildren(text);
  const scrollToEnd = () => { output.scrollTop = output.scrollHeight; };
  endButton.addEventListener("click", scrollToEnd);
  const tools=installLogTools(output,text);

  return {
    append(line) {
      if (!line) return;
      const top = output.scrollTop;
      const follow = output.scrollHeight - output.clientHeight - top <= 2;
      text.appendData(line + "\n");
      const excess = text.length - maxLength;
      const heightBeforeTrim = !follow && excess > 0 ? output.scrollHeight : 0;
      if (excess > 0) text.deleteData(0, excess);

      if (follow) scrollToEnd();
      else output.scrollTop = top - (excess > 0 ? heightBeforeTrim - output.scrollHeight : 0);
      endButton.disabled = false;
      tools.refresh();
    },
    clear() {
      text.data = "";
      output.scrollTop = 0;
      endButton.disabled = true;
      tools.refresh();
    },
  };
}

/* Filter a separate view of the bounded, already-redacted buffer.
   The underlying log and its independent scroll position are never replaced. */
function installLogTools(output,text){
  const doc=output.ownerDocument,id=output.id;
  let toolbar=doc.getElementById(id+"-tools");
  if(toolbar)toolbar.remove();
  doc.getElementById(id+"-matches")?.remove();
  toolbar=doc.createElement("div");toolbar.id=id+"-tools";toolbar.className="tk-log-tools";
  const search=doc.createElement("input");search.type="search";search.placeholder="Поиск по журналу";search.setAttribute("aria-label","Поиск по журналу");
  const level=doc.createElement("select");level.setAttribute("aria-label","Уровень журнала");
  for(const [value,label] of [["","Все строки"],["error","Ошибки"],["warn","Предупреждения"],["info","Информация"]]){
    const option=doc.createElement("option");option.value=value;option.textContent=label;level.append(option);
  }
  const status=doc.createElement("span");status.className="hint";status.setAttribute("role","status");
  const result=doc.createElement("pre");result.className="serial-log tk-log-results";result.id=id+"-matches";result.hidden=true;result.tabIndex=0;result.setAttribute("aria-label","Найденные строки");
  const btn=(label,fn)=>{const node=doc.createElement("button");node.type="button";node.className="btn secondary";node.textContent=label;node.addEventListener("click",fn);return node;};
  const levelOf=line=>{
    const msg=line.replace(/^\[[^\]]+\]\s*/,"");
    if(/^(?:E\s*\(|ERROR\b)|\b(?:ERROR|ESP_ERR_[A-Z_]+|failed)\b|ошибка|не удалось/i.test(msg))return "error";
    if(/^(?:W\s*\(|WARN(?:ING)?\b)|предупреждение/i.test(msg))return "warn";
    return "info";
  };
  function refresh(){
    const q=search.value.trim().toLocaleLowerCase("ru"),mode=level.value;
    const active=Boolean(q||mode);
    result.hidden=!active;
    if(!active){result.textContent="";status.textContent="";return;}
    const rows=text.data.split("\n").filter(line=>line&&(!q||line.toLocaleLowerCase("ru").includes(q))&&(!mode||levelOf(line)===mode));
    result.textContent=rows.join("\n");status.textContent="Найдено строк: "+rows.length;
  }
  search.addEventListener("input",refresh);level.addEventListener("change",refresh);
  const copy=btn("Копировать",async()=>{
    try{await doc.defaultView.navigator.clipboard.writeText(result.hidden?text.data:result.textContent);status.textContent="Скопировано";}
    catch{status.textContent="Не удалось скопировать. Выделите текст вручную.";}
  });
  toolbar.append(search,level,copy);
  const frame=output.parentElement;
  if(typeof frame.requestFullscreen==="function"){
    const full=btn("Развернуть",async()=>{try{if(doc.fullscreenElement===frame)await doc.exitFullscreen();else await frame.requestFullscreen();}catch{status.textContent="Полноэкранный режим недоступен.";}});
    toolbar.append(full);
    // No per-log document listeners are retained when the USB log is recreated.
    frame.onfullscreenchange=()=>{full.textContent=doc.fullscreenElement===frame?"Свернуть":"Развернуть";};
  }
  toolbar.append(status);output.before(toolbar);output.after(result);
  return {refresh};
}
