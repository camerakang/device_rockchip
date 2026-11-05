// Real HTTPS Chromium integration; ephemeral credentials and synthetic sources only.
const fs=require('node:fs'),os=require('node:os'),path=require('node:path');
const {spawn}=require('node:child_process');
const [chromium,origin,output,exchange]=process.argv.slice(2);
const delay=ms=>new Promise(resolve=>setTimeout(resolve,ms));
const profile=fs.mkdtempSync(path.join(os.tmpdir(),'ss07-auth-browser-'));
const browser=spawn(chromium,['--no-sandbox','--headless','--disable-gpu','--remote-debugging-port=0',
  '--ignore-certificate-errors-spki-list='+process.env.SS07_TEST_SPKI,
  '--autoplay-policy=no-user-gesture-required','--user-data-dir='+profile,'about:blank'],{stdio:['ignore','ignore','pipe']});
let stderr='',socket,next=0;const pending=new Map(),responses=[],exceptions=[];
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
async function until(expression,check,attempts=100){
  let value;for(let i=0;i<attempts;i++){value=await evaluate(expression);if(check(value))return value;await delay(100);}
  throw new Error('Browser condition timed out: '+JSON.stringify(value));
}
async function login(name){
  await until('document.getElementById("login-panel")?.hidden===false',Boolean);
  await evaluate(`(()=>{const f=document.getElementById('login-form');f.elements.username.value=${JSON.stringify(name)};
    f.elements.password.value=${JSON.stringify(process.env.SS07_TEST_PASSWORD)};f.requestSubmit();})()`);
  await until('document.getElementById("workspace").hidden',value=>value===false);
}
async function sixVideos(){
  await until('document.querySelectorAll(".camera-card").length',value=>value===6);
  await evaluate(`events.close();receivedAt=0;document.querySelector('.notice').textContent='SYNTHETIC TEST — HTTPS roles and six H.264 streams; not hardware acceptance';
    for(const [channel,card] of cards)card.player.setSource('/media/'+channel+'/whep');
    window.testFrames=()=>[...cards].map(([channel,card])=>({channel,frames:card.player.video.getVideoPlaybackQuality().totalVideoFrames,width:card.player.video.videoWidth,hidden:card.overlay.hidden}));`);
  const decoded=await until('window.testFrames()',rows=>rows.every(r=>r.frames>=20&&r.width===640&&r.hidden),160);
  await delay(1000);const later=await evaluate('window.testFrames()');
  if(!later.every((row,i)=>row.frames>decoded[i].frames+10))throw new Error('WebRTC frames stopped');
  return later;
}
async function exchangePhase(phase){
  fs.writeFileSync(path.join(exchange,phase+'.request'),'ready');
  for(let i=0;i<150;i++){if(fs.existsSync(path.join(exchange,phase+'.response')))return;await delay(100);}
  throw new Error('Native revocation not confirmed: '+phase);
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
    if(msg.method==='Network.responseReceived'&&msg.params.response.url.includes('/content'))responses.push({status:msg.params.response.status,mimeType:msg.params.response.mimeType});
    if(msg.method==='Runtime.exceptionThrown')exceptions.push(msg.params.exceptionDetails.text);
    if(!pending.has(msg.id))return;const item=pending.get(msg.id);pending.delete(msg.id);
    msg.error?item.reject(new Error(JSON.stringify(msg.error))):item.resolve(msg.result);
  };
  await send('Page.enable');await send('Network.enable');await send('Runtime.enable');
  await send('Emulation.setDeviceMetricsOverride',{width:1440,height:1100,deviceScaleFactor:1,mobile:false});
  await login('viewer');
  const forbidden=await evaluate(`Promise.all(['/api/v1/config','/api/v1/recordings'].map(u=>fetch(u).then(r=>r.status)))`);
  if(!forbidden.every(s=>s===403))throw new Error('Viewer role escaped permissions');
  if(!await evaluate('document.getElementById("recording-panel").hidden&&document.getElementById("maintenance-panel").hidden'))throw new Error('Viewer UI permissions');
  const cookies=(await send('Network.getCookies',{urls:[origin]})).cookies;
  const cookie=cookies.find(c=>c.name==='__Host-SS07');
  if(!cookie||!cookie.secure||!cookie.httpOnly||cookie.sameSite!=='Strict')throw new Error('Session cookie protections missing');
  if(await evaluate('document.cookie.includes("SS07")'))throw new Error('Cookie readable by script');
  if(!await evaluate('fetch("/api/v1/status").then(r=>r.json()).then(r=>r.data.streams.every(s=>s.availability==="unavailable"))'))throw new Error('Synthetic fixture claimed hardware availability');
  const decoded=await sixVideos();
  const shot=await send('Page.captureScreenshot',{format:'png'});fs.writeFileSync(path.join(output,'https-six-streams.png'),Buffer.from(shot.data,'base64'));
  // Raw logout deliberately bypasses UI cleanup: the server must revoke native media.
  const logout=await evaluate(`fetch('/api/v1/session').then(r=>r.json()).then(s=>fetch('/api/v1/logout',{method:'POST',
    headers:{'Content-Type':'application/json','X-CSRF-Token':s.data.csrf_token},body:'{}'})).then(r=>r.status)`);
  if(logout!==200)throw new Error('Logout rejected');
  await exchangePhase('logout');await evaluate('SS07Session.refresh()');
  await login('reader');
  await until('document.querySelectorAll("#recording-rows tr").length',value=>value>0);
  if(!await evaluate('document.getElementById("maintenance-panel").hidden'))throw new Error('Reader can maintain');
  const code=await evaluate('SS07Session.request("/api/v1/config",{method:"PUT",headers:{"Content-Type":"application/json"},body:"{}"}).then(r=>r.status)');
  if(code!==403)throw new Error('Reader config write not forbidden');
  await evaluate(`document.querySelector('#recording-rows button').click()`);
  const playback=await until(`(()=>{const v=document.getElementById('recording-video');return {frames:v.getVideoPlaybackQuality().totalVideoFrames,width:v.videoWidth,duration:v.duration};})()`,v=>v.frames>=5&&v.width===640);
  await evaluate(`(()=>{const v=document.getElementById('recording-video');window.seeked=false;v.pause();v.addEventListener('seeked',()=>window.seeked=true,{once:true});v.currentTime=v.duration/2;})()`);
  await until('window.seeked',Boolean);
  const exportResult=await evaluate(`fetch(document.querySelector('#recording-rows a').href).then(async r=>({status:r.status,disposition:r.headers.get('Content-Disposition'),bytes:(await r.arrayBuffer()).byteLength}))`);
  if(exportResult.status!==200||!exportResult.disposition?.includes('attachment')||exportResult.bytes<1000)throw new Error('Recording export failed');
  await evaluate('document.getElementById("logout").click()');await login('admin');
  await until('document.getElementById("config-save").disabled',v=>v===false);
  await evaluate(`(()=>{const e=document.getElementById('config-editor'),v=JSON.parse(e.value);v.device_id='ss07-auth-integration';e.value=JSON.stringify(v);document.getElementById('config-form').requestSubmit();})()`);
  await until('document.getElementById("config-message").textContent',v=>v.includes('配置已保存'));
  if(!await evaluate('fetch("/api/v1/config").then(r=>r.json()).then(r=>r.data.pending_restart&&r.data.persisted.device_id==="ss07-auth-integration")'))throw new Error('Config saved/applied distinction');
  await until('[...document.querySelectorAll("#recording-rows button")].some(b=>b.textContent==="锁定")',Boolean);
  await evaluate(`[...document.querySelectorAll('#recording-rows button')].find(b=>b.textContent==='锁定').click()`);
  await until('[...document.querySelectorAll("#recording-rows tr")].some(r=>r.textContent.includes("已锁定"))',Boolean);
  await evaluate('document.getElementById("log-source").value="maintenance";document.getElementById("log-read").click()');
  await until('document.getElementById("log-rows").textContent',v=>v.includes('CONFIG_UPDATE')&&v.includes('RECORD_LOCK'));
  const logs=await evaluate('fetch(document.getElementById("log-export").href).then(r=>({status:r.status,disposition:r.headers.get("Content-Disposition")}))');
  if(logs.status!==200||!logs.disposition?.includes('attachment'))throw new Error('Log export failed');
  await evaluate('document.getElementById("maintenance-panel").scrollIntoView()');
  const maintenance=await send('Page.captureScreenshot',{format:'png'});fs.writeFileSync(path.join(output,'maintenance.png'),Buffer.from(maintenance.data,'base64'));
  await sixVideos();
  // Stop client reconnects without closing the live sessions. Exercise native kick used by systemd's stop hook.
  await evaluate('for(const card of cards.values()){card.player.check=()=>{};card.player.pc.onconnectionstatechange=null;}');
  await exchangePhase('orphan');
  if(!responses.some(r=>r.status===206&&r.mimeType==='video/mp4'))throw new Error('No real MP4 Range response');
  if(exceptions.length)throw new Error('Browser script exceptions: '+JSON.stringify(exceptions));
  fs.writeFileSync(path.join(output,'browser-auth.json'),JSON.stringify({result:'passed',evidence_kind:'synthetic_test',hardware_verified:false,
    roles:'monitor/reader/maintainer enforced',cookie:{secure:true,httpOnly:true,sameSite:'Strict'},
    decoded,playback,seek:'passed',export:exportResult,range_responses:responses,config_pending_restart:true,record_lock:true,logs_export:true,
    javascript_exceptions:exceptions},null,2)+'\n');
  console.log('PASS: HTTPS login/roles, six WebRTC decoders, revocation, MP4 decode/seek/export, config CAS, lock and audit UI.');
}
main().catch(error=>{console.error(error);process.exitCode=1;}).finally(async()=>{
  if(socket)socket.close();browser.kill('SIGTERM');
  await Promise.race([new Promise(resolve=>browser.once('exit',resolve)),delay(2000)]);
  if(browser.exitCode===null)browser.kill('SIGKILL');fs.rmSync(profile,{recursive:true,force:true});
});
