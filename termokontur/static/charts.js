"use strict";
// Shared chart renderer for overview, reports, readings and sensor history.
function trendScale(values,percent=false){
  if(percent)return {min:0,max:100,step:25};
  const lo=Math.min(...values),hi=Math.max(...values);
  const span=Math.max(4,hi-lo+1);
  const target=span/4,power=10**Math.floor(Math.log10(target));
  const steps=[1,2,2.5,5,10,20].map(v=>v*power);
  for(const step of steps){
    if(step<target)continue;
    const min=Math.floor((lo-.5)/step)*step;
    if(min+4*step>=hi+.5)return {min,max:min+4*step,step};
  }
  return {min:lo-1,max:hi+1,step:(hi-lo+2)/4};
}
function trendTicks(positions,width,labelWidth){
  if(!positions.length)return [];
  if(positions.length===1)return [0];
  const last=positions.length-1,available=Math.max(1,Math.floor(width/labelWidth));
  if(width<labelWidth)return [last];
  const chosen=[0];
  for(let i=1;i<last;i++){
    if(chosen.length>=available)break;
    if((positions[i]-positions[chosen.at(-1)])*width>=labelWidth&&
       (positions[last]-positions[i])*width>=labelWidth)chosen.push(i);
  }
  if((positions[last]-positions[chosen.at(-1)])*width>=labelWidth)chosen.push(last);
  return chosen;
}
const trendWindows=new WeakMap();
function renderTrend(canvas,options){
  const raw=options;const count=raw.points.length;
  let state=trendWindows.get(canvas);
  if(!state){state={size:0};trendWindows.set(canvas,state);}
  if(count<15)state.size=0;
  const size=state.size&&Math.min(state.size,count),offset=size?Math.max(0,count-size):0;
  if(offset)options={...raw,points:raw.points.slice(offset),series:raw.series.map(s=>({...s,values:s.values.slice(offset)})),contiguous:raw.contiguous?i=>raw.contiguous(i+offset):undefined};
  const width=canvas.clientWidth,height=canvas.clientHeight,dpr=window.devicePixelRatio||1;
  if(!width||!height)return;
  const ctx=canvas.getContext("2d");if(!ctx)return;
  canvas.width=Math.round(width*dpr);canvas.height=Math.round(height*dpr);
  ctx.setTransform(dpr,0,0,dpr,0,0);
  const dark=document.documentElement.dataset.theme==="dark";
  const ink=dark?"#c6d0da":"#42576a",grid=dark?"#34414d":"#d6dee6",surface=dark?"#1b242d":"#fff";
  const frame=canvas.parentElement;
  let controls=frame.querySelector(".tk-chart-range");
  if(!controls){
    controls=el("div","tk-chart-range");
    const label=el("label","","Масштаб");
    const range=el("select");range.setAttribute("aria-label","Масштаб графика");
    for(const [value,name] of [[0,"Весь период"],[14,"Последние 14 точек"],[7,"Последние 7 точек"]]){
      const option=el("option","",name);option.value=String(value);range.append(option);
    }
    label.append(range);controls.append(label);canvas.before(controls);
  }
  const range=controls.querySelector("select");
  range.value=String(state.size);range.onchange=()=>{state.size=Number(range.value);renderTrend(canvas,raw);};
  controls.hidden=count<15;
  let live=frame.querySelector(".tk-chart-live");
  if(!live){live=el("span","tk-visually-hidden tk-chart-live");live.setAttribute("role","status");frame.append(live);}
  let tooltip=frame.querySelector(".trend-tooltip")||frame.querySelector("#history-tooltip");
  if(!tooltip){tooltip=el("div","trend-tooltip");frame.append(tooltip);}
  tooltip.classList.add("trend-tooltip");tooltip.hidden=true;
  canvas.onpointermove=canvas.onpointerleave=canvas.onpointerdown=canvas.onkeydown=canvas.onblur=null;
  const data=options.points,series=options.series.filter(s=>s.values.some(Number.isFinite));
  const primary=series.filter(s=>s.unit!=="%"),humidity=series.filter(s=>s.unit==="%");
  const dual=primary.length>0&&humidity.length>0;
  const scale=trendScale(primary.flatMap(s=>s.values.filter(Number.isFinite)),!primary.length);
  const secondary=trendScale([],true);
  const left=width<320?47:58,right=width-(dual?47:20),top=29,bottom=height-35;
  const plotWidth=right-left,plotHeight=bottom-top;
  const times=data.map((p,i)=>Number.isFinite(p.time)?p.time:i);
  const first=Math.min(...times),last=Math.max(...times);
  const positions=times.map(t=>last===first?.5:(t-first)/(last-first));
  const x=i=>left+positions[i]*plotWidth;
  const y=(v,s)=>{const a=dual&&s.unit==="%"?secondary:scale;return bottom-(v-a.min)/(a.max-a.min)*plotHeight;};
  const format=v=>Number(v.toFixed(2)).toLocaleString("ru-RU",{maximumFractionDigits:2});
  function draw(active=-1){
    ctx.clearRect(0,0,width,height);
    ctx.font="12px system-ui";ctx.fillStyle=ink;ctx.textBaseline="middle";
    if(!data.length||!series.length||plotWidth<=0||plotHeight<=0){
      ctx.textAlign="center";ctx.fillText(options.empty||"Нет данных за выбранный период",width/2,height/2,Math.max(1,width-24));return;
    }
    ctx.font="600 11px system-ui";ctx.textAlign="left";ctx.fillStyle=primary.length&&dual?primary[0].color:ink;
    ctx.fillText(primary.length?"Температура, °C":"Влажность, %",left,12);
    if(dual){ctx.textAlign="right";ctx.fillStyle=humidity[0].color;ctx.fillText(width<360?"%":"Влажность, %",right,12);}
    ctx.font="11px system-ui";
    for(let i=0;i<=4;i++){
      const yy=bottom-i*plotHeight/4;
      ctx.strokeStyle=grid;ctx.lineWidth=1;ctx.setLineDash(i===0?[]:[3,4]);
      ctx.beginPath();ctx.moveTo(left,yy);ctx.lineTo(right,yy);ctx.stroke();
      ctx.fillStyle=ink;ctx.textAlign="right";ctx.fillText(format(scale.min+i*scale.step),left-8,yy);
      if(dual){ctx.textAlign="left";ctx.fillText(String(i*25)+"%",right+7,yy);}
    }
    ctx.setLineDash([]);
    const labelWidth=Math.max(44,...data.map(p=>ctx.measureText(p.label).width+15));
    ctx.fillStyle=ink;
    for(const i of trendTicks(positions,plotWidth,labelWidth)){
      ctx.textAlign=positions[i]<.01?"left":positions[i]>.99?"right":"center";
      ctx.fillText(data[i].label,x(i),height-13);
    }
    for(const s of series){
      ctx.strokeStyle=s.color;ctx.lineWidth=s.dash?2:2.6;ctx.lineJoin="round";ctx.lineCap="round";
      ctx.setLineDash(s.dash||[]);ctx.beginPath();
      const connected=i=>i>0&&Number.isFinite(s.values[i-1])&&Number.isFinite(s.values[i])&&(!options.contiguous||options.contiguous(i));
      for(let i=0;i<data.length;i++){
        if(!Number.isFinite(s.values[i]))continue;
        if(connected(i))ctx.lineTo(x(i),y(s.values[i],s));else ctx.moveTo(x(i),y(s.values[i],s));
      }
      ctx.stroke();ctx.setLineDash([]);
      for(let i=0;i<data.length;i++){
        if(!Number.isFinite(s.values[i]))continue;
        const isolated=!connected(i)&&!connected(i+1);
        if(!isolated&&active!==i&&data.length>Math.max(24,plotWidth/17))continue;
        ctx.beginPath();ctx.arc(x(i),y(s.values[i],s),active===i?4.5:3,0,Math.PI*2);
        ctx.fillStyle=surface;ctx.fill();ctx.strokeStyle=s.color;ctx.lineWidth=2;ctx.stroke();
      }
    }
    if(active>=0){
      ctx.strokeStyle=ink;ctx.globalAlpha=.5;ctx.lineWidth=1;ctx.setLineDash([3,4]);
      ctx.beginPath();ctx.moveTo(x(active),top);ctx.lineTo(x(active),bottom);ctx.stroke();
      ctx.setLineDash([]);ctx.globalAlpha=1;
    }
  }
  draw();
  if(!series.length||plotWidth<=0||plotHeight<=0){canvas.removeAttribute("tabindex");return;}
  let active=-1;
  function select(i,announce=false){
    if(!data[i])return;
    active=i;draw(i);tooltip.replaceChildren();
    tooltip.append(el("strong","trend-tooltip-date",data[i].title||data[i].label));
    for(const s of series){
      const row=el("div","trend-tooltip-row"),key=el("span","trend-tooltip-key"),dot=el("i","trend-tooltip-dot");
      dot.style.background=s.color;key.append(dot,document.createTextNode(s.name));
      const v=s.values[i];row.append(key,el("b","",Number.isFinite(v)?format(v)+(s.unit==="%"?" %":" °C"):"Нет данных"));
      tooltip.append(row);
    }
    tooltip.hidden=false;
    if(announce)live.textContent=[data[i].title||data[i].label,...series.map(s=>s.name+": "+(Number.isFinite(s.values[i])?format(s.values[i])+" "+s.unit:"нет данных"))].join("; ");
    const rect=canvas.getBoundingClientRect(),parent=frame.getBoundingClientRect();
    const offset=rect.left-parent.left,half=tooltip.offsetWidth/2;
    tooltip.style.left=Math.max(half+6,Math.min(frame.clientWidth-half-6,offset+x(i)))+"px";
    tooltip.style.top=(rect.top-parent.top+4)+"px";
    tooltip.style.transform="translateX(-50%)";
  }
  function clear(){active=-1;tooltip.hidden=true;draw();}
  function pointer(event){
    const rect=canvas.getBoundingClientRect(),px=event.clientX-rect.left;
    if(px<left-10||px>right+10){clear();return;}
    let best=0;
    for(let i=1;i<data.length;i++)if(Math.abs(x(i)-px)<Math.abs(x(best)-px))best=i;
    if(best!==active)select(best);
  }
  canvas.tabIndex=0;
  canvas.onpointermove=pointer;canvas.onpointerdown=pointer;canvas.onpointerleave=clear;canvas.onblur=clear;
  canvas.onkeydown=event=>{
    if(event.key==="Escape"){clear();return;}
    if(!["ArrowLeft","ArrowRight","Home","End"].includes(event.key))return;
    event.preventDefault();
    select(event.key==="Home"?0:event.key==="End"?data.length-1:Math.max(0,Math.min(data.length-1,active<0?0:active+(event.key==="ArrowRight"?1:-1))),true);
  };
}
