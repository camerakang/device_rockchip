import {Ack,ack,ALL_NODES,Bytes,capsItem,Capabilities,Command as C,decode,discovery,encode,ErrorCode,Frame,guard,Info,info,Kind,Source,State,Status,status,Target,targets,view} from './wire';
import {Endpoint,sameEndpoint,validateEndpoint} from './network';
import {configurationSnapshot,configurationWrite} from './configuration';
import {MOTION_PROFILE_SCHEMA,LIVE_MOTION_PROFILE_SCHEMA,MotionLimit,motionLimits,motionProfile} from './motion_profile';
import {FOCUS_CONTROL_SCHEMA,FocusControlSource,focusControl,focusSource} from './focus_control';

export type Wire={
    // null selects the fixed multicast group; never concatenate datagrams.
    send:(bytes:Uint8Array,to:Endpoint|null)=>Promise<void>;
    close:()=>Promise<void>;
};
export type Clock={nowUs:()=>number;sleepUs:(us:number)=>Promise<void>};
// Host transport deadline, not the device's physical response window or TTL.
export type DiscoveredDevice={epoch:bigint;generation:number;uid:bigint;type:number;firmware:number;boot:bigint};
export type DiscoveryReport={devices:DiscoveredDevice[];conflicts:bigint[];missing:bigint[];reconnected:bigint[];unconfirmed:bigint[]};
export type ClientOptions={transactionTimeoutMs?:number};
// Emitted once before the first send; retain even if all ACKs are lost.
export type ConfigurationRequest={uid:bigint;session:bigint;sequence:number;frame:Uint8Array};
const DiscoveryWindowUs=100000;
const defaultClock:Clock={nowUs:()=>performance.now()*1000,sleepUs:us=>new Promise(r=>setTimeout(r,Math.max(0,us/1000)))};
export class ProtocolError extends Error {constructor(public code:number,public detail:number,public snapshot?:Ack){super(`协议错误 ${ErrorCode[code]??code} (${detail})`);}}
export class SupersededError extends Error {constructor(){super('已被新目标替代');}}
export class ConnectionChangedError extends Error {constructor(public uid:bigint){super(`设备 ${uid} 连接已变化，需重新确认并显式启动`);}}
type Job={run:()=>Promise<unknown>;resolve:(v:unknown)=>void;reject:(e:unknown)=>void;key?:bigint;motion:boolean;uid?:bigint};
export type BudgetNode={uid:bigint;hz:number;maxTargetHz:number;resources:number;statusHz:number;statusPages:number;heartbeatHz:number};
export function budget(devices:BudgetNode[],hostOverheadUs=0){
    if(new Set(devices.map(n=>n.uid)).size!==devices.length||!Number.isFinite(hostOverheadUs)||hostOverheadUs<0)throw new ProtocolError(ErrorCode.Bandwidth,0);
    let us=0;const details=[];
    for(const n of devices){
        if(typeof n.uid!=='bigint'||n.uid<=0n||n.uid>0xffffffffffffffffn||![n.resources,n.hz,n.maxTargetHz,n.statusHz,n.heartbeatHz,n.statusPages].every(Number.isFinite)||!Number.isInteger(n.resources)||!Number.isInteger(n.statusPages)||n.resources<1||n.resources>16||n.hz<=0||n.hz>n.maxTargetHz||n.statusHz<0||n.heartbeatHz<0||n.statusPages<1||n.statusPages>2)throw new ProtocolError(ErrorCode.Bandwidth,0);
        const bytes=(n:number)=>Math.max(46,n+28)+38;
        const cycle=(request:number,response:number)=>(bytes(request)+bytes(response))*8/100;
        const target=39+21+n.resources*8,reply=79;
        const used=n.hz*cycle(target,reply)+n.statusHz*n.statusPages*cycle(41,239)+n.heartbeatHz*cycle(59,79);
        us+=used;details.push({uid:n.uid,targetBytes:target,replyBytes:reply,usPerSecond:used});
    }
    if(us>800000)throw new ProtocolError(ErrorCode.Bandwidth,0);
    return {utilization:us/1e6,details,hostOverheadUs,linkBps:100000000};
}
export class Client {
    private sessionValue:bigint;
    get session(){return this.sessionValue;}
    private renewing=false;
    private safetyCredentials=new Map<bigint,{boot:bigint;generation:number;epoch:bigint}>();
    private sequence=0;private queue:Job[]=[];private runningNodes=new Set<bigint>();private closed=false;
    private endpoints=new Map<bigint,Endpoint>();
    private conflicts=new Set<bigint>();
    private pending=new Map<number,{uid?:bigint;receive:(f:Frame,from?:Endpoint)=>void;reject:(e:unknown)=>void}>();
    private identities=new Map<bigint,DiscoveredDevice>();
    private connectionVersions=new Map<bigint,number>();
    private rearmRequired=new Set<bigint>();
    private reconnecting=new Set<bigint>();
    private ambiguousEndpoints=new Map<bigint,Endpoint[]>();
    private scanning=false;
    private stopGeneration=0;
    private stoppedUids=new Map<bigint,number>();
    private readonly transactionTimeoutMs:number;
    private targetVersions=new Map<bigint,number>();
    private pendingTargets=new Map<bigint,{boot:bigint;generation:number;epoch:bigint;values:Map<string,Target>}>();
    onFrame:(f:Frame)=>void=()=>{};
    constructor(private wire:Wire,private clock:Clock=defaultClock,session?:bigint,options:ClientOptions={}){
        this.transactionTimeoutMs=options.transactionTimeoutMs??30;
        if(!Number.isInteger(this.transactionTimeoutMs)||this.transactionTimeoutMs<30||this.transactionTimeoutMs>2000)throw Error('主机事务超时须为30～2000 ms');
        if(session!==undefined)this.sessionValue=session;else{const bytes=new Uint32Array(2);globalThis.crypto.getRandomValues(bytes);this.sessionValue=(BigInt(bytes[0])<<32n)|BigInt(bytes[1]);}
        if(typeof this.session!=='bigint'||this.session<=0n||this.session>0xffffffffffffffffn)throw Error('会话必须为非零u64');
    }
    setEndpoint(uid:bigint,address:string,port:number){
        validateEndpoint({uid,address,port});
        const previous=this.endpoints.get(uid),endpoint={address,port};
        if(previous&&!sameEndpoint(previous,endpoint))throw new ProtocolError(ErrorCode.Address,0);
        this.endpoints.set(uid,endpoint);
    }
    connectionVersion(uid:bigint){return this.connectionVersions.get(uid)??0;}
    needsRearm(uid:bigint){return this.rearmRequired.has(uid);}
    getEndpoint(uid:bigint){const endpoint=this.endpoints.get(uid);return endpoint?{...endpoint}:undefined;}
    private invalidateConnection(uid:bigint){
        this.connectionVersions.set(uid,this.connectionVersion(uid)+1);
        this.rearmRequired.add(uid);this.reconnecting.add(uid);
        this.stoppedUids.set(uid,(this.stoppedUids.get(uid)??0)+1);
        this.safetyCredentials.delete(uid);this.cancelQueuedTargets(uid);
        const error=new ConnectionChangedError(uid);
        const jobs=this.queue.filter(j=>j.uid===uid);this.queue=this.queue.filter(j=>j.uid!==uid);
        for(const job of jobs)job.reject(error);
        for(const pending of this.pending.values())if(pending.uid===uid)pending.reject(error);
    }
    feed(bytes:Uint8Array,from?:Endpoint){
        if(this.closed)return;
        let f:Frame;try{f=decode(bytes);}catch{return;}
        this.pending.get(f.sequence)?.receive(f,from);
        this.onFrame(f);
    }
    private next(){if(this.sequence===0xffffffff)throw Error('序号耗尽：安全释放后创建新客户端');return ++this.sequence;}
    private enqueue<T>(run:()=>Promise<T>,motion=false,key?:bigint,priority=false,uid?:bigint):Promise<T>{
        if(this.closed)return Promise.reject(Error('连接已关闭'));
        if(this.renewing&&motion)return Promise.reject(new SupersededError());
        return new Promise<T>((resolve,reject)=>{
            if(key!==undefined){const old=this.queue.filter(j=>j.key===key);this.queue=this.queue.filter(j=>j.key!==key);for(const j of old)j.reject(new SupersededError());}
            const job={run,resolve,reject,motion,key,uid:uid??key} as Job;if(priority)this.queue.unshift(job);else this.queue.push(job);void this.drain();
        });
    }
    private drain(){
        if(this.closed)return;
        // UDP is full-duplex: serialize jobs per destination, but allow different
        // destinations to progress independently. ALL_NODES jobs are exclusive.
        for(;;){
            const barrier=this.queue.findIndex(j=>(j.uid??ALL_NODES)===ALL_NODES);
            const index=this.queue.findIndex((j,i)=>{
                const key=j.uid??ALL_NODES;
                if(this.runningNodes.has(ALL_NODES))return false;
                if(key===ALL_NODES)return i===0&&this.runningNodes.size===0;
                return (barrier<0||i<barrier)&&!this.runningNodes.has(key);
            });
            if(index<0)break;
            const j=this.queue.splice(index,1)[0]!;
            const key=j.uid??ALL_NODES;
            this.runningNodes.add(key);
            void j.run().then(j.resolve,j.reject).finally(()=>{this.runningNodes.delete(key);this.drain();});
            if(key===ALL_NODES)break;
        }
    }
    private async sendBounded(raw:Uint8Array,to:Endpoint|null){
        let timer:ReturnType<typeof setTimeout>|undefined;
        try{return await Promise.race([this.wire.send(raw,to),new Promise<never>((_,reject)=>{timer=setTimeout(()=>reject(Error('发送超时')),this.transactionTimeoutMs);})]);}
        finally{clearTimeout(timer);}
    }
    private async exchange(dst:bigint,command:C,payload:Uint8Array,retries=2,key?:bigint,version?:number,onRequest?:(request:ConfigurationRequest)=>void):Promise<Frame>{
        const endpoint=this.endpoints.get(dst);
        if(!endpoint||this.conflicts.has(dst)||this.reconnecting.has(dst))throw new ProtocolError(ErrorCode.Address,0);
        const reads=[C.Hello,C.Info,C.Caps,C.Status,C.ActionStatus,C.ReadConfig];
        if(this.needsRearm(dst)&&!reads.includes(command)&&![C.Stop,C.Estop,C.ResetFault,C.SessionOpen].includes(command))throw new ConnectionChangedError(dst);
        const connectionVersion=this.connectionVersion(dst);
        const sequence=this.next(),generation=this.stopGeneration,uidGeneration=this.stoppedUids.get(dst)??0;
        const stopped=()=>generation!==this.stopGeneration||uidGeneration!==(this.stoppedUids.get(dst)??0);
        const dstUid=dst;
        const raw=encode({kind:Kind.Request,srcUid:0n,dstUid,sequence,command,session:this.session,payload});
        onRequest?.({uid:dst,session:this.session,sequence,frame:raw.slice()});
        for(let attempt=0;attempt<=retries;attempt++){
            if(this.closed)throw Error('连接已关闭');
            if(this.connectionVersion(dst)!==connectionVersion)throw new ConnectionChangedError(dst);
            if(attempt&&(stopped()||(key!==undefined&&this.targetVersions.get(key)!==version)))throw new SupersededError();
            let timer:ReturnType<typeof setTimeout>|undefined,resolve!:(f:Frame)=>void,reject!:(e:unknown)=>void;
            const response=new Promise<Frame>((a,b)=>{resolve=a;reject=b;});void response.catch(()=>{});
            this.pending.set(sequence,{uid:dst,reject,receive:(f,from)=>{if(f.session===this.session&&f.sequence===sequence&&f.command===command&&f.dstUid===0n&&f.srcUid===dstUid&&(!from||sameEndpoint(from,endpoint))&&[Kind.Response,Kind.Ack,Kind.Error].includes(f.kind))resolve(f);}});
            timer=setTimeout(()=>reject(Error('响应超时')),this.transactionTimeoutMs);
            try{
                const f=await Promise.race([response,this.wire.send(raw,endpoint).then(()=>response)]);
                if(this.connectionVersion(dst)!==connectionVersion)throw new ConnectionChangedError(dst);
                if(f.kind===Kind.Error){if(f.payload.length!==44)throw Error('ERROR布局');const snapshot=ack(f.payload.subarray(4));this.safetyCredentials.set(f.srcUid,snapshot);throw new ProtocolError(view(f.payload).getUint16(0,true),view(f.payload).getUint16(2,true),snapshot);}
                if(f.kind!==(reads.includes(command)?Kind.Response:Kind.Ack))throw Error('应答类型不匹配');
                if(f.kind===Kind.Ack)this.safetyCredentials.set(f.srcUid,ack(f.payload));else if(command===C.Status)this.safetyCredentials.set(f.srcUid,status(f.payload));return f;
            }catch(e){
                if(this.connectionVersion(dst)!==connectionVersion)throw new ConnectionChangedError(dst);
                if(e instanceof ProtocolError)throw e;
                if(key!==undefined&&this.targetVersions.get(key)!==version)throw new SupersededError();
                if(attempt===retries||this.closed||stopped())throw e;
            }finally{clearTimeout(timer);this.pending.delete(sequence);}
        }throw Error('事务失败');
    }
    request(uid:bigint,command:C,payload=new Uint8Array()){return this.enqueue(async()=> (await this.exchange(uid,command,payload)).payload,[C.SetSource,C.Heartbeat,C.WriteTargets,C.ExecuteAction,C.ResetFault].includes(command),undefined,false,uid);}
    async readConfiguration(uid:bigint,schema:number){
        const s=configurationSnapshot(await this.request(uid,C.ReadConfig,new Bytes().u16(schema).get()));
        if(s.schema!==schema)throw Error('配置 schema 不匹配');return s;
    }
    async readMotionProfile(uid:bigint){
        const configuration=await this.readConfiguration(uid,MOTION_PROFILE_SCHEMA);
        return {configuration,limits:motionProfile(configuration)};
    }
    // Single bounded transaction; does not stop/discard the gimbal target stream.
    async readFocusControl(uid:bigint){
        const configuration=await this.enqueue(async()=>configurationSnapshot((await this.exchange(uid,C.ReadConfig,new Bytes().u16(FOCUS_CONTROL_SCHEMA).get(),0)).payload),false,undefined,false,uid);
        return {configuration,focus:focusControl(configuration)};
    }
    writeFocusSource(uid:bigint,boot:bigint,generation:number,epoch:bigint,revision:number,source:FocusControlSource,onRequest?:(request:ConfigurationRequest)=>void){
        const payload=configurationWrite(uid,boot,generation,epoch,FOCUS_CONTROL_SCHEMA,revision,focusSource(source));
        return this.enqueue(async()=>{const f=await this.exchange(uid,C.WriteConfig,payload,0,undefined,undefined,onRequest);return {ack:ack(f.payload),session:f.session,sequence:f.sequence};},true,undefined,false,uid);
    }
    // No automatic retry: a physical button/position write is resolved by correlated readback.
    writeFocusCommand(uid:bigint,boot:bigint,generation:number,epoch:bigint,revision:number,values:Uint8Array,onRequest?:(request:ConfigurationRequest)=>void){
        const payload=configurationWrite(uid,boot,generation,epoch,FOCUS_CONTROL_SCHEMA,revision,values);
        return this.enqueue(async()=>{const f=await this.exchange(uid,C.WriteConfig,payload,0,undefined,undefined,onRequest);return {ack:ack(f.payload),session:f.session,sequence:f.sequence};},true,undefined,false,uid);
    }
    // Single transaction: schedule between target ACKs, never block target TTL with retries.
    async readLiveMotionProfile(uid:bigint){
        const configuration=await this.enqueue(async()=>configurationSnapshot((await this.exchange(uid,C.ReadConfig,new Bytes().u16(LIVE_MOTION_PROFILE_SCHEMA).get(),0)).payload),false,undefined,false,uid);
        if(configuration.schema!==LIVE_MOTION_PROFILE_SCHEMA)throw Error('配置 schema 不匹配');
        return {configuration,limits:motionProfile(configuration)};
    }
    writeLiveMotionProfile(uid:bigint,boot:bigint,generation:number,epoch:bigint,revision:number,values:readonly MotionLimit[],onRequest?:(request:ConfigurationRequest)=>void){
        const payload=configurationWrite(uid,boot,generation,epoch,LIVE_MOTION_PROFILE_SCHEMA,revision,motionLimits(values));
        return this.enqueue(async()=>{const f=await this.exchange(uid,C.WriteConfig,payload,0,undefined,undefined,onRequest);return {ack:ack(f.payload),session:f.session,sequence:f.sequence};},true,undefined,false,uid);
    }
    writeMotionProfile(uid:bigint,boot:bigint,generation:number,epoch:bigint,revision:number,values:readonly MotionLimit[],onRequest?:(request:ConfigurationRequest)=>void){
        const encoded=motionLimits(values);
        this.discardTargets(uid);
        return this.writeConfiguration(uid,boot,generation,epoch,MOTION_PROFILE_SCHEMA,revision,encoded,onRequest);
    }
    writeConfiguration(uid:bigint,boot:bigint,generation:number,epoch:bigint,schema:number,revision:number,values:Uint8Array,onRequest?:(request:ConfigurationRequest)=>void){
        const payload=configurationWrite(uid,boot,generation,epoch,schema,revision,values);
        return this.enqueue(async()=>{const f=await this.exchange(uid,C.WriteConfig,payload,2,undefined,undefined,onRequest);return {ack:ack(f.payload),session:f.session,sequence:f.sequence};},false,undefined,false,uid);
    }
    async inspect(uid:bigint):Promise<{info:Info;caps:Capabilities;status:Status}>{
        const identity=info(await this.request(uid,C.Hello));if(identity.uid!==uid)throw Error('INFO身份不匹配');const c:Partial<Capabilities>={resources:[],actions:[]};let cursor=0;
        for(let i=0;i<33;i++){const p=capsItem(await this.request(uid,C.Caps,new Bytes().u16(cursor).get()),c);if(p.next===0xffff){if(!c.maxTargetHz||!c.updateGapUs||!c.targetMinUs||!c.leaseMinUs||!c.resources?.length)throw Error('能力不完整');return {info:identity,caps:c as Capabilities,status:await this.getStatus(uid)};}if(p.next!==cursor+1)throw Error('能力游标');cursor=p.next;}throw Error('能力页数越界');
    }
    getStatus(uid:bigint,cursor=0){return this.request(uid,C.Status,new Bytes().u16(cursor).get()).then(status);}
    async switchSourceAndWait(uid:bigint,boot:bigint,generation:number,source:Source,targetUs:number,leaseUs:number,timeoutUs:number,epoch:bigint){
        await this.setSource(uid,boot,generation,source,targetUs,leaseUs,epoch);
        const end=this.clock.nowUs()+timeoutUs;
        while(this.clock.nowUs()<end){const s=await this.getStatus(uid);if(s.boot!==boot||s.fault)throw Error('切换失败或节点重启');
            if(s.source===source&&s.state===(source===Source.None?State.Safe:State.Active)&&(source!==Source.Mobocom||s.owner===this.session))return s;
            if(s.state!==State.Switching)throw Error('来源被其他操作改变');await this.clock.sleepUs(1000);
        }throw new ProtocolError(ErrorCode.Timeout,0);
    }
    async finish(uid:bigint,boot:bigint,generation:number,values:Target[],timeoutUs:number,epoch:bigint){
        let failure:unknown;
        try{const accepted=await this.writeTargets(uid,boot,generation,values,epoch),end=this.clock.nowUs()+timeoutUs;let applied=false;
            while(this.clock.nowUs()<end){const s=await this.getStatus(uid);if(s.boot!==boot||s.epoch!==epoch||s.generation!==generation||s.owner!==this.session||s.state!==State.Active||s.fault)throw Error('终目标等待期间控制失效');
                if(s.applied>=accepted.accepted){applied=true;break;}await this.clock.sleepUs(1000);
            }if(!applied)throw new ProtocolError(ErrorCode.Timeout,0);
        }catch(e){failure=e;}
        try{const stopped=await this.stop(uid);if(failure)throw failure;return stopped;}catch(e){if(failure&&e!==failure)throw new AggregateError([failure,e],'终目标与停止均失败');throw e;}
    }
    async setSource(uid:bigint,boot:bigint,generation:number,source:Source,targetUs:number,leaseUs:number,epoch:bigint){
        // Targets captured before a source switch must not be sent afterwards.
        this.cancelQueuedTargets(uid);
        const dropped=this.queue.filter(j=>j.uid===uid&&j.motion);this.queue=this.queue.filter(j=>j.uid!==uid||!j.motion);for(const j of dropped)j.reject(new SupersededError());
        const payload=new Bytes().raw(guard(boot,generation,epoch)).u8(source).u32(targetUs).u32(leaseUs).get();
        return this.enqueue(async()=>ack((await this.exchange(uid,C.SetSource,payload)).payload),true,undefined,false,uid);
    }
    heartbeat(uid:bigint,boot:bigint,generation:number,epoch:bigint){return this.enqueue(async()=>ack((await this.exchange(uid,C.Heartbeat,guard(boot,generation,epoch))).payload),true,undefined,false,uid);}
    writeTargets(uid:bigint,boot:bigint,generation:number,values:Target[],epoch:bigint):Promise<Ack>{
        // Validate the individual batch before merging unsent partial updates.
        if(!values.length||values.length>16)return Promise.reject(Error('目标数量'));
        targets(boot,generation,values,epoch);
        const prior=this.pendingTargets.get(uid),pending={boot,generation,epoch,values:prior&&prior.boot===boot&&prior.epoch===epoch&&prior.generation===generation?new Map(prior.values):new Map<string,Target>()};
        const seen=new Set<string>();for(const v of values){const key=`${v.endpoint}/${v.resource}`;if(seen.has(key))return Promise.reject(Error('重复目标资源'));seen.add(key);pending.values.set(key,{...v});}
        if(pending.values.size>16)return Promise.reject(Error('目标数量'));
        const version=(this.targetVersions.get(uid)??0)+1;
        this.pendingTargets.set(uid,pending);this.targetVersions.set(uid,version);
        return this.enqueue(async()=>{
            // 排队时允许调度器原位更新最新值；一旦发出，exchange固定字节重试。
            const latest=this.pendingTargets.get(uid)??pending,currentVersion=this.targetVersions.get(uid)??version;
            this.pendingTargets.delete(uid);
            return ack((await this.exchange(uid,C.WriteTargets,targets(latest.boot,latest.generation,[...latest.values.values()],latest.epoch),2,uid,currentVersion)).payload);
        },true,uid);
    }
    refreshQueuedTargets(uid:bigint,boot:bigint,generation:number,values:Target[],epoch:bigint){
        // 不重新排队，避免快速曲线不断取消同一节点的排队机会；不改已发帧。
        if(!values.length||values.length>16)throw Error('目标数量');targets(boot,generation,values,epoch);
        const seen=new Set<string>();for(const v of values){const key=`${v.endpoint}/${v.resource}`;if(seen.has(key))throw Error('重复目标资源');seen.add(key);}
        const pending=this.pendingTargets.get(uid);
        if(pending&&(pending.boot!==boot||pending.epoch!==epoch||pending.generation!==generation))throw Error('排队目标身份不匹配');
        const updated=new Map(pending?.values);
        for(const v of values)updated.set(`${v.endpoint}/${v.resource}`,{...v});
        if(updated.size>16)throw Error('目标数量');
        this.targetVersions.set(uid,(this.targetVersions.get(uid)??0)+1);
        if(pending)this.pendingTargets.set(uid,{boot,generation,epoch,values:updated});
        return !!pending;
    }
    executeAction(uid:bigint,boot:bigint,generation:number,opcode:number,parameters:Uint8Array,epoch:bigint){
        return this.enqueue(async()=>ack((await this.exchange(uid,C.ExecuteAction,new Bytes().raw(guard(boot,generation,epoch)).u16(opcode).raw(parameters).get())).payload),true,undefined,false,uid);
    }
    async actionStatus(uid:bigint,operation:bigint){const b=await this.request(uid,C.ActionStatus,new Bytes().u64(operation).get());if(b.length!==12)throw Error('动作结果布局');return {operation:view(b).getBigUint64(0,true),outcome:b[8] as OutcomeType,error:view(b).getUint16(9,true),progress:b[11]};}
    resetFault(uid:bigint,boot:bigint,generation:number,epoch:bigint){return this.request(uid,C.ResetFault,guard(boot,generation,epoch)).then(ack);}
    private cancelQueuedTargets(uid:bigint){this.pendingTargets.delete(uid);this.targetVersions.set(uid,(this.targetVersions.get(uid)??0)+1);const jobs=this.queue.filter(j=>j.key===uid);this.queue=this.queue.filter(j=>j.key!==uid);for(const j of jobs)j.reject(new SupersededError());}
    discardTargets(uid:bigint){this.cancelQueuedTargets(uid);}
    stop(uid:bigint,estop=false){
        if(this.closed)return Promise.reject(Error('连接已关闭'));
        if(uid===ALL_NODES)this.stopGeneration++;else this.stoppedUids.set(uid,(this.stoppedUids.get(uid)??0)+1);
        if(uid===ALL_NODES)this.pendingTargets.clear();else this.pendingTargets.delete(uid);
        const queued=this.queue.filter(j=>uid===ALL_NODES||j.uid===uid);this.queue=this.queue.filter(j=>uid!==ALL_NODES&&j.uid!==uid);for(const j of queued)j.reject(Error('停止取消排队任务'));
        if(uid===ALL_NODES){
            const raw=encode({kind:Kind.Request,srcUid:0n,dstUid:0n,sequence:this.next(),command:estop?C.Estop:C.Stop,session:this.session,payload:new Uint8Array()});
            return this.sendBounded(raw,null).then(()=>null);
        }
        return this.enqueue(async()=>{
            if(uid!==ALL_NODES){
                const query=async()=>status((await this.exchange(uid,C.Status,new Bytes().u16(0).get())).payload);
                let s=this.safetyCredentials.get(uid)??await query();
                try{return ack((await this.exchange(uid,estop?C.Estop:C.Stop,guard(s.boot,s.generation,s.epoch),0)).payload);}
                catch(e){if(!(e instanceof ProtocolError)||![ErrorCode.Generation,ErrorCode.Epoch,ErrorCode.Boot].includes(e.code))throw e;s=await query();return ack((await this.exchange(uid,estop?C.Estop:C.Stop,guard(s.boot,s.generation,s.epoch),0)).payload);}
            }
            return null;
        },false,undefined,true,uid);
    }
    private async probeIdentity(uid:bigint,endpoint:Endpoint):Promise<Info|null>{
        // A separate read-only session cannot overtake the node's current-session
        // sequence/cache while its old control transaction is being cancelled.
        const sequence=this.next(),random=new Uint32Array(2);globalThis.crypto.getRandomValues(random);
        const candidate=(BigInt(random[0])<<32n)|BigInt(random[1]);
        const session=candidate&&candidate!==this.session?candidate:(this.session^0x8000000000000000n)||1n;
        let timer:ReturnType<typeof setTimeout>|undefined,resolve!:(identity:Info|null)=>void,reject!:(error:unknown)=>void;
        const response=new Promise<Info|null>((a,b)=>{resolve=a;reject=b;});void response.catch(()=>{});
        this.pending.set(sequence,{reject,receive:(f,from)=>{
            if(f.kind!==Kind.Response||f.session!==session||f.sequence!==sequence||f.command!==C.Info||f.srcUid!==uid||f.dstUid!==0n||!from||!sameEndpoint(from,endpoint))return;
            try{const identity=info(f.payload);if(identity.uid===uid&&identity.boot&&identity.epoch)resolve(identity);}catch{}
        }});
        timer=setTimeout(()=>resolve(null),this.transactionTimeoutMs);
        try{
            return await Promise.race([response,this.wire.send(encode({kind:Kind.Request,srcUid:0n,dstUid:uid,sequence,command:C.Info,session,payload:new Uint8Array()}),endpoint).then(()=>response)]);
        }catch(error){if(this.closed)throw error;return null;}
        finally{clearTimeout(timer);this.pending.delete(sequence);}
    }
    private async scanExchange(required:bigint[],rounds:number):Promise<DiscoveryReport>{
        if(!Number.isInteger(rounds)||rounds<1||rounds>32)throw Error('发现轮数');
        if(new Set(required).size!==required.length||required.some(uid=>typeof uid!=='bigint'||uid<=0n||uid>0xffffffffffffffffn))throw Error('UID名册');
        const found=new Map<bigint,DiscoveredDevice>(),stopGeneration=this.stopGeneration;
        const candidates=new Map<bigint,{device:DiscoveredDevice;endpoint:Endpoint}[]>(),reconnected:bigint[]=[],unconfirmed:bigint[]=[];
        const active=()=>{if(this.closed)throw Error('连接已关闭');if(stopGeneration!==this.stopGeneration)throw new SupersededError();};
        for(let i=0;i<rounds;i++){
            const sequence=this.next();
            let cancel!:(e:unknown)=>void;
            const cancelled=new Promise<never>((_,reject)=>{cancel=reject;});void cancelled.catch(()=>{});
            this.pending.set(sequence,{reject:cancel,receive:(f,from)=>{
                if(f.kind!==Kind.Response||f.dstUid!==0n||f.session!==this.session||f.command!==C.Discover||!f.srcUid||f.payload.length!==26)return;
                const device={uid:f.srcUid,...discovery(f.payload)};
                if(!device.boot||!device.epoch||!device.generation)return;
                const previous=this.endpoints.get(device.uid),endpoint=from??previous;
                if(!endpoint)return;
                try{validateEndpoint({uid:device.uid,...endpoint});}catch{return;}
                const entries=candidates.get(device.uid)??[],old=entries.find(e=>sameEndpoint(e.endpoint,endpoint));
                const known=this.identities.get(device.uid);
                const changed=previous&&!sameEndpoint(previous,endpoint)||known&&(known.boot!==device.boot||known.type!==device.type||known.firmware!==device.firmware)||
                    this.safetyCredentials.has(device.uid)&&this.safetyCredentials.get(device.uid)!.boot!==device.boot||
                    entries.some(e=>!sameEndpoint(e.endpoint,endpoint)||e.device.boot!==device.boot);
                if(changed&&!this.reconnecting.has(device.uid))this.invalidateConnection(device.uid);
                if(old)old.device=device;else entries.push({device,endpoint:{...endpoint}});
                candidates.set(device.uid,entries);
            }});
            try{
                await Promise.race([this.sendBounded(encode({kind:Kind.Request,srcUid:0n,dstUid:0n,sequence,command:C.Discover,session:this.session,payload:new Uint8Array()}),null),cancelled]);
                const end=this.clock.nowUs()+DiscoveryWindowUs;
                while(this.clock.nowUs()<end)await Promise.race([this.clock.sleepUs(end-this.clock.nowUs()),cancelled]);
            }finally{this.pending.delete(sequence);}
            active();
        }
        for(const [uid,entries] of candidates){
            active();
            if(this.reconnecting.has(uid)||this.conflicts.has(uid)||entries.length>1){
                const addresses=[...entries.map(e=>e.endpoint),...(this.ambiguousEndpoints.get(uid)??[])];
                const previous=this.endpoints.get(uid);if(previous)addresses.push(previous);
                const unique=addresses.filter((e,i)=>addresses.findIndex(other=>sameEndpoint(e,other))===i);
                const live=(await Promise.all(unique.map(async endpoint=>({endpoint,identity:await this.probeIdentity(uid,endpoint)})))).filter(e=>e.identity!==null);
                active();
                if(live.length>1){this.conflicts.add(uid);this.ambiguousEndpoints.set(uid,unique);continue;}
                const current=live[0],entry=current&&entries.find(e=>sameEndpoint(e.endpoint,current.endpoint));
                if(!current||!entry||current.identity!.boot!==entry.device.boot||current.identity!.type!==entry.device.type||current.identity!.firmware!==entry.device.firmware){
                    unconfirmed.push(uid);continue;
                }
                this.endpoints.set(uid,{...current.endpoint});this.conflicts.delete(uid);this.ambiguousEndpoints.delete(uid);
                this.reconnecting.delete(uid);reconnected.push(uid);
            }else this.endpoints.set(uid,{...entries[0].endpoint});
            const endpoint=this.endpoints.get(uid)!,device=entries.find(e=>sameEndpoint(e.endpoint,endpoint))!.device;
            this.identities.set(uid,device);found.set(uid,device);
        }
        const devices=[...found.values()].sort((a,b)=>a.uid<b.uid?-1:a.uid>b.uid?1:0);
        return {devices,conflicts:[...this.conflicts],missing:required.filter(uid=>!found.has(uid)),reconnected,unconfirmed};
    }
    // 保留冲突设备的UID，供界面诊断；不会对冲突地址发普通单播。
    async scan(required:bigint[]=[],rounds=2):Promise<DiscoveryReport>{
        if(this.closed)throw Error('连接已关闭');
        if(this.renewing||this.scanning)throw Error('会话轮换或发现正在进行');
        this.scanning=true;
        try{return await this.scanExchange(required,rounds);}
        finally{this.scanning=false;}
    }
    async discover(required:bigint[]=[],rounds=2){
        const result=await this.scan(required,rounds);
        if(result.conflicts.length)throw new ProtocolError(ErrorCode.Address,0);
        if(result.missing.length)throw Error('所需UID名册不完整');return result.devices;
    }
    async reconnect(uid:bigint,rounds=2){
        if(this.closed)throw Error('连接已关闭');
        if(this.renewing||this.scanning)throw Error('会话轮换或发现正在进行');
        if(typeof uid!=='bigint'||uid<=0n||uid>0xffffffffffffffffn)throw Error('UID名册');
        if(!Number.isInteger(rounds)||rounds<1||rounds>32)throw Error('发现轮数');
        if(!this.reconnecting.has(uid))this.invalidateConnection(uid);
        const report=await this.scan([uid],rounds);
        if(report.conflicts.includes(uid))throw new ProtocolError(ErrorCode.Address,0);
        const device=report.devices.find(d=>d.uid===uid);
        if(!device)throw Error('未确认目标UID，请重新发现');
        const version=this.connectionVersion(uid);
        const snapshot=await this.getStatus(uid);
        if(this.connectionVersion(uid)!==version)throw new ConnectionChangedError(uid);
        if(snapshot.boot!==device.boot||snapshot.epoch!==device.epoch)throw Error('重连期间设备上下文变化，请重新发现');
        return {device,endpoint:this.getEndpoint(uid)!,status:snapshot,needsRearm:this.needsRearm(uid)};
    }
    async openSession(uid:bigint,boot:bigint,epoch:bigint,generation:number){
        const version=this.connectionVersion(uid);
        const result=ack(await this.request(uid,C.SessionOpen,new Bytes().u64(boot).u64(epoch).u32(generation).get()));
        if(this.connectionVersion(uid)!==version)throw new ConnectionChangedError(uid);
        if(result.boot!==boot||result.epoch!==epoch+1n||result.generation!==1||result.state!==State.Safe||result.source!==Source.None||result.fault)throw Error('建立会话未确认安全状态');
        this.rearmRequired.delete(uid);
        return result;
    }
    needsSessionRenewal(){return this.sequence>=0xfffff000;}
    async renewSession(){
        if(this.renewing||this.scanning)throw Error('会话轮换或发现正在进行');this.renewing=true;
        const devices=[...this.safetyCredentials.keys()].filter(uid=>!this.needsRearm(uid));
        try {
            for(const uid of devices)this.discardTargets(uid);
            const identities=[];
            for(const uid of devices){
                const before=await this.getStatus(uid);if(before.fault||before.state!==State.Safe&&(before.state!==State.Active||before.source!==Source.Mobocom||before.owner!==this.session))continue;
                await this.stop(uid);const end=this.clock.nowUs()+1000000;let ready:Status|null=null;
                while(this.clock.nowUs()<end){const s=await this.getStatus(uid);if(s.state===State.Safe&&!s.fault&&s.safeConfirmed){ready=s;break;}if(s.fault)throw Error('轮换期间发现真实故障，不自动清除');await this.clock.sleepUs(1000);}
                if(!ready)throw Error('轮换停止未确认');const identity=info(await this.request(uid,C.Info));identities.push({uid,identity,status:ready});
            }
            const previous=this.sessionValue;const bytes=new Uint32Array(2);globalThis.crypto.getRandomValues(bytes);this.sessionValue=(BigInt(bytes[0])<<32n)|BigInt(bytes[1]);if(!this.sessionValue||this.sessionValue===previous)this.sessionValue=previous===1n?2n:previous^1n;
            this.sequence=0;this.safetyCredentials.clear();this.pendingTargets.clear();
            for(const d of identities)await this.openSession(d.uid,d.status.boot,d.status.epoch,d.status.generation);
        }finally{this.renewing=false;}
    }
    async close(){this.closed=true;for(const j of this.queue.splice(0))j.reject(Error('连接已关闭'));for(const p of this.pending.values())p.reject(Error('连接已关闭'));this.pending.clear();await this.wire.close();}
}
type OutcomeType=1|2|3|4;
