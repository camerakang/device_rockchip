"use strict";
(() => {
  const form=document.getElementById("recording-search"), rows=document.getElementById("recording-rows");
  const message=document.getElementById("recording-message"), next=document.getElementById("recording-next");
  const video=document.getElementById("recording-video"), playback=document.getElementById("recording-playback");
  const channels={front:"前方",back:"后方",left:"左侧",right:"右侧",down:"下方",sdi:"主摄像机 · SDI"};
  const states={COMPLETE:"已完成",INCOMPLETE:"未完成 · 中断片段",INVALID:"文件或元数据异常",MISSING:"文件缺失",DELETING:"正在清理"};
  let cursor=null, query=new URLSearchParams(), busy=false, generation=0;
  function node(tag,text){const el=document.createElement(tag);el.textContent=text;return el;}
  function date(value){return value?new Date(value).toLocaleString():"—";}
  function cell(row,value){row.append(node("td",value));}
  function show(item){
    const row=document.createElement("tr"), label=channels[item.channel_id]||item.channel_id;
    cell(row,label);cell(row,date(item.start_utc));
    cell(row,`${item.duration_ms===null?"—":(item.duration_ms/1000).toFixed(1)+" 秒"} / ${(Number(item.size_bytes)/1048576).toFixed(2)} MiB`);
    const available=item.complete && item.availability==="available";
    let status=states[item.state]||"状态未知";
    if(item.complete && !available)status+=item.availability==="disk_offline"?" · 数据盘离线":" · 文件不可读";
    if(item.locked)status+=" · 已锁定";
    if(item.evidence_kind==="synthetic_test")status+=" · 测试源";
    const associations=item.timecode_association||['unavailable'];
    if(associations.includes('continuous_mapping_estimate'))status+=" · 时码区间估计";
    else if(associations.includes('holdover_endpoint'))status+=" · 含失时外推标签";
    else if(associations.includes('endpoint_estimate'))status+=" · 仅片段端点时码估计";
    else status+=" · 未关联时码";
    cell(row,status);
    const actions=document.createElement("td");
    if(available && /^[a-f0-9-]{36}$/.test(item.recording_id)){
      const url=`/api/v1/recordings/${item.recording_id}/content`;
      const play=node("button","回放");play.type="button";
      play.onclick=()=>{video.pause();video.src=url;playback.hidden=false;
        document.getElementById("recording-selected").textContent=`${label} · ${date(item.start_utc)}${item.evidence_kind==="synthetic_test"?" · 测试源":""}`;
        video.play().catch(()=>{message.textContent="点击播放器继续播放；若无法播放，请导出文件检查编码兼容性。";});};
      const download=node("a","导出");download.href=url+"?download=1";download.setAttribute("download","");
      actions.append(play,download);
    }else actions.textContent="—";
    if(SS07Session.has("maintain")&&/^[a-f0-9-]{36}$/.test(item.recording_id)){
      const lock=node("button",item.locked?"解锁":"锁定");lock.type="button";
      lock.onclick=async()=>{
        lock.disabled=true;
        try{
          const response=await SS07Session.request(`/api/v1/recordings/${item.recording_id}/lock`,{
            method:"POST",headers:{"Content-Type":"application/json"},
            body:JSON.stringify({locked:!item.locked,expected_revision:item.lock_revision})});
          if(!response.ok){message.textContent=response.status===409?"录像状态已变化或数据盘不可用，请重新查询后核对。":"锁定结果未确认，请重新查询核对，不要重复提交。";return;}
          await fetchPage();
        }catch(error){message.textContent="连接中断，锁定结果未确认，请重新查询核对。";}
        finally{lock.disabled=false;}
      };actions.append(lock);
    }
    row.append(actions);rows.append(row);
  }
  async function fetchPage(nextPage=false){
    if(busy||!SS07Session.has("recordings.read"))return;
    const current=generation;
    busy=true;next.disabled=true;form.querySelector("button").disabled=true;
    const controller=new AbortController(), timeout=setTimeout(()=>controller.abort(),10000);
    try{
      const params=new URLSearchParams(query);params.set("limit","50");
      if(nextPage&&cursor)params.set("cursor",cursor);
      const response=await SS07Session.request("/api/v1/recordings?"+params,{signal:controller.signal});
      if(current!==generation)return;
      if(!response.ok){message.textContent=response.status===503?"录像索引尚未就绪。数据盘接入并配置后可查询录像。":"录像查询失败，请检查时间范围或稍后重试。";cursor=null;rows.replaceChildren();return;}
      const data=(await response.json()).data;
      if(current!==generation)return;
      if(!Array.isArray(data.items)||data.items.length>200)throw new Error("invalid catalog");
      rows.replaceChildren();data.items.forEach(show);cursor=data.next_cursor;
      const gap=data.events.some(event=>event.kind==="RECORDING_GAP");
      message.textContent=`本页 ${data.items.length} 条录像${data.disk_online?"":" · 数据盘未就绪，当前仅显示索引"}${gap?" · 存在录像中断记录，请检查服务日志":""}`;
    }catch(error){message.textContent="录像服务连接失败，请稍后重试。";rows.replaceChildren();cursor=null;}
    finally{clearTimeout(timeout);busy=false;next.disabled=!cursor;form.querySelector("button").disabled=false;}
  }
  form.addEventListener("submit",event=>{event.preventDefault();if(busy)return;query=new URLSearchParams();
    for(const [key,value] of new FormData(form))if(value)query.set(key,key==="start"||key==="end"?new Date(value).toISOString():value);
    if((query.has('time_start_us')||query.has('time_end_us'))&&!query.has('epoch')){message.textContent="按外部时码查询时，请同时填写 epoch。";return;}
    cursor=null;fetchPage();});
  next.addEventListener("click",()=>fetchPage(true));
  video.addEventListener("error",()=>{message.textContent="无法读取或解码此录像，请确认数据盘状态，也可导出文件检查。";});
  window.addEventListener("pagehide",()=>{video.pause();video.removeAttribute("src");video.load();});
  SS07Session.subscribe(()=>{
    generation++;cursor=null;rows.replaceChildren();playback.hidden=true;video.pause();video.removeAttribute("src");video.load();
    if(SS07Session.has("recordings.read"))fetchPage();
  });
})();
