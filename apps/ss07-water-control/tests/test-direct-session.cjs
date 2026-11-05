'use strict';
// Run the actual browser session controller with deterministic DOM/fetch boundaries.
const assert=require('node:assert/strict'), fs=require('node:fs'), vm=require('node:vm'), path=require('node:path');
const nodes=new Map();
const node=id=>{if(!nodes.has(id))nodes.set(id,{hidden:true,textContent:'',addEventListener(){}});return nodes.get(id);};
let generation=1, denied=false, refreshed=0, requests=[];
const context={window:{},document:{getElementById:node},Headers,setInterval(){},fetch:async(url,options={})=>{
  requests.push({url,options});
  if(url==='/api/v1/session'){
    refreshed++;
    return {ok:true,json:async()=>({data:{authenticated:true,authentication_required:false,
      username:'local-operator',role:'maintainer',permissions:['monitor','recordings.read','maintain'],csrf_token:'csrf-'+generation}})};
  }
  return {status:denied?401:200};
}};
vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../web/session.js'),'utf8'),context);
(async()=>{
  const session=context.window.SS07Session;
  await session.refresh();
  for(const id of ['workspace','recording-panel','maintenance-panel'])assert.equal(node(id).hidden,false,id);
  assert.equal(node('login-panel').hidden,true);assert.equal(node('logout').hidden,true);
  assert.equal(session.has('maintain'),true);
  await session.request('/api/v1/config/plan',{method:'POST',body:'{}'});
  assert.equal(requests.at(-1).options.headers.get('X-CSRF-Token'),'csrf-1');
  generation++;denied=true;const before=refreshed;
  await session.request('/api/v1/config/plan',{method:'POST',body:'{}'});
  assert.equal(refreshed,before+1);
  assert.equal(node('login-panel').hidden,true);
  assert.equal(requests.filter(r=>r.url==='/api/v1/config/plan').length,2,'do not replay a mutation after renewal');
  denied=false;await session.request('/media/front/whep',{method:'POST'});
  assert.equal(requests.at(-1).options.headers.get('X-CSRF-Token'),'csrf-2');
  assert.equal(requests.some(r=>r.url==='/api/v1/login'),false);
  console.log('direct session UI: initial entry, permissions, CSRF and expired-session recovery passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
