"use strict";
const names = {front:"前方",back:"后方",left:"左侧",right:"右侧",down:"下方",sdi:"主摄像机 · SDI"};
const labels = {NOT_IMPLEMENTED:"尚未接入",UNAVAILABLE:"不可用",ABSENT:"设备未连接",OPENING:"正在打开",STREAMING:"采集中",RECOVERING:"正在恢复",CONFIG_ERROR:"配置错误",STALE:"数据已过期",ERROR:"异常",STARTING:"正在启动",RUNNING:"服务运行中",STOPPED:"服务已停止"};
let receivedAt = 0;
const cards = new Map();
let mainChannel = "front";
function text(id, value){document.getElementById(id).textContent=value;}
function element(tag, value, cls){const node=document.createElement(tag);node.textContent=value;if(cls)node.className=cls;return node;}
function kpi(id,value,level){const node=document.getElementById(id);node.textContent=value;node.closest(".kpi").className="kpi "+(level||"");}
function offline(){document.body.className="offline";text("connection","连接中断 · 数据不可用");for(const id of ["kpi-video","kpi-sonar","kpi-timecode","kpi-storage"])kpi(id,"—","bad");for(const node of document.querySelectorAll(".value"))node.textContent="—";for(const card of cards.values()){card.player.setSource(null);card.overlay.hidden=false;card.overlay.textContent="状态已过期";card.native.hidden=true;card.native.textContent="";card.node.classList.remove("live");card.badge.textContent="";}text("timecode","状态已过期");text("storage","状态已过期");}
function makeCard(channel){
  const node=element("article","","camera-card");
  const title=element("button",names[channel]||channel,"camera-title");
  title.type="button";title.title="切换到主画面";
  title.onclick=()=>{mainChannel=channel;setLayout("focus");for(const [id,c] of cards){c.node.classList.toggle("main-camera",id===mainChannel);c.title.setAttribute("aria-pressed",String(id===mainChannel));}};
  const bar=element("div","","camera-bar"),badge=element("span","","camera-badge");bar.append(title,badge);
  const well=element("div","","video-well");
  const video=document.createElement("video");video.muted=true;video.autoplay=true;video.playsInline=true;
  video.onclick=()=>video.play().catch(()=>{});
  const overlay=element("span","尚未接入","video-overlay");
  const info=element("p","尚无视频帧","muted");
  const native=element("p","","muted");native.hidden=true;well.append(video,overlay,info,native);node.append(bar,well);
  node.classList.toggle("main-camera",channel===mainChannel);title.setAttribute("aria-pressed",String(channel===mainChannel));
  const player=new StreamPlayer(video,overlay);
  document.getElementById("streams").append(node);
  const card={node,title,badge,overlay,info,native,player};cards.set(channel,card);return card;
}
function render(data){
  if(!Array.isArray(data.streams)||!Array.isArray(data.sonars))throw new Error("invalid snapshot");
  receivedAt=performance.now();document.body.className="online";text("connection","状态服务已连接");
  let liveCount=0,channelCount=0;
  for(const stream of data.streams){
    if(!Object.hasOwn(names,stream.channel_id))continue;
    const card=cards.get(stream.channel_id)||makeCard(stream.channel_id);
    const live=stream.availability==="available"&&stream.live_url===`/media/${stream.channel_id}/whep`;
    card.player.setSource(live?stream.live_url:null);
    channelCount++;if(live)liveCount++;card.node.classList.toggle("live",live);
    card.native.hidden=!live||typeof stream.native_url!=="string";
    card.native.textContent=card.native.hidden?"":`拉流：${typeof stream.rtsp_url==="string"?stream.rtsp_url+" · ":""}${stream.native_url} · TCP`;
    if(!live){card.overlay.hidden=false;card.overlay.textContent=labels[stream.capture_state]||"暂无视频";}
    const age=stream.last_frame_age_ms;
    card.info.textContent=age===null?"尚无视频帧":`接收帧：${age} ms 前`;
    if(stream.timestamp_basis==="media_ingress_activity")card.info.textContent=`媒体接收：${stream.media_activity_age_ms} ms 前`;
    card.badge.textContent=stream.fps_observed!==null?`${Number(stream.fps_observed).toFixed(1)} fps`:(labels[stream.capture_state]||"");
  }
  const sonars=document.getElementById("sonars");sonars.replaceChildren();
  let sonarValid=0;
  for(const sonar of data.sonars){
    const valid=sonar.valid&&sonar.distance_mm!==null;if(valid)sonarValid++;
    const row=element("div","","sonar-row"+(valid?" valid":""));
    row.append(element("span",names[sonar.direction]||sonar.direction,"dir"),element("span",valid?`${sonar.distance_mm} mm`:"—","value"),element("span",valid?"有效":"无数据","state"));
    sonars.append(row);
  }
  kpi("kpi-video",`${liveCount} / ${channelCount}`,liveCount===channelCount&&channelCount?"ok":liveCount?"warn":"bad");
  kpi("kpi-sonar",`${sonarValid} / ${data.sonars.length}`,sonarValid===data.sonars.length&&sonarValid?"ok":sonarValid?"warn":"bad");
  const tc=data.timecode;
  if(tc.quality==="ESTIMATED")text("timecode",`${tc.source_valid?"已收到时码 · 接收时间估计":"时码已失效 · 短时外推"} · epoch ${tc.source_epoch} · ${tc.age_ms} ms 前；同步误差未校准`);
  else text("timecode",tc.quality==="VALID"?"有效":"暂无可用时码");
  text("storage",data.storage.recording_active?`正在录像 · ${data.storage.channels_recording.length} 路`:data.storage.recording_enabled?"录像已启用，等待媒体流":data.storage.mount_present?"已检测到挂载，录像尚未启用":"录像数据盘未挂载");
  kpi("kpi-timecode",tc.quality==="VALID"?"有效":tc.quality==="ESTIMATED"?(tc.source_valid?"估计":"外推"):"无信号",tc.quality==="VALID"?"ok":tc.quality==="ESTIMATED"?"warn":"bad");
  kpi("kpi-storage",data.storage.recording_active?`录制中 ${data.storage.channels_recording.length} 路`:data.storage.mount_present?"未录制":"未挂载",data.storage.recording_active?"ok":"warn");
  text("device",data.device_id+" · v"+data.app_version);text("updated","更新于 "+new Date(data.observed_at_utc).toLocaleString("zh-CN",{hour12:false}));
}
let events=null;
SS07Session.subscribe(()=>{
  events?.close();events=null;receivedAt=0;offline();
  if(!SS07Session.has("monitor")){text("connection","等待登录");return;}
  events=new EventSource("/api/v1/events");
  events.addEventListener("status",event=>{try{render(JSON.parse(event.data).data);}catch(error){offline();}});
  events.onerror=()=>{offline();SS07Session.refresh();};
});
setInterval(()=>{if(receivedAt&&performance.now()-receivedAt>4000)offline();else for(const card of cards.values())card.player.check();},1000);
window.addEventListener("pagehide",()=>{events?.close();for(const card of cards.values())card.player.close();});
const navButtons=[...document.querySelectorAll(".sidenav [data-view]")];
function showView(id){for(const button of navButtons){const active=button.dataset.view===id;if(active)button.setAttribute("aria-current","page");else button.removeAttribute("aria-current");document.getElementById(button.dataset.view).classList.toggle("is-active",active);}}
function syncNav(){for(const button of navButtons)button.hidden=document.getElementById(button.dataset.view).hidden;const current=navButtons.find(b=>b.hasAttribute("aria-current"));if(!current||current.hidden)showView("overview");}
for(const button of navButtons)button.onclick=()=>showView(button.dataset.view);
SS07Session.subscribe(()=>queueMicrotask(syncNav));
const clock=document.getElementById("clock");
function tick(){clock.textContent=new Date().toLocaleTimeString("zh-CN",{hour12:false});}
tick();setInterval(tick,1000);
const streamGrid=document.getElementById("streams"),layoutButtons=[...document.querySelectorAll("[data-layout]")];
function setLayout(mode){streamGrid.classList.toggle("equal",mode==="equal");for(const button of layoutButtons)button.setAttribute("aria-pressed",String(button.dataset.layout===mode));try{localStorage.setItem("ss07-layout",mode);}catch(error){}}
for(const button of layoutButtons)button.onclick=()=>setLayout(button.dataset.layout);
try{setLayout(localStorage.getItem("ss07-layout")==="equal"?"equal":"focus");}catch(error){setLayout("focus");}
