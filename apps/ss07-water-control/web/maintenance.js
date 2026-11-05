"use strict";
(() => {
  const editor=document.getElementById("config-editor"), message=document.getElementById("config-message");
  const save=document.getElementById("config-save"), reload=document.getElementById("config-reload");
  const preview=document.getElementById("config-plan"), apply=document.getElementById("config-apply"), recover=document.getElementById("config-recover");
  const planMessage=document.getElementById("config-plan-message"), applicationStatus=document.getElementById("config-application-status");
  const names={media:"视频分发",storage:"录像",timecode:"时码",maintenance:"维护",api:"管理接口",gateway:"网页入口",
    "camera.front":"前视采集","camera.back":"后视采集","camera.left":"左视采集","camera.right":"右视采集","camera.down":"下视采集"};
  const phases={PREPARED:"准备应用",STOPPING:"停止受影响的服务",SWITCHING:"切换运行配置",STARTING:"启动服务",VERIFYING:"检查生效结果",
    APPLIED:"配置已生效",ROLLING_BACK:"恢复原配置",ROLLED_BACK:"应用失败，已恢复原配置",ROLLBACK_FAILED:"恢复尚未完成",RECOVERY_PENDING:"等待恢复验证",FAILED:"应用未完成"};
  let revision=null, remoteRevision=null, activeRevision=null, prepared=null, transaction=null, available=false, applying=false;
  let busy=false, generation=0, dirty=false, poll=null;
  function buttons(){
    const ready=!busy&&SS07Session.has("maintain");
    preview.disabled=!ready||!available||applying||dirty||!revision||revision!==remoteRevision||!activeRevision;
    apply.disabled=preview.disabled||!prepared||!prepared.plan.services.length||prepared.revision!==revision||prepared.active_revision!==activeRevision;
    recover.disabled=!ready||!available||applying||!transaction||transaction.phase!=="ROLLBACK_FAILED";
  }
  function schedule(){clearTimeout(poll);poll=setTimeout(()=>readConfig(false),1500);}
  async function readConfig(resetEditor=true){
    if(busy||!SS07Session.has("maintain"))return;
    const current=generation;busy=true;save.disabled=true;reload.disabled=true;
    try{
      const response=await SS07Session.request("/api/v1/config");
      if(!response.ok)throw new Error("configuration unavailable");
      const data=(await response.json()).data;if(current!==generation)return;
      remoteRevision=data.revision;activeRevision=data.active_revision;
      if(resetEditor){revision=data.revision;editor.value=data.persisted?JSON.stringify(data.persisted,null,2):"";dirty=false;prepared=null;planMessage.textContent="";}
      editor.disabled=!data.mutable||!data.persisted;save.disabled=editor.disabled;
      available=data.application?.state==="RUNNING";transaction=data.application?.status?.transaction||null;
      applying=Boolean(data.application?.status?.busy);
      message.textContent=data.error?"配置不可读，请联系本机维护人员。":revision!==remoteRevision?
        "配置已被其他操作修改；当前编辑内容保留，请重新读取后合并。":data.pending_apply?
        "配置已保存，尚未应用。检查影响后可执行应用；发生故障会尝试恢复原配置。":"当前保存值与运行配置一致；各服务的实际结果如下。";
      const rows=Object.entries(data.apply_result?.services||{}).map(([name,value])=>{
        const label=value.state!=="APPLIED"?"尚未确认":value.reason==="unconfigured"?"已加载（设备未配置）":
          value.reason==="disabled"?"已加载（功能未启用）":"已确认生效";
        return `${names[name]||name}：${label}`;
      });
      applicationStatus.textContent=(transaction?(phases[transaction.phase]||"状态待确认")+"\n":"")+rows.join("\n");
      if(applying)schedule();
    }catch(error){if(current===generation){message.textContent="无法读取配置，请稍后重试。";if(applying)schedule();}}
    finally{busy=false;reload.disabled=false;buttons();}
  }
  document.getElementById("config-form").addEventListener("submit",async event=>{
    event.preventDefault();if(busy||!revision||!SS07Session.has("maintain"))return;
    let candidate;try{candidate=JSON.parse(editor.value);}catch(error){message.textContent="配置不是有效 JSON，请修正后保存。";return;}
    busy=true;save.disabled=true;reload.disabled=true;
    const current=generation;
    try{
      const response=await SS07Session.request("/api/v1/config",{method:"PUT",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({candidate,expected_revision:revision})});
      const result=await response.json();if(current!==generation)return;
      if(response.ok){revision=remoteRevision=result.data.revision;dirty=false;prepared=null;message.textContent="配置已保存，尚未应用。请检查应用影响。";}
      else if(response.status===409){message.textContent="配置已被其他维护操作修改。请先重新读取，再合并修改。";revision=null;}
      else if(response.status===422)message.textContent="配置未通过校验。请核对字段、端口、设备绑定和参数。";
      else {message.textContent="保存结果未确认。请重新读取配置核对结果，不要重复提交。";revision=null;}
    }catch(error){message.textContent="连接中断，保存结果未确认。请重新读取配置核对结果。";revision=null;}
    finally{busy=false;save.disabled=!revision;reload.disabled=false;buttons();}
  });
  editor.addEventListener("input",()=>{dirty=true;prepared=null;buttons();});
  reload.addEventListener("click",()=>readConfig());
  async function operation(kind){
    if(busy||!SS07Session.has("maintain"))return;
    const body=kind==="recover"?{expected_operation_id:transaction.operation_id}:{expected_revision:revision,expected_active_revision:activeRevision};
    busy=true;buttons();save.disabled=true;reload.disabled=true;const current=generation;
    try{
      const response=await SS07Session.request("/api/v1/config/"+kind,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
      const result=await response.json();if(current!==generation)return;
      if(!response.ok){prepared=null;planMessage.textContent=response.status===409?"配置已改变、正在应用或需要先恢复，请重新读取状态。":"暂不能执行此配置应用，请检查设备参数和服务状态。";return;}
      if(kind==="plan"){
        prepared=result.data;
        planMessage.textContent=prepared.plan.services.length?"将重启："+prepared.plan.services.map(n=>names[n]||n).join("、")+"。"+
          (prepared.plan.recording_restart?"录像服务将短暂重启。":"录像进程继续运行，受影响的视频源可能产生片段缺口。")+
          (prepared.plan.session_restart?"管理接口重启后需要重新登录。":""):"没有待应用的修改。";
      }else{
        prepared=null;applying=true;planMessage.textContent="应用请求已受理，正在检查各服务。连接中断时请重新登录并读取结果。";schedule();
      }
    }catch(error){prepared=null;planMessage.textContent="请求结果未确认，请重新读取状态，避免重复提交。";schedule();}
    finally{busy=false;save.disabled=!revision;reload.disabled=false;buttons();}
  }
  preview.addEventListener("click",()=>operation("plan"));
  apply.addEventListener("click",()=>operation("apply"));
  recover.addEventListener("click",()=>operation("recover"));
  const logs=document.getElementById("log-rows"), logMessage=document.getElementById("log-message");
  const source=document.getElementById("log-source"), next=document.getElementById("log-next"), download=document.getElementById("log-export");
  let logCursor=null, logBusy=false;
  async function readLogs(more=false){
    if(logBusy||!SS07Session.has("maintain"))return;
    logBusy=true;next.disabled=true;const current=generation;
    const params=new URLSearchParams({source:source.value,limit:"50"});if(more&&logCursor)params.set("cursor",logCursor);
    try{
      const response=await SS07Session.request("/api/v1/logs?"+params);if(!response.ok)throw new Error("log unavailable");
      const data=(await response.json()).data;if(current!==generation)return;
      logs.textContent=JSON.stringify(data.items,null,2);logCursor=data.next_cursor;
      logMessage.textContent=`本页 ${data.items.length} 条；日志仅保留有限历史。`;
      params.set("download","1");download.href="/api/v1/logs?"+params;download.hidden=false;
    }catch(error){logs.textContent="";logCursor=null;download.hidden=true;logMessage.textContent="此日志尚未就绪或无法读取。";}
    finally{logBusy=false;next.disabled=!logCursor;}
  }
  document.getElementById("log-read").addEventListener("click",()=>readLogs());
  next.addEventListener("click",()=>readLogs(true));
  source.addEventListener("change",()=>{logCursor=null;readLogs();});
  SS07Session.subscribe(()=>{
    generation++;revision=null;editor.value="";editor.disabled=true;save.disabled=true;
    clearTimeout(poll);prepared=null;transaction=null;available=false;applying=false;dirty=false;
    planMessage.textContent="";applicationStatus.textContent="";buttons();
    logs.textContent="";download.hidden=true;logCursor=null;
    if(SS07Session.has("maintain")){readConfig();readLogs();}
  });
})();
