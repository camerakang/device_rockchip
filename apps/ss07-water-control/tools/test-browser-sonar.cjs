// Real Chromium against the local API and synthetic archived records only.
const fs=require('node:fs'),os=require('node:os'),path=require('node:path'),crypto=require('node:crypto');
const {spawn}=require('node:child_process');
const [chromium,origin,output,epoch,start,end]=process.argv.slice(2);
const delay=ms=>new Promise(resolve=>setTimeout(resolve,ms));
const profile=fs.mkdtempSync(path.join(os.tmpdir(),'ss07-sonar-browser-'));
const downloads=path.join(output,'downloads');fs.mkdirSync(downloads);
const browser=spawn(chromium,['--no-sandbox','--headless','--disable-gpu','--remote-debugging-port=0','--user-data-dir='+profile,'about:blank'],{stdio:['ignore','ignore','pipe']});
let stderr='',socket,next=0;const pending=new Map(),queries=[],exceptions=[];
browser.stderr.on('data',chunk=>stderr=(stderr+chunk.toString()).slice(-8192));
function send(method,params={}){return new Promise((resolve,reject)=>{
  const id=++next,timer=setTimeout(()=>{pending.delete(id);reject(new Error('CDP timeout '+method));},10000);
  pending.set(id,{resolve:value=>{clearTimeout(timer);resolve(value);},reject:error=>{clearTimeout(timer);reject(error);}});
  socket.send(JSON.stringify({id,method,params}));
});}
async function evaluate(expression){const result=await send('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(result.exceptionDetails)throw new Error(JSON.stringify(result.exceptionDetails));return result.result.value;}
async function until(expression,check){let value;for(let i=0;i<100;i++){value=await evaluate(expression);if(check(value))return value;await delay(100);}throw new Error('Browser condition timed out '+JSON.stringify(value));}
async function main(){
  let port;
  for(let i=0;i<50;i++){
    if(browser.exitCode!==null)throw new Error('Chromium exited '+stderr);
    try{port=fs.readFileSync(path.join(profile,'DevToolsActivePort'),'utf8').split('\n')[0];break;}catch{await delay(100);}
  }
  if(!port)throw new Error('Chromium startup timed out');
  const target=await(await fetch(`http://127.0.0.1:${port}/json/new?about:blank`,{method:'PUT'})).json();
  socket=new WebSocket(target.webSocketDebuggerUrl);await new Promise((resolve,reject)=>{socket.onopen=resolve;socket.onerror=reject;});
  socket.onmessage=event=>{
    const message=JSON.parse(event.data);
    if(message.method==='Network.requestWillBeSent'&&message.params.request.url.includes('/sonars/archives'))queries.push(message.params.request.url);
    if(message.method==='Runtime.exceptionThrown')exceptions.push(message.params.exceptionDetails);
    const waiter=pending.get(message.id);if(!waiter)return;pending.delete(message.id);
    message.error?waiter.reject(new Error(JSON.stringify(message.error))):waiter.resolve(message.result);
  };
  await send('Runtime.enable');await send('Page.enable');await send('Network.enable');
  await send('Emulation.setDeviceMetricsOverride',{width:1440,height:1000,deviceScaleFactor:1,mobile:false});
  await send('Page.setDownloadBehavior',{behavior:'allow',downloadPath:downloads});
  await send('Page.navigate',{url:origin});
  await until('document.querySelectorAll("#sonar-archive-rows tr").length',n=>n>=2);
  await until('[...document.querySelectorAll("#sonars .value")].map(n=>n.textContent)',values=>values.length===5&&values.every(v=>v==='—'));
  await until('document.querySelector("#sonar-archive-search button").disabled',value=>value===false);
  const query={direction:'front',source_epoch:epoch,start_us:start,end_us:end};
  await evaluate(`(()=>{const form=document.getElementById('sonar-archive-search');for(const [key,value] of Object.entries(${JSON.stringify(query)}))form.elements[key].value=value;form.requestSubmit();})()`);
  await until('document.querySelector("#sonar-archive-search button").disabled',value=>value===false);
  if(!queries.some(raw=>{const url=new URL(raw);return Object.entries(query).every(([key,value])=>url.searchParams.get(key)===value);}))throw new Error('u64 query values were rounded');
  const selected=await evaluate('(()=>{const row=document.querySelector("#sonar-archive-rows tr");if(!row?.querySelector("button"))throw new Error("no readable part");row.querySelector("button").click();return row.textContent;})()');
  if(!selected.includes('测试数据'))throw new Error('Missing test evidence label');
  const preview=await until('[...document.querySelectorAll("#sonar-sample-rows tr")].map(r=>r.textContent)',items=>items.length>0);
  if(!preview.every(row=>row.includes('前方')&&row.includes('估计')&&row.includes(epoch)))throw new Error('Filtered preview or exact epoch failed');
  await evaluate('document.querySelector("#sonar-archive-rows a").click()');
  let file;
  for(let i=0;i<100;i++){const found=fs.readdirSync(downloads).find(name=>name.endsWith('.jsonl'));if(found){file=found;break;}await delay(100);}
  if(!file)throw new Error('Archive download did not finish');
  const raw=fs.readFileSync(path.join(downloads,file)),lines=raw.toString().trimEnd().split('\n').map(line=>JSON.parse(line));
  if(lines[0].ss07_sonar_archive!==1||!lines.slice(1).every(sample=>sample.evidence_kind==='synthetic_test'))throw new Error('Invalid downloaded archive');
  const index=await(await fetch(origin+'/api/v1/sonars/archives?source_epoch='+epoch)).json();
  const meta=index.data.items.find(item=>file.includes(item.archive_id));
  const digest=crypto.createHash('sha256').update(raw).digest('hex');
  if(!meta||meta.sha256!==digest||lines.length-1!==meta.event_count)throw new Error('Download digest or event count mismatch');
  await evaluate('document.getElementById("sonar-archive-panel").scrollIntoView()');
  const shot=await send('Page.captureScreenshot',{format:'png'});fs.writeFileSync(path.join(output,'sonar-archives.png'),Buffer.from(shot.data,'base64'));
  if(exceptions.length)throw new Error('Browser script exceptions '+JSON.stringify(exceptions));
  fs.writeFileSync(path.join(output,'browser.json'),JSON.stringify({evidence_kind:'synthetic_test',result:'passed',hardware_verified:false,
    live_sonar_distances:'all_null',query,preview_rows:preview.length,download_events:lines.length-1,download_sha256:digest,browser_exceptions:exceptions},null,2)+'\n');
  console.log('PASS: archive query, exact u64 filters, preview, verified download and unavailable live sonar.');
}
main().catch(error=>{console.error(error);process.exitCode=1;}).finally(async()=>{
  if(socket)socket.close();browser.kill('SIGTERM');
  await Promise.race([new Promise(resolve=>browser.once('exit',resolve)),delay(2000)]);
  if(browser.exitCode===null)browser.kill('SIGKILL');fs.rmSync(profile,{recursive:true,force:true});
});
