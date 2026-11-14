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
    document.querySelector('meta[name="theme-color"]')?.setAttribute("content",dark?"#0c1726":"#101d31");
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
      for(const btn of buttons){btn.textContent=dark?"☀":"☾";btn.setAttribute("aria-label",label);btn.title=label;}
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