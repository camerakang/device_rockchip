// Node 22+ CDP harness. Inputs and screenshot are explicitly synthetic tests.
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {spawn} = require('node:child_process');
const [chromium, origin, output] = process.argv.slice(2);
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'ss07-browser-'));
const browser = spawn(chromium, ['--no-sandbox', '--headless', '--disable-gpu',
  '--remote-debugging-port=0', '--autoplay-policy=no-user-gesture-required',
  '--user-data-dir=' + profile, 'about:blank'], {stdio: ['ignore', 'ignore', 'pipe']});
let stderr = '';
browser.stderr.on('data', chunk => { stderr = (stderr + chunk.toString()).slice(-8192); });
let socket;
let next = 0;
const pending = new Map();
function send(method, params = {}) {
  return new Promise((resolve, reject) => {
    const id = ++next;
    const timer = setTimeout(() => {pending.delete(id); reject(new Error('CDP timeout: ' + method));}, 10000);
    pending.set(id, {resolve: value => {clearTimeout(timer); resolve(value);}, reject});
    socket.send(JSON.stringify({id, method, params}));
  });
}
async function evaluate(expression) {
  const result = await send('Runtime.evaluate', {expression, returnByValue: true, awaitPromise: true});
  if (result.exceptionDetails) throw new Error(JSON.stringify(result.exceptionDetails));
  return result.result.value;
}
async function main() {
  let debugPort;
  for (let i = 0; i < 50; i++) {
    if (browser.exitCode !== null) throw new Error('Chromium exited: ' + stderr);
    try { debugPort = fs.readFileSync(path.join(profile, 'DevToolsActivePort'), 'utf8').split('\n')[0]; break; }
    catch { await delay(100); }
  }
  if (!debugPort) throw new Error('Chromium startup timeout: ' + stderr);
  const target = await (await fetch(`http://127.0.0.1:${debugPort}/json/new?${origin}/`, {method:'PUT'})).json();
  socket = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {socket.onopen = resolve; socket.onerror = reject;});
  socket.onmessage = event => {
    const msg = JSON.parse(event.data);
    if (!pending.has(msg.id)) return;
    const waiter = pending.get(msg.id); pending.delete(msg.id);
    msg.error ? waiter.reject(new Error(JSON.stringify(msg.error))) : waiter.resolve(msg.result);
  };
  await send('Page.enable');
  await send('Emulation.setDeviceMetricsOverride', {width:1440,height:1000,deviceScaleFactor:1,mobile:false});
  let dashboard;
  for (let i = 0; i < 50; i++) {
    dashboard = await evaluate('({ready:document.querySelectorAll(".camera-card").length===6, values:[...document.querySelectorAll(".value")].map(n=>n.textContent)})');
    if (dashboard.ready) break;
    await delay(100);
  }
  if (!dashboard.ready || dashboard.values.length !== 5 || !dashboard.values.every(v => v === '—')) throw new Error('Dashboard missing unavailable readings');
  const hardware = await evaluate('fetch("/api/v1/status").then(r=>r.json()).then(r=>r.data.streams.every(s=>s.availability==="unavailable"))');
  if (!hardware) throw new Error('Test fixture incorrectly presented as real hardware');
  await evaluate(`events.close(); receivedAt=0;
    document.querySelector('.notice').textContent='SYNTHETIC TEST — six native H.264 streams; not hardware acceptance';
    for(const [channel,card] of cards){card.player.setSource('/media/'+channel+'/whep');}
    window.testFrameCounts=()=>[...cards].map(([channel,card])=>({channel,frames:card.player.video.getVideoPlaybackQuality().totalVideoFrames,width:card.player.video.videoWidth,message:card.overlay.textContent,hidden:card.overlay.hidden}));`);
  let decoded;
  for (let i = 0; i < 80; i++) {
    decoded = await evaluate('window.testFrameCounts()');
    if (decoded.every(v => v.frames >= 25 && v.width === 640 && v.hidden)) break;
    await delay(250);
  }
  if (!decoded.every(v => v.frames >= 25 && v.width === 640 && v.hidden)) throw new Error('Six decoders failed: '+JSON.stringify(decoded));
  await delay(1200);
  const later = await evaluate('window.testFrameCounts()');
  if (!later.every((v,i) => v.frames > decoded[i].frames + 10)) throw new Error('Decoded frames not advancing');
  await evaluate('cards.get("sdi").title.click()');
  if (!await evaluate('cards.get("sdi").node.classList.contains("main-camera")')) throw new Error('Main camera switch failed');
  const shot = await send('Page.captureScreenshot', {format:'png'});
  fs.writeFileSync(path.join(output,'six-streams.png'), Buffer.from(shot.data,'base64'));
  await evaluate('offline()');
  if (!await evaluate('[...cards.values()].every(c=>c.overlay.hidden===false && c.player.pc===null && c.player.video.srcObject===null)')) throw new Error('Offline overlay/session cleanup failed');
  fs.writeFileSync(path.join(output,'browser.json'),JSON.stringify({evidence_kind:'synthetic_test',hardware_verified:false,
    result:'passed',decoded:later,main_switch:'passed',offline_cleanup:'passed'},null,2)+'\n');
  console.log('PASS: six WebRTC decoders, advancing frames, main switch and offline cleanup; synthetic inputs only.');
}
main().catch(error => {console.error(error);process.exitCode=1;}).finally(async () => {
  if (socket) socket.close();
  browser.kill('SIGTERM');
  await Promise.race([new Promise(resolve=>browser.once('exit',resolve)),delay(2000)]);
  if (browser.exitCode===null) browser.kill('SIGKILL');
  fs.rmSync(profile,{recursive:true,force:true});
});
