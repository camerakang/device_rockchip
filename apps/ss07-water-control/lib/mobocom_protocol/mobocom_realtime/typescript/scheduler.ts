import {budget,Client,BudgetNode,SupersededError,ConnectionChangedError} from './client';
import {Capabilities,Source,State,Status,Target} from './wire';

export type ResourceKey={endpoint:number;resource:number};
export type Schedule={uid:bigint;caps:Capabilities;status:Status;hz:number;statusHz:number;heartbeatHz:number;targetTtlUs:number;leaseTtlUs:number;resources?:ResourceKey[]};
type Slot=Schedule&{connectionVersion:number;selected:ResourceKey[];failed:boolean;values:Target[]|null;targetDue:number;statusDue:number;heartbeatDue:number;targetBusy:boolean;statusBusy:boolean;heartbeatBusy:boolean};
// Explicit tick; applications choose their timer/task. No promise of desktop hard realtime.
export class RealtimeScheduler {
    private slots:Slot[]=[];private running=false;
    onError:(uid:bigint,error:unknown)=>void=()=>{};
    onStatus:(uid:bigint,status:Status)=>void=()=>{};
    constructor(private client:Client){}
    configure(schedules:Schedule[],hostOverheadUs:number){
        if(this.running)throw Error('先停止调度');
        for(const s of schedules){if(this.client.needsRearm(s.uid)||s.status.state!==State.Active||s.status.source!==Source.Mobocom||s.status.owner!==this.client.session||s.status.fault)throw Error('节点控制权未确认');
            if(!Number.isFinite(s.targetTtlUs)||!Number.isFinite(s.leaseTtlUs)||s.targetTtlUs<s.caps.targetMinUs||s.targetTtlUs>s.caps.targetMaxUs||s.leaseTtlUs<s.caps.leaseMinUs||s.leaseTtlUs>s.caps.leaseMaxUs||1e6/s.hz+hostOverheadUs>=s.targetTtlUs||Math.min(1e6/s.hz,s.heartbeatHz>0?1e6/s.heartbeatHz:Infinity)+hostOverheadUs>=s.leaseTtlUs)throw Error('更新间隔与已协商保护期限不匹配');
        }
        const selections=schedules.map(s=>{
            const keys=s.resources??s.caps.resources;
            if(!keys.length||keys.length>16||new Set(keys.map(r=>`${r.endpoint}/${r.resource}`)).size!==keys.length)throw Error('所选资源为空或重复');
            const chosen=keys.map(k=>{const r=s.caps.resources.find(r=>r.endpoint===k.endpoint&&r.resource===k.resource);if(!r)throw Error('所选资源不存在');return r;});
            for(const r of chosen)if(r.group&&s.caps.resources.some(other=>other.group===r.group&&!chosen.includes(other)))throw Error('协调组必须完整选择');
            if(s.caps.resources.some((r,i)=>(s.status.activeMask&(1<<i))&&!chosen.includes(r)))throw Error('已有活跃资源不在选择中，先STOP并重新接管');
            return chosen.map(r=>({endpoint:r.endpoint,resource:r.resource}));
        });
        const nodes:BudgetNode[]=schedules.map((s,i)=>({uid:s.uid,hz:s.hz,maxTargetHz:s.caps.maxTargetHz,resources:selections[i].length,statusHz:s.statusHz,statusPages:Math.ceil(s.caps.resources.length/8),heartbeatHz:s.heartbeatHz}));
        const report=budget(nodes,hostOverheadUs);
        this.slots=schedules.map((s,i)=>({...s,connectionVersion:this.client.connectionVersion(s.uid),selected:selections[i],failed:false,values:null,targetDue:0,statusDue:0,heartbeatDue:0,targetBusy:false,statusBusy:false,heartbeatBusy:false}));return report;
    }
    setTargets(uid:bigint,values:Target[]){
        const s=this.slots.find(s=>s.uid===uid);if(!s)throw Error('节点未配置');
        if(s.connectionVersion!==this.client.connectionVersion(uid))throw new ConnectionChangedError(uid);
        if(values.length!==s.selected.length)throw Error('调度器需要完整的所选资源目标快照');
        const seen=new Set<string>();
        for(const v of values){const key=`${v.endpoint}/${v.resource}`,r=s.caps.resources.find(r=>r.endpoint===v.endpoint&&r.resource===v.resource);if(!s.selected.some(k=>k.endpoint===v.endpoint&&k.resource===v.resource)||seen.has(key)||!r||!Number.isInteger(v.value)||v.value<r.minimum||v.value>r.maximum)throw Error('目标范围或资源');seen.add(key);}
        if(this.running&&s.values&&values.some(v=>!s.values!.some(old=>old.endpoint===v.endpoint&&old.resource===v.resource&&old.value===v.value)))this.client.refreshQueuedTargets(uid,s.status.boot,s.status.generation,values,s.status.epoch);
        s.values=values.map(v=>({...v}));
    }
    start(){if(!this.slots.length||this.slots.some(s=>!s.values||s.connectionVersion!==this.client.connectionVersion(s.uid)||this.client.needsRearm(s.uid)))throw Error('先重新确认连接、配置并提供初始目标');this.running=true;}
    private fail(s:Slot,e:unknown){if(e instanceof SupersededError||!this.running||s.failed)return;s.failed=true;s.values=null;this.client.discardTargets(s.uid);this.onError(s.uid,e);}
    tick(nowUs:number){
        if(!this.running)return;
        for(const s of this.slots){
            if(!s.values)continue;
            if(s.connectionVersion!==this.client.connectionVersion(s.uid)){this.fail(s,new ConnectionChangedError(s.uid));continue;}
            if(nowUs>=s.targetDue&&!s.targetBusy){s.targetBusy=true;const period=1e6/s.hz;
                // 按固定周期推进，避免每次小迟延都移动相位；跳过错过的周期，仅发最新值。
                s.targetDue=s.targetDue?s.targetDue+(Math.floor((nowUs-s.targetDue)/period)+1)*period:nowUs+period;
                void this.client.writeTargets(s.uid,s.status.boot,s.status.generation,s.values,s.status.epoch).catch(e=>this.fail(s,e)).finally(()=>{s.targetBusy=false;});}
            if(s.statusHz&&nowUs>=s.statusDue&&!s.statusBusy){s.statusBusy=true;s.statusDue=nowUs+1e6/s.statusHz;
                void (async()=>{for(let cursor=0;;){const result=await this.client.getStatus(s.uid,cursor);this.onStatus(s.uid,result);
                    if(result.boot!==s.status.boot||result.epoch!==s.status.epoch||result.generation!==s.status.generation||result.owner!==this.client.session||result.state!==State.Active||result.fault)throw Error('节点失去控制权或发生故障');
                    if(result.next===0xffff)break;cursor=result.next;
                }})().catch(e=>this.fail(s,e)).finally(()=>{s.statusBusy=false;});}
            if(s.heartbeatHz&&nowUs>=s.heartbeatDue&&!s.heartbeatBusy){s.heartbeatBusy=true;s.heartbeatDue=nowUs+1e6/s.heartbeatHz;
                void this.client.heartbeat(s.uid,s.status.boot,s.status.generation,s.status.epoch).catch(e=>this.fail(s,e)).finally(()=>{s.heartbeatBusy=false;});}
        }
    }
    async finish(finals:Map<bigint,Target[]>,timeoutUs:number){
        const stale=this.slots.find(s=>s.connectionVersion!==this.client.connectionVersion(s.uid));
        if(stale)throw new ConnectionChangedError(stale.uid);
        if(this.slots.some(s=>!finals.has(s.uid)))throw Error('缺少节点终目标');
        // Validate all snapshots before ending ordinary scheduling.
        for(const s of this.slots){const values=finals.get(s.uid)!;const seen=new Set<string>();
            if(values.length!==s.selected.length)throw Error('终目标需要完整的所选资源快照');
            for(const v of values){const key=`${v.endpoint}/${v.resource}`,r=s.caps.resources.find(r=>r.endpoint===v.endpoint&&r.resource===v.resource);if(!s.selected.some(k=>k.endpoint===v.endpoint&&k.resource===v.resource)||seen.has(key)||!r||!Number.isInteger(v.value)||v.value<r.minimum||v.value>r.maximum)throw Error('终目标范围或资源');seen.add(key);}
        }
        this.running=false;for(const s of this.slots){s.values=null;this.client.discardTargets(s.uid);}
        return Promise.all(this.slots.map(async s=>{try{return {uid:s.uid,ack:await this.client.finish(s.uid,s.status.boot,s.status.generation,finals.get(s.uid)!,timeoutUs,s.status.epoch)};}catch(error){return {uid:s.uid,error};}}));
    }
    async stop(estop=false){this.running=false;const nodes=this.slots.map(s=>s.uid);for(const s of this.slots){s.values=null;this.client.discardTargets(s.uid);}const results=[];for(const uid of nodes){try{results.push({uid,ack:await this.client.stop(uid,estop)});}catch(error){results.push({uid,error});}}return results;}
}
