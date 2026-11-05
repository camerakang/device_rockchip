"use strict";
window.SS07Session = (() => {
  const listeners=new Set(), form=document.getElementById("login-form");
  const message=document.getElementById("login-message"), logout=document.getElementById("logout");
  let state=null, pending=null;
  const denied=()=>({authenticated:false,authentication_required:true,permissions:[],role:null,csrf_token:null});
  const has=permission=>Boolean(state?.permissions.includes(permission));
  function publish(value){
    const changed=JSON.stringify([state?.username,state?.role,state?.csrf_token,state?.permissions]) !==
      JSON.stringify([value.username,value.role,value.csrf_token,value.permissions]);
    state=value;
    document.getElementById("login-panel").hidden=!value.authentication_required||value.authenticated;
    document.getElementById("workspace").hidden=!has("monitor");
    document.getElementById("recording-panel").hidden=!has("recordings.read");
    document.getElementById("maintenance-panel").hidden=!has("maintain");
    logout.hidden=!value.authenticated||!value.authentication_required;
    const roles={monitor:"监看",reader:"录像读取",maintainer:"维护",development_read_only:"本机只读开发"};
    document.getElementById("session-label").textContent=value.authenticated&&!value.authentication_required?"本机操作":value.authenticated?`${value.username} · ${roles[value.role]}`:roles[value.role]||"请登录";
    if(changed)for(const listener of listeners)listener(value);
  }
  async function refresh(){
    if(pending)return pending;
    pending=(async()=>{
      try{
        const response=await fetch("/api/v1/session",{cache:"no-store",credentials:"same-origin"});
        if(!response.ok)throw new Error("session unavailable");
        publish((await response.json()).data);
      }catch(error){message.textContent="身份服务暂不可用，请稍后重试。";if(!state)publish(denied());}
      finally{pending=null;}
    })();
    return pending;
  }
  async function request(url,options={}){
    const headers=new Headers(options.headers||{});
    if(!["GET","HEAD"].includes((options.method||"GET").toUpperCase())&&state?.csrf_token)
      headers.set("X-CSRF-Token",state.csrf_token);
    const response=await fetch(url,{...options,headers,credentials:"same-origin",cache:"no-store"});
    if(response.status===401){
      if(state?.authenticated&&!state.authentication_required)await refresh();
      else{publish(denied());message.textContent="会话已失效，请重新登录。";}
    }
    return response;
  }
  form.addEventListener("submit",async event=>{
    event.preventDefault();const button=form.querySelector("button");button.disabled=true;
    message.textContent="正在登录…";
    try{
      const fields=new FormData(form);
      const response=await fetch("/api/v1/login",{method:"POST",headers:{"Content-Type":"application/json"},
        credentials:"same-origin",body:JSON.stringify({username:fields.get("username"),password:fields.get("password")})});
      form.elements.password.value="";
      if(!response.ok){message.textContent=response.status===429?"登录尝试过于频繁，请稍后再试。":response.status===503?"身份服务暂不可用。":"用户名或密码不正确。";return;}
      publish((await response.json()).data);message.textContent="";
    }catch(error){message.textContent="无法连接身份服务，请稍后重试。";}
    finally{form.elements.password.value="";button.disabled=false;}
  });
  logout.addEventListener("click",async()=>{
    logout.disabled=true;
    try{
      const response=await request("/api/v1/logout",{method:"POST",headers:{"Content-Type":"application/json"},body:"{}"});
      publish(denied());message.textContent=response.ok?"已退出。":"退出请求未确认，请关闭页面并联系维护人员。";
    }catch(error){publish(denied());message.textContent="退出请求未确认，请关闭页面并联系维护人员。";}
    finally{logout.disabled=false;}
  });
  setInterval(refresh,30000);refresh();
  return {has,request,refresh,subscribe(listener){listeners.add(listener);if(state)listener(state);}};
})();
