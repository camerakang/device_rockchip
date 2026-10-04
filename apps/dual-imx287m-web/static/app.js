const $ = id => document.getElementById(id);
let busy = false, initialized = false;
const phases = {running:'正在采集',stopped:'采集已停止',starting:'正在启动',stopping:'正在停止',error:'需要检查设备'};
function notice(message) { $('notice').hidden = !message; $('notice').textContent = message || ''; }
function setBusy(value) { busy=value; $('start').disabled=value; $('stop').disabled=value; $('start').innerHTML=value?'正在应用…':'<span>▶</span> 应用并启动'; }
function configFromForm() { return {mode:$('mode').value,format:$('format').value,fps:Number($('fps').value),exposure_us:Number($('exposure').value),preview_fps:Number($('preview').value),auto_contrast:$('contrast').checked}; }
function formatNote() { const mode=$('mode').value, format=$('format').value; $('format-note').textContent=format==='raw12'?(mode==='hardware'?'RAW12 高速同步档使用 319.4 Hz，接近标称 320 fps。':'RAW12 自由运行可设置 320 fps，实际约 319.4 fps。'):'曝光过长会限制帧率；建议先用较低帧率验证画面。'; }
async function control(path,body) { const response=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}); const result=await response.json(); if(!response.ok)throw Error(result.error||'操作失败'); return result; }
function update(status) {
  $('connection').classList.remove('offline'); $('connection').innerHTML='<i></i>设备已连接';
  $('run-state').textContent=phases[status.phase]||status.phase; $('run-state').className='run-state '+status.phase;
  if(!initialized){const c=status.config;$('mode').value=c.mode;$('format').value=c.format;$('fps').value=c.fps;$('exposure').value=c.exposure_us;$('preview').value=c.preview_fps;$('contrast').checked=c.auto_contrast;$('preview-value').textContent=c.preview_fps+' fps';formatNote();initialized=true;}
  const hardware=status.config.mode==='hardware';
  $('mode-summary').textContent=hardware?'共同硬件触发':'两路自由运行'; $('capture-mode').textContent=hardware?'共同硬件触发':'自由运行';
  $('timing').textContent=hardware?'共用触发 '+status.config.fps+' Hz':'相机连续输出';
  $('config-summary').textContent=status.config.format.toUpperCase()+' · 720 × 544';
  const preview=status.cameras.map(c=>c.preview_fps); $('preview-status').textContent=preview.map(f=>f.toFixed(1)).join(' / ')+' fps';
  $('skew').textContent=status.phase==='running'&&status.paired_eof_us!==null?Math.abs(status.paired_eof_us).toFixed(1)+' µs':'等待配对';
  for(const camera of status.cameras){const i=camera.index,active=status.phase==='running'&&camera.age!==null&&camera.age<2;
    $('fps-'+i).innerHTML=camera.fps.toFixed(1)+'<small> fps</small>';
    $('frames-'+i).textContent=camera.frames.toLocaleString();$('dropped-'+i).textContent=camera.dropped.toLocaleString();$('lost-'+i).textContent=hardware?camera.trigger_lost.toLocaleString():'—';
    $('dropped-'+i).classList.toggle('warning',camera.dropped>0);$('lost-'+i).classList.toggle('warning',camera.trigger_lost>0);
    $('format-'+i).textContent='RAW'+camera.bits+' · 720 × 544'; $('live-'+i).textContent=active?'● LIVE':status.phase==='running'?'等待触发':'已停止';$('live-'+i).classList.toggle('active',active);
    if(camera.frames>0)$('image-'+i).classList.add('loaded');
  }
  $('logs').textContent=status.logs.join('\n');
  if(status.error)notice(status.error);
}
async function refresh(){try{const response=await fetch('/api/status');if(!response.ok)throw Error('状态读取失败');update(await response.json());}catch(error){$('connection').classList.add('offline');$('connection').innerHTML='<i></i>连接已断开';$('run-state').textContent='设备离线';}finally{setTimeout(refresh,1000);}}
$('settings').addEventListener('submit',async event=>{event.preventDefault();if(busy)return;setBusy(true);notice('');try{const result=await control('/api/start',configFromForm());update(result.status);}catch(error){notice(error.message);}finally{setBusy(false);}});
$('stop').addEventListener('click',async()=>{if(busy)return;setBusy(true);notice('');try{const result=await control('/api/stop',{});update(result.status);}catch(error){notice(error.message);}finally{setBusy(false);}});
$('preview').addEventListener('input',()=>{$('preview-value').textContent=$('preview').value+' fps';});
$('format').addEventListener('change',()=>{$('fps').value={raw12:319.4,raw10:300,raw8:30}[$('format').value];formatNote();});
$('mode').addEventListener('change',formatNote);
$('contrast').addEventListener('change',async()=>{try{await control('/api/preview',{auto_contrast:$('contrast').checked});}catch(error){notice(error.message);}});
for(const button of document.querySelectorAll('[data-fullscreen]'))button.addEventListener('click',()=>{const card=$('card-'+button.dataset.fullscreen);if(document.fullscreenElement)document.exitFullscreen();else card.requestFullscreen().catch(error=>notice(error.message));});
for(const i of [0,1])$('image-'+i).addEventListener('error',()=>{setTimeout(()=>{$('image-'+i).src='/stream/'+i+'.mjpg?t='+Date.now();},2000);});
refresh();
