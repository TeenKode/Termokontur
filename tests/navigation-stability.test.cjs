"use strict";
// Browser-free regression tests for tab navigation and scroll stabilization.
const {test}=require("node:test");
const assert=require("node:assert/strict");
const fs=require("node:fs");
const vm=require("node:vm");
const path=require("node:path");
const root=path.resolve(__dirname,"..");
const source=["reports.js","notifications.js","updates.js","settings.js","max-admin.js","app.js"].map(name=>fs.readFileSync(path.join(root,"termokontur/static",name),"utf8")).join("\n");
const chartSource=fs.readFileSync(path.join(root,"termokontur/static/charts.js"),"utf8");
const styles=fs.readFileSync(path.join(root,"termokontur/static/style.css"),"utf8");

function extractFunction(name){
  const start=source.indexOf("function "+name+"(");
  assert.notEqual(start,-1,"Missing function "+name);
  const begin=source.indexOf("{",start);
  let depth=0;
  for(let i=begin;i<source.length;i++){
    if(source[i]==="{")depth++;
    else if(source[i]==="}"&&--depth===0)return source.slice(start,i+1);
  }
  throw Error("Unclosed function "+name);
}
function node({hidden=false,dataset={},top=0}={}){
  const classes=new Set();
  const attributes=new Map();
  return {hidden,dataset,classes,attributes,
    classList:{toggle:(name,on)=>on?classes.add(name):classes.delete(name),contains:(name)=>classes.has(name)},
    setAttribute:(key,value)=>attributes.set(key,value),
    removeAttribute:(key)=>attributes.delete(key),
    getBoundingClientRect:()=>({top:top-viewport.scrollY})
  };
}
let viewport;
function harness(){
  const names=["overview","readings","rooms","staff","reports","sensors","settings"];
  const categories=["summary","general","notifications","max","sensors","backups","security","updates"];
  const views=Object.fromEntries(names.map(name=>[name,node({hidden:name!=="overview"})]));
  const nav=Object.fromEntries(names.map(name=>[name,node()]));
  const panes=Object.fromEntries(categories.map(name=>[name,node({hidden:name!=="summary",dataset:{settingsView:name}})]));
  const buttons=Object.fromEntries(categories.map(name=>[name,node({dataset:{settingsTarget:name}})]));
  const appRoot=node({top:0}),layout=node({top:200}),settingsCategory={value:"summary"};
  const calls={scrolls:[],chart:0,backups:0,updates:0,max:0};
  viewport={scrollY:0,scrollPaddingTop:"12px",scrollTo:({top,behavior})=>{calls.scrolls.push({top,behavior});viewport.scrollY=top;}};
  const document={
    getElementById:(id)=>views[id.slice(5)],
    querySelector:(selector)=>{
      if(selector===".settings-layout")return layout;
      const tab=selector.match(/^\[data-tab="(.+)"\]$/);
      if(tab)return nav[tab[1]];
      const pane=selector.match(/^\.settings-pane\[data-settings-view="(.+)"\]$/);
      if(pane)return panes[pane[1]];
      throw Error("Unexpected selector: "+selector);
    },
    querySelectorAll:(selector)=>{
      if(selector==="[data-settings-view]")return Object.values(panes);
      if(selector===".settings-nav-button")return Object.values(buttons);
      throw Error("Unexpected selectorAll: "+selector);
    }
  };
  const ctx=vm.createContext({document,window:viewport,settingsCategory,
    getComputedStyle:()=>({scrollPaddingTop:viewport.scrollPaddingTop}),
    show:(id,yes)=>{views[id.slice(5)].hidden=!yes;},
    $:(id)=>id==="app-root"?appRoot:views[id.slice(5)],
    drawChart:()=>calls.chart++,
    loadBackups:()=>calls.backups++,
    loadUpdateStatus:()=>calls.updates++,
    loadMaxDiagnostics:()=>calls.max++,
    loadAudit:()=>{}
  });
  vm.runInContext('let selectedTab="overview",selectedSettingsCategory="summary";\n'+
    ["scrollToSectionStart","setTab","setSettingsCategory"].map(extractFunction).join("\n"),ctx);
  return {ctx,views,nav,panes,buttons,calls,settingsCategory};
}
test("main navigation scrolls to top before hiding a long section",()=>{
  const h=harness();
  viewport.scrollY=850;
  vm.runInContext('setTab("rooms")',h.ctx);
  assert.equal(viewport.scrollY,0);
  assert.deepEqual(h.calls.scrolls,[{top:0,behavior:"instant"}]);
  assert.equal(h.views.rooms.hidden,false);
  assert.equal(h.views.overview.hidden,true);
  assert.equal(h.nav.rooms.attributes.get("aria-current"),"page");
  assert.equal(h.nav.overview.attributes.has("aria-current"),false);
  viewport.scrollY=140;
  vm.runInContext('setTab("rooms")',h.ctx);
  assert.equal(viewport.scrollY,140,"repeat click must not move the page");
});
test("staff is a dedicated tab, accessible without scrolling through rooms",()=>{
  const h=harness();
  viewport.scrollY=1000;
  vm.runInContext('setTab("staff")',h.ctx);
  assert.equal(h.views.staff.hidden,false);
  assert.equal(h.views.rooms.hidden,true);
  assert.equal(h.nav.staff.attributes.get("aria-current"),"page");
  assert.equal(viewport.scrollY,0);
  vm.runInContext('setTab("rooms")',h.ctx);
  assert.equal(h.views.staff.hidden,true);
  assert.equal(h.views.rooms.hidden,false);
});
test("reports tab is accessible without shifting other navigation items",()=>{
  const h=harness();
  viewport.scrollY=500;
  vm.runInContext('setTab("reports")',h.ctx);
  assert.equal(h.views.reports.hidden,false);
  assert.equal(h.views.staff.hidden,true);
  assert.equal(h.nav.reports.attributes.get("aria-current"),"page");
  assert.equal(viewport.scrollY,0);
});
test("settings categories keep scroll steady and skip repeated data loads",()=>{
  const h=harness();
  viewport.scrollY=720;
  vm.runInContext('setSettingsCategory("backups")',h.ctx);
  assert.equal(viewport.scrollY,188);
  assert.equal(h.calls.scrolls[0].behavior,"instant");
  assert.equal(h.panes.backups.hidden,false);
  assert.equal(h.panes.summary.hidden,true);
  assert.equal(h.buttons.backups.attributes.get("aria-current"),"page");
  assert.equal(h.settingsCategory.value,"backups");
  assert.equal(h.calls.backups,1);
  viewport.scrollY=185;
  vm.runInContext('setSettingsCategory("backups")',h.ctx);
  assert.equal(viewport.scrollY,185);
  assert.equal(h.calls.backups,1);
  vm.runInContext('setSettingsCategory("security")',h.ctx);
  assert.equal(viewport.scrollY,185,"near the settings header do not force-scroll");
});
test("settings navigation leaves space above its picker for the mobile header",()=>{
  const h=harness();
  viewport.scrollPaddingTop="72px";
  viewport.scrollY=720;
  vm.runInContext('setSettingsCategory("backups")',h.ctx);
  assert.equal(viewport.scrollY,128);
  assert.equal(h.settingsCategory.value,"backups");
});
test("global feedback and busy indicators never change layout size",()=>{
  assert.match(styles,/html\{scrollbar-gutter:stable\}/);
  assert.match(styles,/\.panel-feedback\{position:fixed;/);
  assert.match(styles,/button\.operation-busy::before\{content:"";position:absolute;/);
  assert.doesNotMatch(styles,/scroll-snap-type:x proximity/);
  assert.doesNotMatch(styles,/transform:translateY\(4px\)/);
});

test("operation feedback follows the full action and preserves the button's disabled state",async()=>{
  const button=node(), operations=new Map();
  button.classList.add=name=>button.classes.add(name);
  button.classList.remove=name=>button.classes.delete(name);
  let calls=0, timer, finish;
  const work=new Promise(resolve=>{finish=resolve;});
  const context=vm.createContext({
    activeOperations:operations,
    setTimeout:callback=>{timer=callback;},clearTimeout:()=>{timer=undefined;},
    syncFeedbackContainer:()=>{},renderOperationStatus:()=>{},
    toast:message=>assert.fail(message)
  });
  const run=vm.runInContext("(async "+extractFunction("runOperation")+")",context);
  const action=()=>{calls++;return work;};
  const pending=run(button,"Сохраняем…",action);
  await run(button,"Сохраняем…",action);
  assert.equal(calls,1);
  assert.equal(button.attributes.get("aria-busy"),"true");
  assert.equal(operations.get(button).visible,false);
  timer();
  assert.equal(operations.get(button).visible,true);
  button.disabled=true;
  finish();
  await pending;
  assert.equal(operations.size,0);
  assert.equal(button.disabled,true);
  assert.equal(button.attributes.has("aria-disabled"),false);
});

test("refresh keeps the selected room in the manual reading form",()=>{
  function select(options,value){
    return {
      options,
      get value(){return value;},
      set value(next){value=this.options.includes(next)?next:"";},
      replaceChildren(){this.options=[];value="";}
    };
  }
  const fields={
    "building-filter":select(["","Школа"],"Школа"),
    "reading-filter":select(["","1","2"],"2"),
    "reading-room":select(["","1","2"],"2"),
    "reading-room-hint":{},
    "reading-save":{}
  };
  const rooms=[{id:1,building:"Школа",name:"101"},{id:2,building:"Школа",name:"102"}];
  const context=vm.createContext({
    rooms,
    buildingScope:"",
    $:id=>fields[id],
    show:(id,visible)=>{fields[id].hidden=!visible;},
    option:(field,value)=>{field.options.push(value);}
  });
  vm.runInContext(extractFunction("renderFilters")+"\nrenderFilters();",context);
  assert.equal(fields["reading-room"].value,"2");
  assert.equal(fields["reading-filter"].value,"2");
  rooms.pop();
  vm.runInContext("renderFilters()",context);
  assert.equal(fields["reading-room"].value,"");
  assert.equal(fields["reading-filter"].value,"");
});

test("clearing a report hides its table and disables both exports",()=>{
  const fields=Object.fromEntries([
    "report-results","report-kpis","report-export-excel","report-export-pdf","report-status"
  ].map(id=>[id,{}]));
  const context=vm.createContext({
    reportsData:{from:"2026-11-01",to:"2026-11-07"},
    $:id=>fields[id],
    show:(id,visible)=>{fields[id].hidden=!visible;}
  });
  vm.runInContext(
    [extractFunction("clearReport"),extractFunction("renderReport"),
      'clearReport("Не удалось сформировать отчёт.");'].join("\n"),context
  );
  assert.equal(fields["report-results"].hidden,true);
  assert.equal(fields["report-kpis"].hidden,true);
  assert.equal(fields["report-export-excel"].disabled,true);
  assert.equal(fields["report-export-pdf"].disabled,true);
  assert.equal(fields["report-status"].textContent,"Не удалось сформировать отчёт.");
});


// Shared trend axes must remain meaningful for constant/negative temperatures
// and a single measurement; humidity must always keep its physical scale.
test("trend scale and label spacing",()=>{
  const vm=require("node:vm");
  const start=chartSource.indexOf("function trendScale("),end=chartSource.indexOf("function renderTrend(");
  assert.ok(start>=0&&end>start);
  const {trendScale,trendTicks}=vm.runInNewContext(chartSource.slice(start,end)+";({trendScale,trendTicks})");
  for(const values of [[21],[21,22],[-12,-3],[16.3,38.4],[0]]){
    const a=trendScale(values);
    assert.ok(a.min<Math.min(...values)&&a.max>Math.max(...values));
    assert.ok(a.max-a.min>=4&&a.step>0);
  }
  const h=trendScale([],true);
  assert.equal(h.min,0);assert.equal(h.max,100);assert.equal(h.step,25);
  assert.equal(JSON.stringify(trendTicks([.5],200,60)),"[0]");
  assert.equal(JSON.stringify(trendTicks([0,.1,.2,.3,1],100,60)),"[0,4]");
  assert.equal(JSON.stringify(trendTicks([],100,60)),"[]");
});


test("API deadlines distinguish routine calls and large imports",()=>{
  const timeout=vm.runInNewContext("("+extractFunction("apiTimeout")+")");
  assert.equal(timeout("/api/daily"),30000);
  assert.equal(timeout("/api/sensors/firmware/bundled/retry"),120000);
  assert.equal(timeout("/api/backups/import"),300000);
  assert.match(source,/AbortSignal\.timeout\(apiTimeout\(path\)\)/);
});


test("notification audiences preserve MAX recipient selection and building filters",()=>{
  const vm=require("node:vm");
  const module=fs.readFileSync(path.join(root,"termokontur/static/notifications.js"),"utf8");
  const fields={"notification-audience":{value:"all"},"notification-building":{value:"A"}};
  const context=vm.createContext({$:id=>fields[id]});
  vm.runInContext(module,context);
  vm.runInContext(`notificationPeople=[
    {user_id:"1",missing:true,rooms:[{building:"A"},{building:"B"}]},
    {user_id:"2",missing:false,rooms:[{building:"B"}]},
    {user_id:"3",missing:true,rooms:[{building:"A"}]}
  ];notificationSelected.add("2");`,context);
  for(const [mode,expected] of [["all","1,2,3"],["missing","1,3"],["building","1,3"],["selected","2"]]){
    fields["notification-audience"].value=mode;
    assert.equal(vm.runInContext('notificationTargets().map(person=>person.user_id).join(",")',context),expected);
  }
  fields["notification-audience"].value="building";
  fields["notification-building"].value="B";
  assert.equal(vm.runInContext('notificationTargets().map(person=>person.user_id).join(",")',context),"1,2");
});


test("initial session probe completes before login controls become enabled",async()=>{
  const fields={"login-name":{disabled:true},password:{disabled:true},"login-form":{querySelector:()=>submit}};
  const submit={disabled:true};let complete;
  const calls=[];
  const context=vm.createContext({$:id=>fields[id],api:()=>new Promise(resolve=>{complete=resolve;}),enterLogin:()=>calls.push("login"),enterApp:()=>calls.push("app"),refresh:async()=>{}});
  const boot=vm.runInContext("(async "+extractFunction("bootPanel")+")",context);
  const pending=boot();
  assert.equal(fields.password.disabled,true);
  assert.equal(submit.disabled,true);
  complete({authenticated:false});
  await pending;
  assert.deepEqual(calls,["login"]);
  assert.equal(fields.password.disabled,false);
  assert.equal(submit.disabled,false);
});


test("update controls prevent duplicate or unavailable installations",()=>{
  const fields={"check-update":{},"install-update":{}};
  const context=vm.createContext({$:id=>fields[id]});
  vm.runInContext(fs.readFileSync(path.join(root,"termokontur/static/updates.js"),"utf8"),context);
  const scenarios=[
    [false,true,"idle",false,false,true,true],
    [true,false,"idle",false,false,false,true],
    [true,true,"idle",false,false,false,false],
    [true,true,"queued",false,false,true,true],
    [true,true,"installing",false,false,true,true],
    [true,true,"idle",true,false,true,true],
    [true,true,"idle",false,true,true,true],
    [true,true,"failed",false,false,false,false]
  ];
  for(const [supported,available,state,checking,requesting,check,install] of scenarios){
    vm.runInContext(`updateSupported=${supported};updateAvailable=${available};updateState=${JSON.stringify(state)};updateChecking=${checking};updateRequesting=${requesting};renderUpdateControls();`,context);
    assert.equal(fields["check-update"].disabled,check);
    assert.equal(fields["install-update"].disabled,install);
  }
  assert.equal(vm.runInContext("updateElapsed(3661)",context),"1 ч 1 мин");
  assert.equal(vm.runInContext("updateElapsed(61)",context),"1 мин 1 сек");
});


test("settings save keeps unsaved edits and sends only fields with their original baseline",async()=>{
  const form={dataset:{dirty:"true"},querySelector:()=>({id:"feedback"})};
  const context=vm.createContext({form,settingsCache:{organization_name:"Old",timezone:"Asia/Vladivostok",retention_days:30,reminder_days:[1]},document:{title:""},setFeedback:()=>{},toast:()=>{}});
  vm.runInContext(fs.readFileSync(path.join(root,"termokontur/static/settings.js"),"utf8"),context);
  let resolve,request;
  context.api=(path,method,body)=>{request={path,method,body};return new Promise(r=>{resolve=r;});};
  vm.runInContext(`renderSettingsSummary=()=>{};updateDirtyNavigation=()=>{};settingsBaseline.set(form,{...settingsCache});settingsEditRevision.set(form,1);`,context);
  const pending=vm.runInContext('saveSettingsPatch(form,{organization_name:"New"},"Saved")',context);
  assert.equal(request.path,"/api/settings");
  assert.equal(request.method,"PATCH");
  assert.equal(JSON.stringify(request.body),JSON.stringify({changes:{organization_name:"New"},expected:{organization_name:"Old"}}));
  vm.runInContext("settingsEditRevision.set(form,2)",context);
  resolve({organization_name:"New",timezone:"Asia/Vladivostok",retention_days:30,reminder_days:[1]});
  assert.equal(await pending,true);
  assert.equal(form.dataset.dirty,"true");
  assert.equal(vm.runInContext("settingsBaseline.get(form).organization_name",context),"New");
});


test("MAX admin name fallback and CSV formula protection",()=>{
  const context=vm.createContext({});
  vm.runInContext(fs.readFileSync(path.join(root,"termokontur/static/max-admin.js"),"utf8"),context);
  assert.equal(vm.runInContext('maxUserName({first_name:"Иван",last_name:"Иванов"})',context),"Иван Иванов");
  assert.equal(vm.runInContext('maxUserName({username:"demo",user_id:"123"})',context),"@demo");
  assert.equal(vm.runInContext('maxUserName({user_id:"123"})',context),"MAX ID 123");
  for(const value of ["=1+1","+1","-1","@SUM(A1)","  =2"]){
    context.value=value;
    assert.equal(vm.runInContext('csvMaxCell(value)',context),'"\''+value+'"');
  }
});
