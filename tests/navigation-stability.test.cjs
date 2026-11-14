"use strict";
// Browser-free regression tests for tab navigation and scroll stabilization.
const {test}=require("node:test");
const assert=require("node:assert/strict");
const fs=require("node:fs");
const vm=require("node:vm");
const path=require("node:path");
const root=path.resolve(__dirname,"..");
const source=fs.readFileSync(path.join(root,"termokontur/static/app.js"),"utf8");
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
  const names=["overview","readings","rooms","settings"];
  const categories=["summary","general","notifications","max","sensors","backups","security","updates"];
  const views=Object.fromEntries(names.map(name=>[name,node({hidden:name!=="overview"})]));
  const nav=Object.fromEntries(names.map(name=>[name,node()]));
  const panes=Object.fromEntries(categories.map(name=>[name,node({hidden:name!=="summary",dataset:{settingsView:name}})]));
  const buttons=Object.fromEntries(categories.map(name=>[name,node({dataset:{settingsTarget:name}})]));
  const topbar=node({top:0}),layout=node({top:200});
  const calls={scrolls:[],chart:0,backups:0,updates:0,max:0};
  viewport={scrollY:0,scrollTo:({top,behavior})=>{calls.scrolls.push({top,behavior});viewport.scrollY=top;}};
  const document={
    getElementById:(id)=>views[id.slice(5)],
    querySelector:(selector)=>{
      if(selector===".topbar")return topbar;
      if(selector===".settings-layout")return layout;
      const tab=selector.match(/^\\[data-tab="(.+)"\\]$/);
      if(tab)return nav[tab[1]];
      const pane=selector.match(/^\\.settings-pane\\[data-settings-view="(.+)"\\]$/);
      if(pane)return panes[pane[1]];
      throw Error("Unexpected selector: "+selector);
    },
    querySelectorAll:(selector)=>{
      if(selector==="[data-settings-view]")return Object.values(panes);
      if(selector===".settings-nav-button")return Object.values(buttons);
      throw Error("Unexpected selectorAll: "+selector);
    }
  };
  const ctx=vm.createContext({document,window:viewport,
    show:(id,yes)=>{views[id.slice(5)].hidden=!yes;},
    $:(id)=>views[id.slice(5)],
    drawChart:()=>calls.chart++,
    loadBackups:()=>calls.backups++,
    loadUpdateStatus:()=>calls.updates++,
    loadMaxDiagnostics:()=>calls.max++
  });
  vm.runInContext('let selectedTab="overview",selectedSettingsCategory="summary";\\n'+
    ["scrollToSectionStart","setTab","setSettingsCategory"].map(extractFunction).join("\\n"),ctx);
  return {ctx,views,nav,panes,buttons,calls};
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
test("settings categories keep scroll steady and skip repeated data loads",()=>{
  const h=harness();
  viewport.scrollY=720;
  vm.runInContext('setSettingsCategory("backups")',h.ctx);
  assert.equal(viewport.scrollY,188);
  assert.equal(h.calls.scrolls[0].behavior,"instant");
  assert.equal(h.panes.backups.hidden,false);
  assert.equal(h.panes.summary.hidden,true);
  assert.equal(h.buttons.backups.attributes.get("aria-current"),"page");
  assert.equal(h.calls.backups,1);
  viewport.scrollY=185;
  vm.runInContext('setSettingsCategory("backups")',h.ctx);
  assert.equal(viewport.scrollY,185);
  assert.equal(h.calls.backups,1);
  vm.runInContext('setSettingsCategory("security")',h.ctx);
  assert.equal(viewport.scrollY,185,"near the settings header do not force-scroll");
});
test("global feedback and busy indicators never change layout size",()=>{
  assert.match(styles,/html\\{scrollbar-gutter:stable\\}/);
  assert.match(styles,/\\.operation-status\\{position:fixed;/);
  assert.match(styles,/button\\.operation-busy::before\\{content:"";position:absolute;/);
  assert.doesNotMatch(styles,/scroll-snap-type:x proximity/);
  assert.doesNotMatch(styles,/transform:translateY\\(4px\\)/);
  assert.match(source,/button\\.matches\\("\\[data-tab\\], \\[data-settings-target\\]"\\)/);
});
