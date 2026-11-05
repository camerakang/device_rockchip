"use strict";
(() => {
  const panel=document.getElementById("sonar-archive-panel"),form=document.getElementById("sonar-archive-search");
  const rows=document.getElementById("sonar-archive-rows"),message=document.getElementById("sonar-archive-message"),next=document.getElementById("sonar-archive-next");
  const preview=document.getElementById("sonar-sample-preview"),samples=document.getElementById("sonar-sample-rows"),sampleMessage=document.getElementById("sonar-sample-message");
  const labels={front:"前方",back:"后方",left:"左侧",right:"右侧",down:"下方"};
  const states={COMPLETE:"分段已完成",INCOMPLETE:"中断分段 · 未确认完成",MISSING:"文件缺失",INVALID:"文件校验异常",DELETING:"正在清理"};
  let generation=0,cursor=null,query=new URLSearchParams(),busy=false,previewGeneration=0;
  function node(tag,text){const value=document.createElement(tag);value.textContent=text;return value;}
  function cell(row,text){row.append(node("td",text));}
  async function read(url){
    const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),10000);
    try{
      const response=await SS07Session.request(url,{signal:controller.signal});
      return {ok:response.ok,data:response.ok?(await response.json()).data:null};
    }
    finally{clearTimeout(timer);}
  }
  async function showSamples(id){
    const current=generation,selected=++previewGeneration;
    preview.hidden=false;samples.replaceChildren();sampleMessage.textContent="正在校验并读取分段…";
    try{
      const params=new URLSearchParams(query);params.set("limit","20");
      const response=await read(`/api/v1/sonars/archives/${id}/samples?`+params);
      if(current!==generation||selected!==previewGeneration)return;
      if(!response.ok)throw new Error("archive unavailable");
      const data=response.data;
      if(current!==generation||selected!==previewGeneration)return;
      for(const item of data.items){
        const row=document.createElement("tr");
        cell(row,labels[item.direction]||item.kind);
        cell(row,item.kind==="MEASUREMENT"&&item.valid?`${item.distance_mm} mm`:"—");
        cell(row,new Date(item.observed_at_utc).toLocaleString());
        const tag=item.time_tag;
        cell(row,tag.quality==="ESTIMATED"?`估计 · epoch ${tag.source_epoch} · ${tag.estimated_time_us} µs`:"无可用时码");
        samples.append(row);
      }
      sampleMessage.textContent=`本次显示 ${data.items.length} 条${data.contains_gap?" · 分段含缺口":""}${data.evidence_kind==="synthetic_test"?" · 测试数据":""}；完整记录可导出。`;
    }catch(error){if(current===generation&&selected===previewGeneration)sampleMessage.textContent="记录读取失败，请检查数据盘或重新查询。";}
  }
  function show(item){
    const row=document.createElement("tr");
    cell(row,item.directions.map(d=>labels[d]||d).join("、")||"总线状态");
    cell(row,new Date(item.start_utc).toLocaleString());
    cell(row,`${item.event_count} 条 / ${(Number(item.size_bytes)/1048576).toFixed(2)} MiB`);
    let status=states[item.state]||"状态未知";
    if(item.contains_gap)status+=" · 含采集缺口";
    if(item.locked)status+=" · 已锁定";
    if(item.evidence_kind==="synthetic_test")status+=" · 测试数据";
    if(item.availability==="disk_offline")status+=" · 数据盘离线";
    cell(row,status);
    const actions=document.createElement("td");
    if(item.state==="COMPLETE"&&item.availability==="indexed_unverified"&&/^[a-f0-9]{64}$/.test(item.archive_id)){
      const button=node("button","查看记录");button.type="button";button.onclick=()=>showSamples(item.archive_id);
      const link=node("a","导出完整分段");link.href=`/api/v1/sonars/archives/${item.archive_id}/content?download=1`;link.setAttribute("download","");
      actions.append(button,link);
    }else actions.textContent="—";
    row.append(actions);rows.append(row);
  }
  async function fetchPage(more=false){
    if(busy||!SS07Session.has("recordings.read"))return;
    const current=generation;busy=true;next.disabled=true;form.querySelector("button").disabled=true;
    try{
      const params=new URLSearchParams(query);params.set("limit","50");if(more&&cursor)params.set("cursor",cursor);
      const response=await read("/api/v1/sonars/archives?"+params);
      if(current!==generation)return;
      if(!response.ok)throw new Error("archive unavailable");
      const data=response.data;
      if(current!==generation)return;
      if(!Array.isArray(data.items)||data.items.length>200)throw new Error("invalid archive list");
      rows.replaceChildren();data.items.forEach(show);cursor=data.next_cursor;
      message.textContent=`本页 ${data.items.length} 个分段${data.disk_online?"":" · 数据盘离线，当前仅显示索引"}。读取或导出时校验文件完整性。`;
    }catch(error){if(current===generation){message.textContent="采样记录尚未就绪，请检查归档配置与数据盘。";rows.replaceChildren();cursor=null;}}
    finally{if(current===generation){busy=false;next.disabled=!cursor;form.querySelector("button").disabled=false;}}
  }
  form.addEventListener("submit",event=>{
    event.preventDefault();if(busy)return;query=new URLSearchParams();
    for(const [key,value] of new FormData(form))if(value)query.set(key,value);
    if((query.has("start_us")||query.has("end_us"))&&!query.has("source_epoch")){message.textContent="按外部时码查询时，请同时填写 epoch。";return;}
    cursor=null;previewGeneration++;preview.hidden=true;fetchPage();
  });
  next.onclick=()=>fetchPage(true);
  SS07Session.subscribe(()=>{
    generation++;previewGeneration++;cursor=null;busy=false;rows.replaceChildren();samples.replaceChildren();preview.hidden=true;form.querySelector("button").disabled=false;
    panel.hidden=!SS07Session.has("recordings.read");
    if(!panel.hidden)fetchPage();
  });
})();
