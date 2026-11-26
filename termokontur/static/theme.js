"use strict";
(() => {
  const key="termokontur-theme", allowed=new Set(["system","light","dark"]);
  const media=typeof window.matchMedia==="function"?window.matchMedia("(prefers-color-scheme: dark)"):null;
  function read(){try{const value=window.localStorage.getItem(key);return allowed.has(value)?value:"system";}catch{return "system";}}
  let preference=read();
  const isDark=()=>preference==="dark"||(preference==="system"&&!!media?.matches);
  function apply(){
    const dark=isDark();
    document.documentElement.dataset.theme=dark?"dark":"light";
    document.documentElement.dataset.themePreference=preference;
    document.querySelector('meta[name="theme-color"]')?.setAttribute("content",dark?"#10161c":"#eff2f5");
    document.dispatchEvent(new CustomEvent("termokontur:themechange",{detail:{preference,effective:dark?"dark":"light"}}));
  }
  function setPreference(value){
    if(!allowed.has(value))return;
    preference=value;
    try{window.localStorage.setItem(key,value);}catch{}
    apply();
  }
  window.termokonturTheme={getPreference:()=>preference,isDark,setPreference};
  if(media){
    const changed=()=>{if(preference==="system")apply();};
    if(media.addEventListener)media.addEventListener("change",changed);
    else media.addListener?.(changed);
  }
  window.addEventListener("storage",e=>{if(e.key===key){preference=read();apply();}});
  document.addEventListener("DOMContentLoaded",()=>{
    const buttons=document.querySelectorAll("[data-theme-toggle]");
    const choices=document.querySelectorAll('input[name="theme-preference"]');
    function sync(){
      const dark=isDark(), label=dark?"Включить светлую тему":"Включить тёмную тему";
      for(const btn of buttons){btn.innerHTML=dark?'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M2 12h2m16 0h2M5 5l2 2m10 10 2 2M19 5l-2 2M7 17l-2 2"/></svg>':'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round" aria-hidden="true"><path d="M20.8 14.4A9 9 0 0 1 9.6 3.2a9 9 0 1 0 11.2 11.2Z"/></svg>';btn.setAttribute("aria-label",label);btn.title=label;}
      for(const choice of choices)choice.checked=choice.value===preference;
      const summary=document.getElementById("settings-summary-theme");
      if(summary)summary.textContent=({system:"Как на устройстве",light:"Светлая",dark:"Тёмная"})[preference];
    }
    for(const btn of buttons)btn.addEventListener("click",()=>setPreference(isDark()?"light":"dark"));
    for(const choice of choices)choice.addEventListener("change",()=>{if(choice.checked)setPreference(choice.value);});
    document.addEventListener("termokontur:themechange",()=>{
      sync();
      const view=document.getElementById("view-readings");
      if(view&&!view.hidden&&typeof window.drawChart==="function")window.drawChart();
      const overview=document.getElementById("view-overview");
      if(overview&&!overview.hidden&&typeof window.drawHistoryChart==="function")window.drawHistoryChart();
    });
    sync();
  });
  apply();
})();
/* Browser-local view preferences; never sent to the API. */
(() => {
  const root=document.documentElement;
  const read=(key,fallback)=>{try{return localStorage.getItem(key)||fallback;}catch{return fallback;}};
  const save=(key,value)=>{try{localStorage.setItem(key,value);}catch{}};
  const density=read("termokontur-density","normal");
  root.dataset.density=["compact","normal","wide"].includes(density)?density:"normal";
  const media=window.matchMedia?.("(min-width: 1024px) and (max-width: 1439px)");
  let navPreference=read("termokontur-navigation","auto");
  const nav=()=>navPreference==="compact"||navPreference==="full"?navPreference:media?.matches?"compact":"full";
  root.dataset.navigation=nav();
  document.addEventListener("DOMContentLoaded",()=>{
    const toggle=document.getElementById("tk-collapse"), select=document.getElementById("tk-density");
    function syncNavigation(){const compact=nav()==="compact";root.dataset.navigation=compact?"compact":"full";if(toggle){const label=compact?"Развернуть меню":"Свернуть меню";toggle.textContent=label;toggle.setAttribute("aria-label",label);toggle.setAttribute("aria-pressed",String(compact));}}
    toggle?.addEventListener("click",()=>{navPreference=nav()==="compact"?"full":"compact";save("termokontur-navigation",navPreference);syncNavigation();window.dispatchEvent(new Event("resize"));});
    media?.addEventListener?.("change",syncNavigation);
    syncNavigation();
    if(select){select.value=root.dataset.density;select.addEventListener("change",()=>{if(["compact","normal","wide"].includes(select.value)){root.dataset.density=select.value;save("termokontur-density",select.value);}});}
    const view=document.getElementById("tk-sensor-view");
    if(view){const stored=read("termokontur-sensor-view","cards");view.value=stored==="table"?"table":"cards";
      const sync=()=>{const table=view.value==="table";document.getElementById("sensor-cards").hidden=table;document.getElementById("tk-sensor-table").hidden=!table;};
      view.addEventListener("change",()=>{save("termokontur-sensor-view",view.value);sync();});sync();}
  });
})();
