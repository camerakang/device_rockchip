// Real Chromium playback/seek of fMP4 recordings; inputs are explicitly synthetic.
const fs=require('node:fs'),os=require('node:os'),path=require('node:path');
const {spawn}=require('node:child_process');
const [chromium,origin,output,mode]=process.argv.slice(2);
const delay=ms=>new Promise(resolve=>setTimeout(resolve,ms));
const profile=fs.mkdtempSync(path.join(os.tmpdir(),'ss07-playback-'));
const browser=spawn(chromium,['--no-sandbox','--headless','--disable-gpu','--remote-debugging-port=0',
  '--autoplay-policy=no-user-gesture-required','--user-data-dir='+profile,'about:blank'],{stdio:['ignore','ignore','pipe']});
let stderr='',socket,next=0;
const pending=new Map(),responses=[],queries=[];
browser.stderr.on('data',chunk=>{stderr=(stderr+chunk.toString()).slice(-8192);});
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
async function until(expression,check,attempts=80){
  let result;
  for(let i=0;i<attempts;i++){result=await evaluate(expression);if(check(result))return result;await delay(100);}
  throw new Error('Browser condition timed out: '+JSON.stringify(result));
}
async function main(){
  let port;
  for(let i=0;i<50;i++){
    if(browser.exitCode!==null)throw new Error('Chromium exited: '+stderr);
    try{port=fs.readFileSync(path.join(profile,'DevToolsActivePort'),'utf8').split('\n')[0];break;}catch{await delay(100);}
  }
  if(!port)throw new Error('Chromium startup timeout: '+stderr);
  const target=await(await fetch(`http://127.0.0.1:${port}/json/new?${origin}/`,{method:'PUT'})).json();
  socket=new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve,reject)=>{socket.onopen=resolve;socket.onerror=reject;});
  socket.onmessage=event=>{
    const msg=JSON.parse(event.data);
    if(msg.method==='Network.responseReceived'&&msg.params.response.url.includes('/content'))responses.push({status:msg.params.response.status,mimeType:msg.params.response.mimeType});
    if(msg.method==='Network.requestWillBeSent'&&msg.params.request.url.includes('/api/v1/recordings?'))queries.push(msg.params.request.url);
    if(!pending.has(msg.id))return;
    const item=pending.get(msg.id);pending.delete(msg.id);
    msg.error?item.reject(new Error(JSON.stringify(msg.error))):item.resolve(msg.result);
  };
  await send('Page.enable');await send('Network.enable');
  await send('Emulation.setDeviceMetricsOverride',{width:1440,height:1000,deviceScaleFactor:1,mobile:false});
  await until('document.querySelectorAll("#recording-rows tr").length',value=>value>0);
  if(!await evaluate('fetch("/api/v1/status").then(r=>r.json()).then(r=>r.data.streams.every(s=>s.availability==="unavailable"))'))throw new Error('Synthetic fixture reported hardware availability');
  await evaluate('document.querySelector("#recording-search select").value="front";document.querySelector("#recording-search").requestSubmit()');
  await until('[...document.querySelectorAll("#recording-rows tr")].map(row=>row.cells[0].textContent)',value=>value.length>0&&value.every(v=>v==='前方'));
  let timecodeFilter=null;
  if(mode==='timecode'){
    const body=await(await fetch(origin+'/api/v1/recordings?channel=front')).json();
    const item=body.data.items.find(item=>item.complete&&item.duration_ms>=1500&&item.time_tags?.end?.quality==='ESTIMATED');
    if(!item)throw new Error('No timecode-tagged segment');
    const tag=item.time_tags.end;
    timecodeFilter={epoch:tag.source_epoch,time_start_us:(BigInt(tag.estimated_time_us)-1000000n).toString(),
      time_end_us:(BigInt(tag.estimated_time_us)+1000000n).toString()};
    await until('document.querySelector("#recording-search button").disabled',value=>value===false);
    await evaluate(`(()=>{const form=document.getElementById('recording-search');
      for(const [key,value] of Object.entries(${JSON.stringify(timecodeFilter)}))form.elements[key].value=value;form.requestSubmit();})()`);
    await until('document.querySelector("#recording-search button").disabled',value=>value===false);
    if(!queries.some(raw=>{const url=new URL(raw);return Object.entries(timecodeFilter).every(([key,value])=>url.searchParams.get(key)===value);}))
      throw new Error('Browser rounded u64 timecode query values');
    await until('[...document.querySelectorAll("#recording-rows tr")].map(row=>row.textContent)',value=>value.length>0&&value.every(v=>v.includes('时码')));
  }
  const selected=await evaluate(`(()=>{const row=[...document.querySelectorAll('#recording-rows tr')].find(r=>parseFloat(r.cells[2].textContent)>=1.5&&r.querySelector('button'));
    if(!row)throw new Error('No playable completed segment');row.querySelector('button').click();return row.textContent;})()`);
  if(!selected.includes('测试源'))throw new Error('Missing synthetic evidence label');
  const expression=`(()=>{const v=document.getElementById('recording-video');return {width:v.videoWidth,height:v.videoHeight,frames:v.getVideoPlaybackQuality().totalVideoFrames,current:v.currentTime,duration:v.duration,error:v.error?.code??null};})()`;
  const decoded=await until(expression,value=>value.width===640&&value.frames>=5);
  if(decoded.error||decoded.duration<1.5)throw new Error('MP4 decode failed: '+JSON.stringify(decoded));
  await evaluate(`(()=>{const v=document.getElementById('recording-video');v.pause();window.testSeeked=false;
    v.addEventListener('seeked',()=>window.testSeeked=true,{once:true});v.currentTime=Math.min(1,v.duration/2);})()`);
  await until('window.testSeeked',value=>value===true);
  const seek=await evaluate(expression);
  if(seek.current<0.5||seek.error)throw new Error('Playback seek failed');
  if(!responses.some(r=>r.status===206&&r.mimeType==='video/mp4'))throw new Error('Browser did not receive MP4 range response');
  await evaluate('document.getElementById("recording-playback").scrollIntoView({block:"end"})');
  const shot=await send('Page.captureScreenshot',{format:'png'});
  fs.writeFileSync(path.join(output,'recording-playback.png'),Buffer.from(shot.data,'base64'));
  fs.writeFileSync(path.join(output,'browser-recording.json'),JSON.stringify({evidence_kind:'synthetic_test',hardware_verified:false,
    result:'passed',channel_filter:'passed',timecode_filter:timecodeFilter,decoded,seek,range_responses:responses},null,2)+'\n');
  console.log('PASS: real MP4 browser decode, seek, Range and channel filter; synthetic inputs only.');
}
main().catch(error=>{console.error(error);process.exitCode=1;}).finally(async()=>{
  if(socket)socket.close();browser.kill('SIGTERM');
  await Promise.race([new Promise(resolve=>browser.once('exit',resolve)),delay(2000)]);
  if(browser.exitCode===null)browser.kill('SIGKILL');fs.rmSync(profile,{recursive:true,force:true});
});
