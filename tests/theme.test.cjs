"use strict";
const {test}=require("node:test"),assert=require("node:assert/strict"),fs=require("node:fs"),vm=require("node:vm"),path=require("node:path");
const root=path.resolve(__dirname,"..");
const src=fs.readFileSync(path.join(root,"termokontur/static/theme.js"),"utf8");
function setup(saved,osDark=true){
  const registered={},data=new Map(saved===undefined?[]:[["termokontur-theme",saved]]);
  function item(value){const events={};return {value,checked:false,attrs:{},textContent:"",title:"",
    addEventListener:(type,fn)=>events[type]=fn,setAttribute(name,v){this.attrs[name]=v;},
    run:type=>events[type]?.()};}
  const buttons=[item(""),item("")],choices=["system","light","dark"].map(item);
  const summary={textContent:""},view={hidden:false},meta={setAttribute(k,v){this[k]=v;}};
  const document={documentElement:{dataset:{}},addEventListener:(name,fn)=>(registered[name]??=[]).push(fn),
    dispatchEvent:e=>(registered[e.type]||[]).forEach(fn=>fn(e)),
    querySelector:()=>meta,querySelectorAll:s=>s==="[data-theme-toggle]"?buttons:choices,
    getElementById:id=>id==="settings-summary-theme"?summary:id==="view-readings"?view:null};
  const media={matches:osDark,addEventListener:(type,fn)=>media.change=fn};
  let redraws=0;
  const window={localStorage:{getItem:key=>data.get(key)??null,setItem:(key,v)=>data.set(key,v)},
    matchMedia:()=>media,addEventListener:(type,fn)=>window[type]=fn,drawChart:()=>redraws++};
  vm.runInNewContext(src,{window,document,CustomEvent:class{constructor(type,opt){this.type=type;this.detail=opt.detail;}}});
  document.dispatchEvent({type:"DOMContentLoaded"});
  return {window,document,media,buttons,choices,data,summary,meta,get redraws(){return redraws;}};
}
test("system mode switches automatically and repaints chart",()=>{
  const h=setup();
  assert.equal(h.document.documentElement.dataset.theme,"dark");
  assert.equal(h.choices[0].checked,true);
  assert.equal(h.meta.content,"#0c1726");
  h.media.matches=false;h.media.change();
  assert.equal(h.document.documentElement.dataset.theme,"light");
  assert.equal(h.buttons[0].attrs["aria-label"],"Включить тёмную тему");
  assert.ok(h.redraws>0);
});
test("dark choice persists and quick switch works",()=>{
  const h=setup("light",true);
  assert.equal(h.document.documentElement.dataset.theme,"light");
  h.choices[2].checked=true;h.choices[2].run("change");
  assert.equal(h.data.get("termokontur-theme"),"dark");
  assert.equal(h.summary.textContent,"Тёмная");
  h.media.matches=false;h.media.change();
  assert.equal(h.document.documentElement.dataset.theme,"dark");
  h.buttons[1].run("click");
  assert.equal(h.document.documentElement.dataset.theme,"light");
  assert.equal(h.data.get("termokontur-theme"),"light");
});
test("invalid and cross-tab preferences",()=>{
  const h=setup("invalid",false);
  assert.equal(h.window.termokonturTheme.getPreference(),"system");
  h.window.termokonturTheme.setPreference("invalid");
  assert.equal(h.document.documentElement.dataset.theme,"light");
  h.data.set("termokontur-theme","dark");
  h.window.storage({key:"termokontur-theme"});
  assert.equal(h.document.documentElement.dataset.theme,"dark");
});
test("theme bootstraps before stylesheet, includes dialogs and canvas colors",()=>{
  const html=fs.readFileSync(path.join(root,"termokontur/static/index.html"),"utf8");
  const css=fs.readFileSync(path.join(root,"termokontur/static/style.css"),"utf8");
  const js=fs.readFileSync(path.join(root,"termokontur/static/app.js"),"utf8");
  const py=fs.readFileSync(path.join(root,"termokontur/app.py"),"utf8");
  assert.ok(html.indexOf("/static/theme.js")<html.indexOf("/static/style.css"));
  assert.equal((html.match(/data-theme-toggle/g)||[]).length,2);
  for(const value of ["system","light","dark"])assert.match(html,new RegExp('name="theme-preference" value="'+value+'"'));
  assert.match(css,/html\[data-theme="dark"\] dialog/);
  assert.match(css,/html\[data-theme="dark"\] \.status\.below/);
  assert.match(css,/html\[data-theme="dark"\] \.heatmap-table/);
  assert.match(js,/const chartGrid=dark\?/);
  assert.match(js,/const historyGrid=dark\?/);
  assert.match(py,/\("app\.js", "style\.css", "theme\.js"\)/);
});
