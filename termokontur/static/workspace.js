"use strict";
// Non-destructive, browser-local presentation controls; no API data or credentials stored.
(() => {
  const collator=new Intl.Collator("ru",{numeric:true,sensitivity:"base"});
  const read=key=>{try{return JSON.parse(localStorage.getItem("tk-ui-"+key)||"null");}catch{return null;}};
  const save=(key,value)=>{try{localStorage.setItem("tk-ui-"+key,JSON.stringify(value));}catch{}};
  const make=(tag,cls,text)=>{const node=document.createElement(tag);if(cls)node.className=cls;if(text)node.textContent=text;return node;};
  function button(label,callback){const b=make("button","btn secondary",label);b.type="button";b.addEventListener("click",callback);return b;}
  function enhanceTable(table,key,sortable,optional){
    if(!table||!table.tHead||!table.tBodies.length)return;
    const body=table.tBodies[0],heads=[...table.tHead.rows[0].cells];
    const pref=read(key)||{};
    let col=sortable.includes(pref.col)?pref.col:null,dir=pref.dir===-1?-1:1;
    const hidden=new Set(Array.isArray(pref.hidden)?pref.hidden.filter(i=>optional.includes(i)):[]);
    const order=new WeakMap();let seq=0;
    const scroll=table.closest(".table-scroll");
    const tools=make("div","tk-table-tools");
    const label=make("span","muted","Строк: ");
    const count=make("span","tk-row-count","0");label.append(count);tools.append(label);
    const select=make("details","tk-column-picker");select.append(make("summary","","Столбцы"));
    const choices=make("div","tk-column-choices");
    const checks=new Map();
    for(const i of optional){
      const l=make("label"),input=make("input");input.type="checkbox";input.checked=!hidden.has(i);
      l.append(input,document.createTextNode(heads[i].textContent));choices.append(l);checks.set(i,input);
      input.addEventListener("change",()=>{if(input.checked)hidden.delete(i);else hidden.add(i);apply();persist();});
    }
    select.append(choices);tools.append(select);
    const reset=button("Сбросить",()=>{col=null;dir=1;hidden.clear();for(const input of checks.values())input.checked=true;apply();persist();});tools.append(reset);
    scroll.before(tools);
    const sortButtons=new Map();
    for(const i of sortable){
      const th=heads[i],label=th.textContent;
      const b=button(label,()=>{if(col===i)dir=-dir;else{col=i;dir=1;}apply();persist();});
      b.className="tk-sort";th.replaceChildren(b);sortButtons.set(i,b);
    }
    function persist(){save(key,{col,dir,hidden:[...hidden]});}
    function keyOf(row,i){
      const cell=row.cells[i];
      if(!cell)return "";
      if(cell.dataset.sortValue!==undefined)return cell.dataset.sortValue;
      return cell.textContent.trim();
    }
    function apply(){
      observer.disconnect();
      const rows=[...body.rows].filter(row=>row.cells.length===heads.length);
      for(const row of rows)if(!order.has(row))order.set(row,seq++);
      const sorted=[...rows].sort((x,y)=>{
        if(col===null)return order.get(x)-order.get(y);
        const av=keyOf(x,col),bv=keyOf(y,col);
        const missing=v=>v===""||v==="—";
        if(missing(av)!==missing(bv))return missing(av)?1:-1;
        const an=Number(av.replace(",", ".")),bn=Number(bv.replace(",", "."));
        const cmp=Number.isFinite(an)&&Number.isFinite(bn)?an-bn:collator.compare(av,bv);
        return dir*cmp||order.get(x)-order.get(y);
      });
      if(sorted.some((row,i)=>row!==rows[i])){
        const top=scroll.scrollTop,left=scroll.scrollLeft;
        for(const row of sorted)body.append(row);
        scroll.scrollTop=top;scroll.scrollLeft=left;
      }
      for(let i=0;i<heads.length;i++){
        heads[i].hidden=hidden.has(i);
        for(const row of rows)row.cells[i].hidden=hidden.has(i);
        if(sortButtons.has(i)){
          heads[i].setAttribute("aria-sort",col===i?(dir===1?"ascending":"descending"):"none");
          sortButtons.get(i).dataset.direction=col===i?(dir===1?"asc":"desc"):"";
        }
      }
      for(const row of body.rows)if(row.cells.length===1&&row.cells[0].colSpan>1)row.cells[0].colSpan=heads.length-hidden.size;
      count.textContent=String(rows.filter(row=>!row.hidden).length);
      observer.observe(body,{childList:true,subtree:true,attributes:true,attributeFilter:["hidden"]});
    }
    const observer=new MutationObserver(apply);apply();
  }
  function expandCharts(){
    for(const id of ["history-chart","reading-chart","sensor-chart","report-temperature-chart"]){
      const canvas=document.getElementById(id),frame=canvas?.parentElement;
      if(!frame||typeof frame.requestFullscreen!=="function")continue;
      const b=button("Развернуть график",async()=>{try{if(document.fullscreenElement===frame)await document.exitFullscreen();else await frame.requestFullscreen();}catch{b.textContent="Полноэкранный режим недоступен";}});
      b.classList.add("tk-chart-expand");frame.prepend(b);
      document.addEventListener("fullscreenchange",()=>{b.textContent=document.fullscreenElement===frame?"Свернуть график":"Развернуть график";window.dispatchEvent(new Event("resize"));});
    }
  }
  document.addEventListener("DOMContentLoaded",()=>{
    enhanceTable(document.querySelector(".latest-table"),"overview",[0,1,2,4],[3,4]);
    enhanceTable(document.querySelector(".readings-table"),"readings",[0,1,2,3,4],[3,4]);
    enhanceTable(document.querySelector(".rooms-table"),"rooms",[0,1,2,3],[2,3,4]);
    enhanceTable(document.querySelector("#tk-sensor-table table"),"sensors",[0,1,2,3,4,5,6],[3,4,5,6]);
    expandCharts();
    // Remember non-sensitive choices only. Search text, room IDs and credentials
    // deliberately stay in memory, not in persistent browser storage.
    for(const id of ["history-period","overview-status"]){
      const select=document.getElementById(id),saved=read(id);
      if(select&&[...select.options].some(option=>option.value===saved))select.value=saved;
      select?.addEventListener("change",()=>save(id,select.value));
    }
    window.addEventListener("beforeunload",event=>{
      if(!document.querySelector('.settings-pane form[data-dirty="true"]'))return;
      event.preventDefault();event.returnValue="";
    });
  });
})();
