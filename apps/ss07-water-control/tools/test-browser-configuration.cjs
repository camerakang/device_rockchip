// Real HTTPS UI and WebRTC with synthetic sources; the parent observes recorder continuity.
const fs=require('node:fs'),os=require('node:os'),path=require('node:path');
const {spawn}=require('node:child_process');
const [chromium,origin,output,exchange]=process.argv.slice(2);
const delay=ms=>new Promise(resolve=>setTimeout(resolve,ms));
const profile=fs.mkdtempSync(path.join(os.tmpdir(),'ss07-configuration-browser-'));
const browser=spawn(chromium,['--no-sandbox','--headless','--disable-gpu','--remote-debugging-port=0',
  '--ignore-certificate-errors-spki-list='+process.env.SS07_TEST_SPKI,
  '--autoplay-policy=no-user-gesture-required','--user-data-dir='+profile,'about:blank'],{stdio:['ignore','ignore','pipe']});
let stderr='',socket,next=0;const pending=new Map(),exceptions=[];
browser.stderr.on('data',chunk=>stderr=(stderr+chunk.toString()).slice(-8192));
function send(method,params={}){
  return new Promise((resolve,reject)=>{
    const id=++next,timer=setTimeout(()=>{pending.delete(id);reject(new Error('CDP timeout: '+method));},10000);
    pending.set(id,{resolve:value=>{clearTimeout(timer);resolve(value);},reject:error=>{clearTimeout(timer);reject(error);}});
    socket.send(JSON.stringify({id,method,params}));
  });
}
async function evaluate(expression){
  const result=await send('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});
  if(result.exceptionDetails)throw new Error(JSON.stringify(result.exceptionDetails));
  return result.result.value;
}
async function until(expression,check,attempts=150){
  let value;for(let i=0;i<attempts;i++){value=await evaluate(expression);if(check(value))return value;await delay(100);}
  throw new Error('Browser condition timed out: '+expression+' => '+JSON.stringify(value));
}
async function login(){
  await until('document.getElementById("login-panel")?.hidden===false',Boolean);
  await evaluate(`(()=>{const f=document.getElementById('login-form');f.elements.username.value='admin';
    f.elements.password.value=${JSON.stringify(process.env.SS07_TEST_PASSWORD)};f.requestSubmit();})()`);
  await until('document.getElementById("workspace").hidden',value=>value===false);
  await until('document.getElementById("config-save").disabled',value=>value===false);
}
async function config(){return evaluate('fetch("/api/v1/config").then(r=>r.json()).then(r=>r.data)');}
async function terminalConfig(phase){
  // The private durable journal becomes terminal before the next 1 Hz public
  // heartbeat. Observe the API's eventual result, not the parent's file timing.
  return until('fetch("/api/v1/config").then(r=>r.json()).then(r=>r.data)',
    value=>value?.application?.status?.transaction?.phase===phase&&!value.application.status.busy);
}
async function sixVideos(){
  await until('document.querySelectorAll(".camera-card").length',value=>value===6);
  await evaluate(`events.close();receivedAt=0;
    document.querySelector('.notice').textContent='SYNTHETIC TEST — configuration application and recovery; not hardware acceptance';
    for(const [channel,card] of cards)card.player.setSource('/media/'+channel+'/whep');
    window.testFrames=()=>[...cards].map(([channel,card])=>({channel,frames:card.player.video.getVideoPlaybackQuality().totalVideoFrames,
      width:card.player.video.videoWidth,hidden:card.overlay.hidden}));`);
  const decoded=await until('window.testFrames()',rows=>rows.every(r=>r.frames>=20&&r.width===640&&r.hidden),180);
  await delay(1000);const later=await evaluate('window.testFrames()');
  if(!later.every((row,i)=>row.frames>decoded[i].frames+10))throw new Error('WebRTC frames stopped');
  return later;
}
async function saveAndPlan(change,services){
  await until('document.getElementById("config-save").disabled',value=>value===false);
  await evaluate(`(()=>{const e=document.getElementById('config-editor'),v=JSON.parse(e.value);${change}
    e.value=JSON.stringify(v,null,2);e.dispatchEvent(new Event('input',{bubbles:true}));
    document.getElementById('config-form').requestSubmit();})()`);
  await until('document.getElementById("config-message").textContent',value=>value.includes('配置已保存'));
  await until('document.getElementById("config-plan").disabled',value=>value===false);
  await evaluate('document.getElementById("config-plan").click()');
  await until('document.getElementById("config-apply").disabled',value=>value===false);
  const text=await evaluate('document.getElementById("config-plan-message").textContent');
  if(!services.every(name=>text.includes(name))||!text.includes('录像进程继续运行')||!text.includes('需要重新登录'))
    throw new Error('Incorrect application impact: '+text);
  const current=await config();
  if(!current.pending_apply||current.revision===current.active_revision)throw new Error('Saved edit was treated as applied');
  return current;
}
async function applyAndObserve(phase,revision){
  fs.writeFileSync(path.join(exchange,phase+'.request'),JSON.stringify({revision}));
  await evaluate('document.getElementById("config-apply").click()');
  for(let i=0;i<650;i++){
    if(fs.existsSync(path.join(exchange,phase+'.response'))){
      if(!JSON.parse(fs.readFileSync(path.join(exchange,phase+'.response'),'utf8')).verified)throw new Error('Parent verification failed');
      await send('Page.navigate',{url:origin+'/'});await login();return;
    }
    await delay(100);
  }
  throw new Error('Application or recorder continuity not confirmed: '+phase);
}
async function main(){
  let port;
  for(let i=0;i<50;i++){
    if(browser.exitCode!==null)throw new Error('Chromium exited: '+stderr);
    try{port=fs.readFileSync(path.join(profile,'DevToolsActivePort'),'utf8').split('\n')[0];break;}catch{await delay(100);}
  }
  if(!port)throw new Error('Chromium startup timeout');
  const target=await(await fetch(`http://127.0.0.1:${port}/json/new?${origin}/`,{method:'PUT'})).json();
  socket=new WebSocket(target.webSocketDebuggerUrl);await new Promise((resolve,reject)=>{socket.onopen=resolve;socket.onerror=reject;});
  socket.onmessage=event=>{
    const msg=JSON.parse(event.data);
    if(msg.method==='Runtime.exceptionThrown')exceptions.push(msg.params.exceptionDetails.text);
    if(!pending.has(msg.id))return;const item=pending.get(msg.id);pending.delete(msg.id);
    msg.error?item.reject(new Error(JSON.stringify(msg.error))):item.resolve(msg.result);
  };
  await send('Page.enable');await send('Network.enable');await send('Runtime.enable');
  await send('Emulation.setDeviceMetricsOverride',{width:1440,height:1100,deviceScaleFactor:1,mobile:false});
  await login();
  const initial=await config(),before=await sixVideos();
  const first=await saveAndPlan("v.device_id='ss07-apply-integration';",['管理接口']);
  await applyAndObserve('apply',first.revision);
  const applied=await terminalConfig('APPLIED');
  if(applied.pending_apply||applied.active_revision!==first.revision||applied.application.status.transaction.phase!=='APPLIED')
    throw new Error('Successful application not confirmed: '+JSON.stringify(applied));
  if(applied.application.status.transaction.services.join(',')!=='api')throw new Error('Unnecessary services restarted');
  await until('document.getElementById("config-application-status").textContent',v=>v.includes('配置已生效')&&v.includes('已加载（设备未配置）'));
  const afterApply=await sixVideos();
  const settings=JSON.parse(fs.readFileSync(path.join(exchange,'settings.json'),'utf8'));
  const second=await saveAndPlan('v.api.port='+settings.occupied_port+';',['管理接口','网页入口']);
  await applyAndObserve('rollback',second.revision);
  const rolledBack=await terminalConfig('ROLLED_BACK'),transaction=rolledBack.application.status.transaction;
  if(!rolledBack.pending_apply||rolledBack.active_revision!==first.revision||rolledBack.revision!==second.revision
      ||rolledBack.runtime.api.port!==initial.runtime.api.port||rolledBack.persisted.api.port!==settings.occupied_port
      ||transaction.phase!=='ROLLED_BACK'||!transaction.configuration_restored)
    throw new Error('Failure did not restore confirmed config and retain desired edit: '+JSON.stringify(rolledBack));
  await until('document.getElementById("config-application-status").textContent',v=>v.includes('应用失败，已恢复原配置'));
  const afterRollback=await sixVideos();
  await evaluate('document.getElementById("log-source").value="application";document.getElementById("log-read").click()');
  await until('document.getElementById("log-rows").textContent',v=>v.includes('CONFIG_APPLY')&&v.includes('CONFIG_ROLLBACK'));
  const exported=await evaluate('fetch(document.getElementById("log-export").href).then(r=>({status:r.status,disposition:r.headers.get("Content-Disposition")}))');
  if(exported.status!==200||!exported.disposition?.includes('attachment'))throw new Error('Application audit export failed');
  if(exceptions.length)throw new Error('Browser script exceptions: '+JSON.stringify(exceptions));
  fs.writeFileSync(path.join(output,'browser-configuration.json'),JSON.stringify({result:'passed',evidence_kind:'synthetic_test',
    hardware_verified:false,ui_save_preview_apply:true,session_restart_requires_login:true,successful_apply:true,
    occupied_listener_automatic_rollback:true,desired_edit_retained:true,unconfigured_device_label:true,application_audit_export:true,
    decoded:{before,afterApply,afterRollback},javascript_exceptions:exceptions},null,2)+'\n');
  console.log('PASS: HTTPS configuration UI, selective application, bind failure recovery, relogin and six WebRTC decoders.');
}
main().catch(error=>{console.error(error);process.exitCode=1;}).finally(async()=>{
  if(socket)socket.close();browser.kill('SIGTERM');
  await Promise.race([new Promise(resolve=>browser.once('exit',resolve)),delay(2000)]);
  if(browser.exitCode===null)browser.kill('SIGKILL');fs.rmSync(profile,{recursive:true,force:true});
});
