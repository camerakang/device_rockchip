declare const require:(name:string)=>any;
import {ALL_NODES,Bytes,Command as C,decode,encode,ErrorCode,Frame,Kind,Source,State,view,capsItem,status,info,discovery,Capabilities} from './wire';
import {budget,Client,Clock,ProtocolError,SupersededError,ConnectionChangedError,Wire,ConfigurationRequest} from './client';
import {Endpoint} from './network';
import {HostTimeline,decodeTimeCode,encodeTimeCode} from './timecode';
import {RealtimeScheduler} from './scheduler';
import {DeviceType,isRegisteredDeviceType,isProductionDeviceType,isVirtualDeviceType} from './device_types';
import {configurationSnapshot,configurationWrite,radioChannels,dualRadioConfiguration} from './configuration';
import {motionLimits,decodeMotionLimits,motionProfile} from './motion_profile';
import {FocusControlSource,focusSource,focusControl,focusPositions,focusButton} from './focus_control';
const fs=require('fs'),assert=require('assert').strict;
const load=(name:string)=>JSON.parse(fs.readFileSync(`../${name}.json`,'utf8'));
const vectors=load('vectors');
const unhex=(s:string)=>Uint8Array.from(s.match(/../g)?.map(x=>parseInt(x,16))??[]);
const hex=(b:Uint8Array)=>Array.from(b).map(v=>v.toString(16).padStart(2,'0')).join('');
let checks=0;
function check(condition:boolean){assert(condition);checks++;}
async function spin(){for(let i=0;i<30;i++)await Promise.resolve();}
const endpoint=(uid:bigint):Endpoint=>({address:`device-${uid}`,port:47821});
const ackPayload=(f:Frame)=>new Bytes().u64((f.dstUid*10n)&0xffffffffffffffffn).u32(2).u8(State.Active).u8(Source.Mobocom).u16(0).u32(f.sequence).u32(f.sequence).u64(0n).u64(2n).get();
const identityPayload=(uid:bigint,boot=40n,epoch=1n)=>new Bytes().u64(uid).u16(DeviceType.Gimbal).u32(1).u64(boot).u8(0x22).u16(1).u64(epoch).get();
type NetworkDevice={uid:bigint;address:string;boot:bigint;epoch:bigint;state:State;advertise?:boolean};
function networkReplies(wire:TestWire,devices:NetworkDevice[]){
    wire.handler=f=>{
        const send=(d:NetworkDevice,payload:Uint8Array,kind:Kind)=>wire.client.feed(encode({...f,kind,srcUid:d.uid,dstUid:0n,payload}),{address:d.address,port:47821});
        if(f.command===C.Discover){
            for(const d of devices)if(d.advertise!==false)send(d,new Bytes().u16(DeviceType.Gimbal).u32(1).u64(d.boot).u64(d.epoch).u32(1).get(),Kind.Response);
            return;
        }
        const d=devices.find(d=>d.uid===f.dstUid&&d.address===wire.destinations.at(-1)?.address);if(!d)return;
        if(f.command===C.Info)send(d,identityPayload(d.uid,d.boot,d.epoch),Kind.Response);
        else if(f.command===C.Status){
            const payload=new Uint8Array(64),v=view(payload);v.setBigUint64(0,d.boot,true);v.setUint32(8,1,true);
            payload[16]=d.state;payload[17]=d.state===State.Active?Source.Mobocom:Source.None;
            if(d.state===State.Fault)v.setUint16(18,4,true);
            v.setBigUint64(20,d.state===State.Active?wire.client.session:0n,true);payload[52]=1;
            v.setUint16(53,0xffff,true);v.setBigUint64(56,d.epoch,true);send(d,payload,Kind.Response);
        }else if(f.command===C.SessionOpen){
            const accepted=d.state===State.Safe;if(accepted)d.epoch++;
            const snapshot=new Bytes().u64(d.boot).u32(1).u8(d.state).u8(d.state===State.Active?Source.Mobocom:Source.None).u16(0).u32(0).u32(0).u64(0n).u64(d.epoch).get();
            send(d,accepted?snapshot:new Bytes().u16(ErrorCode.State).u16(0).raw(snapshot).get(),accepted?Kind.Ack:Kind.Error);
        }else if(f.command===C.Stop||f.command===C.Estop){
            d.state=State.Safe;send(d,new Bytes().u64(d.boot).u32(1).u8(State.Safe).u8(Source.None).u16(0).u32(0).u32(0).u64(0n).u64(d.epoch).get(),Kind.Ack);
        }else send(d,ackPayload(f),Kind.Ack);
    };
}
class TestWire implements Wire {
    client!:Client;frames:Frame[]=[];raw:Uint8Array[]=[];destinations:(Endpoint|null)[]=[];time=0;
    handler?: (f:Frame)=>void;
    clock:Clock={nowUs:()=>this.time,sleepUs:async us=>{this.time+=us;}};
    async send(raw:Uint8Array,to:Endpoint|null){const f=decode(raw);this.frames.push(f);this.raw.push(raw.slice());this.destinations.push(to);this.handler?.(f);}
    async close(){}
    reply(f:Frame,payload:Uint8Array=ackPayload(f),kind=Kind.Ack,from=endpoint(f.dstUid)){
        this.client.feed(encode({...f,kind,srcUid:f.dstUid,dstUid:0n,payload}),from);
    }
}
function setup(uids=[1n,2n],clock=true){
    const wire=new TestWire(),client=new Client(wire,clock?wire.clock:undefined,100n);wire.client=client;
    for(const uid of uids){const e=endpoint(uid);client.setEndpoint(uid,e.address,e.port);}
    return {wire,client};
}
function codecTests(){
    const roundtrip=(raw:string)=>{const bytes=unhex(raw);check(bytes[2]===0x22);check(hex(encode(decode(bytes)))===raw);};
    for(const row of vectors.frames)roundtrip(row.hex);
    for(const row of vectors.trace)for(const key of ['request','reply'])if(row[key])roundtrip(row[key]);
    for(const name of ['configuration_vectors','motion_profile_vectors','live_motion_profile_vectors','focus_control_vectors']){
        for(const row of load(name))for(const key of ['request','reply'])roundtrip(row[key]);
    }
    for(const row of load('timeout_recovery_vectors'))for(const step of row.steps)for(const key of ['request','reply'])if(step[key])roundtrip(step[key]);
    for(const row of load('device_type_vectors').cases){
        roundtrip(row.request);roundtrip(row.reply);
        const f=decode(unhex(row.reply));
        check(isRegisteredDeviceType(row.type)&&isVirtualDeviceType(row.type)!==isProductionDeviceType(row.type));
        if(f.command===C.Caps){const caps:Partial<Capabilities>={};capsItem(f.payload,caps);check(caps.type===row.type);}
        else check((f.command===C.Info?info(f.payload):discovery(f.payload)).type===row.type);
    }
    check(DeviceType.Fan===0x9001&&DeviceType.Gimbal===0x9007);
    for(const n of [0,65535,-1,NaN,0x900a])check(!isRegisteredDeviceType(n));
    const first=unhex(vectors.frames[0].hex);
    for(const bad of [first.slice(1),first.slice(0,-1),new Bytes().raw(first).u8(0).get(),new Bytes().raw(first).raw(first).get()])assert.throws(()=>decode(bad));
    for(const offset of [2,3,4,21,27,35,38]){const bad=first.slice();bad[offset]^=0xff;assert.throws(()=>decode(bad));}
    const high=0x9007000000000004n,f=decode(first);
    check(decode(encode({...f,dstUid:high})).dstUid===high);
    assert.throws(()=>encode({...f,dstUid:Number(high) as unknown as bigint}));
    const tc=unhex(vectors.timecode_hex);check(hex(encodeTimeCode(decodeTimeCode(tc)))===vectors.timecode_hex);
    const timeline=new HostTimeline();check(timeline.observe({epoch:1n,sequence:0,timeUs:1000000n},0n,260n));check(timeline.start(1000n)===1001000n);
    check(timeline.now(40000n)===1040000n&&timeline.fallback);
    check(timeline.observe({epoch:2n,sequence:0,timeUs:500n},40000n,40260n));check(timeline.now(41000n)===1041000n);timeline.stop();check(timeline.start(41000n)===1500n);
    const values=[1,2].map(resource=>({endpoint:1,resource,vmax:25000,amax:50000}));
    assert.deepEqual(decodeMotionLimits(motionLimits(values)),values);
    for(const bad of [[],[values[0],values[0]],[{...values[0],vmax:0}],[{...values[0],amax:NaN}]])assert.throws(()=>motionLimits(bad));
    assert.throws(()=>radioChannels(16,0));assert.throws(()=>configurationSnapshot(new Uint8Array(22)));
    for(const row of load('focus_command_vectors'))check(hex(row.operation==='positions'?focusPositions(row.mask,row.positions):focusButton(row.action))===row.hex);
    assert.throws(()=>focusButton(2));assert.throws(()=>focusPositions(0,[0,0,0]));assert.throws(()=>focusSource(-1 as FocusControlSource));
    for(const row of load('motion_profile_vectors')){const f=decode(unhex(row.reply));if(f.kind===Kind.Response)check(motionProfile(configurationSnapshot(f.payload)).length===2);}
    for(const row of load('focus_control_vectors')){const f=decode(unhex(row.reply));if(f.kind===Kind.Response)check(focusControl(configurationSnapshot(f.payload)).outputIdle);}
    check(dualRadioConfiguration(configurationSnapshot(decode(unhex(load('configuration_vectors')[0].reply)).payload)).focusChannel===9);
    check(hex(configurationWrite(2n,20n,1,1n,1,0,radioChannels(0,2)))===hex(decode(unhex(load('configuration_vectors')[1].request)).payload));
}
async function concurrencyTests(){
    const a=0x9007000000000004n,b=a+1n,{wire,client}=setup([a,b]);
    const p=client.request(a,C.Info),q=client.request(b,C.Info),next=client.request(a,C.Info);
    check(wire.frames.length===2);
    wire.reply(wire.frames[1],Uint8Array.of(2),Kind.Response);
    check((await q)[0]===2);
    wire.reply(wire.frames[0],Uint8Array.of(1),Kind.Response);check((await p)[0]===1);await spin();
    check(wire.frames.length===3&&wire.frames[2].dstUid===a);
    wire.reply(wire.frames[2],Uint8Array.of(3),Kind.Response);check((await next)[0]===3);
    check(wire.destinations[0]?.address===endpoint(a).address&&wire.destinations[1]?.address===endpoint(b).address);
    await assert.rejects(client.request(999n,C.Info),ProtocolError);
    const pending=client.request(a,C.Info);let done=false;void pending.then(()=>{done=true;});
    const f=wire.frames.at(-1)!;
    wire.reply({...f,session:f.session+1n},Uint8Array.of(9),Kind.Response);
    wire.reply({...f,dstUid:b},Uint8Array.of(9),Kind.Response);
    wire.reply(f,Uint8Array.of(9),Kind.Response,endpoint(b));
    await spin();check(!done);
    wire.reply(f,Uint8Array.of(4),Kind.Response);check((await pending)[0]===4);
    await client.close();
}
async function queueTests(){
    const {wire,client}=setup();
    const target=(value:number,resource=1)=>client.writeTargets(1n,10n,2,[{endpoint:1,resource,value}],2n);
    const first=target(10),old=target(20).catch(e=>e),latest=target(30,2);
    check(await old instanceof SupersededError);
    wire.reply(wire.frames[0]);await first;await spin();
    check(wire.frames[1].payload[20]===2&&view(wire.frames[1].payload).getInt32(25,true)===20);
    wire.reply(wire.frames[1]);await latest;await spin();
    const flying=target(40),dropped=client.request(1n,C.Info).catch(e=>e),stop=client.stop(1n);
    check(await dropped instanceof Error);wire.reply(wire.frames[2]);await flying;await spin();
    check(wire.frames[3].command===C.Stop);wire.reply(wire.frames[3]);await stop;await spin();
    const active=target(50),globalStop=client.stop(ALL_NODES,true);
    check(wire.frames.at(-1)?.command===C.Estop&&wire.frames.at(-1)?.dstUid===0n&&wire.destinations.at(-1)===null);
    wire.reply(wire.frames[4]);await active;await globalStop;await client.close();
}
async function discoveryTests(){
    const {wire,client}=setup(),high=0x9007000000000004n;
    wire.handler=f=>{if(f.command===C.Discover)client.feed(encode({...f,kind:Kind.Response,srcUid:high,dstUid:0n,payload:new Bytes().u16(DeviceType.Gimbal).u32(1).u64(40n).u64(1n).u32(1).get()}),endpoint(high));};
    const first=client.request(1n,C.Info),scan=client.scan([high],2),last=client.request(2n,C.Info);
    check(wire.frames.length===3&&wire.frames[1].command===C.Discover&&wire.frames[2].dstUid===2n);
    wire.reply(wire.frames[0],new Uint8Array(),Kind.Response);await first;
    wire.reply(wire.frames[2],new Uint8Array(),Kind.Response);await last;
    const found=await scan;check(found.devices[0].uid===high&&!found.conflicts.length&&!found.missing.length&&wire.time>=200000);
    wire.handler=f=>wire.reply(f,new Bytes().u64(40n).u32(1).u8(State.Safe).u8(Source.None).u16(0).u32(0).u32(0).u64(0n).u64(2n).get());
    await client.openSession(high,40n,1n,1);
    check(wire.frames.at(-1)?.payload.length===20&&view(wire.frames.at(-1)!.payload).getBigUint64(0,true)===40n);
    assert.throws(()=>client.setEndpoint(high,'other-host',47821),ProtocolError);
    check(client.getEndpoint(high)?.address===endpoint(high).address);
    await assert.rejects(client.scan([],1.5));
    await client.close();
    const empty=setup();check((await empty.client.scan([1n],1)).missing[0]===1n);await empty.client.close();
    const conflict=setup([]);
    networkReplies(conflict.wire,[{uid:high,address:endpoint(high).address,boot:40n,epoch:1n,state:State.Safe},{uid:high,address:'duplicate',boot:40n,epoch:1n,state:State.Safe}]);
    check((await conflict.client.scan([],1)).conflicts[0]===high);await assert.rejects(conflict.client.discover([],1),ProtocolError);await conflict.client.close();
    // A discovery window must not hold up an active node's targets or heartbeat.
    const live=setup([high]);let release!:()=>void;
    live.wire.clock.sleepUs=us=>new Promise<void>(resolve=>{release=()=>{live.wire.time+=us;resolve();};});
    live.wire.handler=f=>{if(f.command!==C.Discover)live.wire.reply(f,new Bytes().u64(40n).u32(2).u8(State.Active).u8(Source.Mobocom).u16(0).u32(f.sequence).u32(f.sequence).u64(0n).u64(2n).get());};
    const background=live.client.scan([high],1);await spin();
    await assert.rejects(live.client.scan([],1),/发现正在进行/);
    await assert.rejects(live.client.renewSession(),/发现正在进行/);
    await live.client.writeTargets(high,40n,2,[{endpoint:1,resource:1,value:100}],2n);
    await live.client.heartbeat(high,40n,2,2n);
    check(live.wire.frames.map(f=>f.command).join(',')===[C.Discover,C.WriteTargets,C.Heartbeat].join(','));
    const activeRequest=vectors.frames.find((s:any)=>s.name==='discover_while_active').hex;
    const activeReply=decode(unhex(vectors.trace.find((s:any)=>s.request===activeRequest).reply));
    live.client.feed(encode({...activeReply,session:live.wire.frames[0].session,sequence:live.wire.frames[0].sequence}),endpoint(high));
    release();check((await background).devices[0].uid===high);await live.client.close();
    // The same 26B format is returned by SAFE, ACTIVE and FAULT; state is queried separately.
    for(const name of ['discover_multicast','discover_while_active','discover_while_fault']){
        const request=vectors.frames.find((s:any)=>s.name===name).hex;
        const row=vectors.trace.find((s:any)=>s.request===request),reply=decode(unhex(row.reply));
        const state=setup([]);
        state.wire.handler=query=>state.client.feed(encode({...reply,session:query.session,sequence:query.sequence}),endpoint(high));
        const result=await state.client.scan([high],1);
        check(result.devices[0].uid===high&&result.devices[0].generation===discovery(reply.payload).generation);
        await state.client.close();
    }
    const closing=setup([],false),waiting=closing.client.scan([],1);
    await spin();await closing.client.close();await assert.rejects(waiting,/连接已关闭/);
}
async function reconnectTests(){
    const uid=0x9007000000000004n,other=uid+1n;
    const device=(address:string,boot=40n,state=State.Active):NetworkDevice=>({uid,address,boot,epoch:1n,state});
    const {wire,client}=setup([uid,other]);
    // Leave an old motion request in flight and another queued before DHCP changes.
    const flying=client.writeTargets(uid,40n,1,[{endpoint:1,resource:1,value:100}],1n).catch(e=>e);
    const old=wire.frames[0],queued=client.writeTargets(uid,40n,1,[{endpoint:1,resource:1,value:200}],1n).catch(e=>e);
    const devices=[device('dhcp-new'),{...device(endpoint(other).address),uid:other}];networkReplies(wire,devices);
    const report=await client.scan([uid],1);
    check(report.reconnected.includes(uid)&&!report.conflicts.length&&!report.unconfirmed.length);
    check(await flying instanceof ConnectionChangedError&&await queued instanceof SupersededError);
    check(client.getEndpoint(uid)?.address==='dhcp-new'&&client.needsRearm(uid));
    check(wire.frames.filter(f=>f.command===C.WriteTargets).length===1);
    check(wire.frames.filter(f=>f.command===C.Info).every(f=>f.session!==client.session));
    // Read-only reconnect neither clears a running source nor grants renewed motion.
    const connected=await client.reconnect(uid,1);
    check(connected.device.uid===uid&&connected.status.state===State.Active&&connected.needsRearm);
    check(wire.frames.every(f=>[C.WriteTargets,C.Discover,C.Info,C.Status].includes(f.command)));
    const count=wire.frames.length;
    await assert.rejects(client.writeTargets(uid,40n,1,[{endpoint:1,resource:1,value:300}],1n),ConnectionChangedError);
    await assert.rejects(client.heartbeat(uid,40n,1,1n),ConnectionChangedError);
    await assert.rejects(client.executeAction(uid,40n,1,1,new Uint8Array(),1n),ConnectionChangedError);
    await assert.rejects(client.writeConfiguration(uid,40n,1,1n,1,0,radioChannels(0,2)),ConnectionChangedError);
    check(wire.frames.length===count);
    await client.heartbeat(other,40n,1,1n);check(wire.destinations.at(-1)?.address===endpoint(other).address);
    // Late ACKs from the old endpoint cannot reinstall credentials or unblock motion.
    wire.reply(old);check(client.needsRearm(uid));
    await assert.rejects(client.openSession(uid,40n,1n,1),ProtocolError);check(client.needsRearm(uid));
    await client.stop(uid);check(wire.frames.at(-1)?.command===C.Stop&&view(wire.frames.at(-1)!.payload).getBigUint64(12,true)===1n);
    await client.openSession(uid,40n,1n,1);check(!client.needsRearm(uid));
    await client.writeTargets(uid,40n,1,[{endpoint:1,resource:1,value:400}],2n);
    check(wire.destinations.at(-1)?.address==='dhcp-new');await client.close();

    // Cached old address still answers UID-unicast, despite missing multicast: ambiguous, not a move.
    const duplicate=setup([uid]),oldAddress=endpoint(uid).address;
    networkReplies(duplicate.wire,[device('new'),{...device(oldAddress),advertise:false}]);
    check((await duplicate.client.scan([uid],1)).conflicts.includes(uid));
    await assert.rejects(duplicate.client.getStatus(uid),ProtocolError);
    // Once the second live identity disappears, resolve the conflict without rebuilding Client.
    networkReplies(duplicate.wire,[device('new')]);
    const resolved=await duplicate.client.scan([uid],1);
    check(!resolved.conflicts.length&&resolved.reconnected.includes(uid)&&duplicate.client.needsRearm(uid));
    await duplicate.client.close();

    // DHCP may reuse the previous IP for a different UID; it is not a duplicate of our device.
    const reused=setup([uid]);networkReplies(reused.wire,[device('new'),{...device(oldAddress),uid:other,advertise:false}]);
    const reusedReport=await reused.client.scan([uid],1);
    check(!reusedReport.conflicts.length&&reusedReport.reconnected.includes(uid));await reused.client.close();

    // Same endpoint after reboot is also a fresh connection, not permission to reuse old targets.
    const reboot=setup([uid]);networkReplies(reboot.wire,[device(oldAddress,40n)]);
    await reboot.client.scan([uid],1);networkReplies(reboot.wire,[device(oldAddress,41n,State.Fault)]);
    const restarted=await reboot.client.reconnect(uid,1);
    check(restarted.device.boot===41n&&restarted.status.fault===4&&restarted.needsRearm);await reboot.client.close();

    // A move between rounds may leave two historical addresses in the collection,
    // but only the new one is reachable at verification time.
    const between=setup([uid]);let round=0;
    networkReplies(between.wire,[device('new')]);const responder=between.wire.handler!;
    between.wire.handler=f=>{
        if(f.command!==C.Discover){responder(f);return;}
        between.client.feed(encode({...f,kind:Kind.Response,srcUid:uid,dstUid:0n,payload:new Bytes().u16(DeviceType.Gimbal).u32(1).u64(40n).u64(1n).u32(1).get()}),{address:round++?'new':oldAddress,port:47821});
    };
    const transitioned=await between.client.scan([uid],2);
    check(!transitioned.conflicts.length&&transitioned.reconnected.includes(uid));await between.client.close();

    const missing=setup([uid]);
    await assert.rejects(missing.client.reconnect(uid,0),/发现轮数/);check(!missing.client.needsRearm(uid));
    await assert.rejects(missing.client.reconnect(uid,1),/未确认目标UID/);
    check(missing.client.needsRearm(uid));await missing.client.close();

    // An ACK already resolved in the same turn must still fail if discovery invalidates
    // the connection before exchange processes that ACK.
    const race=setup([uid]);const racing=race.client.writeTargets(uid,40n,1,[{endpoint:1,resource:1,value:100}],1n).catch(e=>e);
    race.wire.reply(race.wire.frames[0]);
    networkReplies(race.wire,[device('new')]);await race.client.scan([uid],1);
    check(await racing instanceof ConnectionChangedError&&race.client.needsRearm(uid));await race.client.close();

    const malformed=setup([uid]);networkReplies(malformed.wire,[device('new',40n,State.Safe)]);
    await malformed.client.reconnect(uid,1);
    malformed.wire.handler=f=>malformed.wire.reply(f,new Bytes().u64(40n).u32(1).u8(State.Safe).u8(Source.None).u16(0).u32(0).u32(0).u64(0n).u64(1n).get(),Kind.Ack,{address:'new',port:47821});
    await assert.rejects(malformed.client.openSession(uid,40n,1n,1),/安全状态/);
    check(malformed.client.needsRearm(uid));await malformed.client.close();

    const changing=setup([uid]),changingDevice=device('new',40n,State.Safe);
    networkReplies(changing.wire,[changingDevice]);const changingReply=changing.wire.handler!;
    changing.wire.handler=f=>{if(f.command===C.Status)changingDevice.boot++;changingReply(f);};
    await assert.rejects(changing.client.reconnect(uid,1),/上下文变化/);
    check(changing.client.needsRearm(uid));await changing.client.close();
    const renewal=setup([uid]);networkReplies(renewal.wire,[device('new',40n,State.Safe)]);
    await renewal.client.reconnect(uid,1);const beforeRenewal=renewal.wire.frames.length;
    await renewal.client.renewSession();
    check(renewal.wire.frames.length===beforeRenewal&&renewal.client.needsRearm(uid));await renewal.client.close();

    // A forged DISCOVER or malformed/mismatched INFO cannot redirect control traffic.
    for(const bad of ['uid','source','layout','boot','session','no-source']){
        const invalid=setup([uid]);
        invalid.wire.handler=f=>{
            if(f.command===C.Discover)invalid.client.feed(encode({...f,kind:Kind.Response,srcUid:uid,dstUid:0n,payload:new Bytes().u16(DeviceType.Gimbal).u32(1).u64(40n).u64(1n).u32(1).get()}),{address:'new',port:47821});
            else if(f.command===C.Info&&invalid.wire.destinations.at(-1)?.address==='new'){
                invalid.client.feed(encode({...f,kind:Kind.Response,srcUid:uid,dstUid:0n,session:bad==='session'?f.session+1n:f.session,payload:bad==='layout'?new Uint8Array(2):identityPayload(bad==='uid'?other:uid,bad==='boot'?41n:40n)}),bad==='no-source'?undefined:{address:bad==='source'?'wrong':'new',port:47821});
            }
        };
        const rejected=await invalid.client.scan([uid],1);
        check(rejected.unconfirmed.includes(uid)&&!rejected.conflicts.length&&rejected.missing.includes(uid));
        check(invalid.client.getEndpoint(uid)?.address===oldAddress&&invalid.client.needsRearm(uid));await invalid.client.close();
    }
    // Closing or an all-node stop during unicast verification must not commit a new mapping.
    for(const cancel of ['close','stop']){
        const interrupted=setup([uid]);let verifying!:()=>void;
        const probing=new Promise<void>(resolve=>{verifying=resolve;});
        interrupted.wire.handler=f=>{
            if(f.command===C.Discover)interrupted.client.feed(encode({...f,kind:Kind.Response,srcUid:uid,dstUid:0n,payload:new Bytes().u16(DeviceType.Gimbal).u32(1).u64(40n).u64(1n).u32(1).get()}),{address:'new',port:47821});
            else if(f.command===C.Info)verifying();
        };
        const pending=interrupted.client.reconnect(uid,1).catch(e=>e);
        await probing;
        if(cancel==='close')await interrupted.client.close();else await interrupted.client.stop(0n,true);
        check(await pending instanceof Error&&interrupted.client.getEndpoint(uid)?.address===oldAddress);
        await interrupted.client.close();
    }
}
async function deadlineTests(){
    const {wire,client}=setup();
    await assert.rejects(client.request(1n,C.Info),/超时/);check(wire.frames.length===3&&wire.raw.every(raw=>hex(raw)===hex(wire.raw[0])));
    const pending=client.request(1n,C.Info).catch(e=>e),queued=client.request(1n,C.Info).catch(e=>e);
    await client.close();check(await pending instanceof Error&&await queued instanceof Error);
    const blocked=setup();blocked.wire.send=()=>new Promise(()=>{});
    await assert.rejects(blocked.client.request(1n,C.Info),/超时/);await blocked.client.close();
    const failed=setup();let sends=0;failed.wire.send=async()=>{sends++;throw Error('network down');};
    await assert.rejects(failed.client.request(1n,C.Info),/network down/);check(sends===3);await failed.client.close();
    const errors=setup();errors.wire.handler=f=>errors.wire.reply(f,new Bytes().u16(ErrorCode.Generation).u16(0).raw(ackPayload(f)).get(),Kind.Error);
    await assert.rejects(errors.client.heartbeat(1n,10n,1,2n),(e:ProtocolError)=>e.code===ErrorCode.Generation&&e.snapshot?.generation===2);
    check(errors.wire.frames.length===1);await errors.client.close();
    for(const n of [0,29,2001,NaN,30.5])assert.throws(()=>new Client(wire,undefined,100n,{transactionTimeoutMs:n}));
}
async function configurationTests(){
    const {wire,client}=setup(),values=[1,2].map(resource=>({endpoint:1,resource,vmax:25000,amax:50000}));
    let rows=load('motion_profile_vectors');
    wire.handler=f=>{const response=decode(unhex(rows[f.command===C.ReadConfig?0:1].reply));wire.reply(f,response.payload,response.kind);};
    check((await client.readMotionProfile(2n)).limits[0].vmax===50000);
    let receipt:ConfigurationRequest|undefined;
    await client.writeMotionProfile(2n,20n,1,1n,0,values,r=>{receipt=r;});
    check(receipt?.uid===2n&&receipt.sequence===2);
    rows=load('focus_control_vectors');
    check((await client.readFocusControl(2n)).focus.source===FocusControlSource.Wireless);
    await client.writeFocusSource(2n,20n,1,1n,1,FocusControlSource.Wired);
    rows=load('configuration_vectors');
    check((await client.readConfiguration(2n,1)).revision===0);
    await client.writeConfiguration(2n,20n,1,1n,1,0,radioChannels(0,2));
    wire.handler=undefined;
    const before=wire.frames.length;
    await assert.rejects(client.writeLiveMotionProfile(2n,20n,2,2n,0,values));check(wire.frames.length===before+1);
    await assert.rejects(client.writeFocusCommand(2n,20n,1,1n,1,focusButton(0)));check(wire.frames.length===before+2);
    await assert.rejects(client.writeMotionProfile(2n,20n,1,1n,0,values));
    check(wire.frames.length===before+5&&hex(wire.raw.at(-1)!)===hex(wire.raw.at(-3)!));
    await client.close();
}
function budgetTests(){
    const item={uid:1n,hz:100,maxTargetHz:100,resources:3,statusHz:1,statusPages:1,heartbeatHz:0};
    check(Math.abs(budget([item,{...item,uid:2n}]).utilization-0.00478592)<1e-10);
    check(budget(Array.from({length:64},(_,i)=>({...item,uid:BigInt(i+1)}))).utilization<0.8);
    assert.throws(()=>budget([item,item]),ProtocolError);
    assert.throws(()=>budget([{...item,hz:101}]),ProtocolError);
    assert.throws(()=>budget(Array.from({length:1000},(_,i)=>({...item,uid:BigInt(i+1)}))),ProtocolError);
}
async function schedulerTests(){
    const {wire,client}=setup(),caps:Partial<Capabilities>={resources:[],actions:[]};
    for(const step of vectors.trace)if(step.node===1&&step.reply){const f=decode(unhex(step.reply));if(f.command===C.Caps)capsItem(f.payload,caps);}
    const owned=status(decode(unhex(vectors.trace.find((s:any)=>s.node===1&&s.at_us===1040).reply)).payload);
    const scheduler=new RealtimeScheduler(client);
    scheduler.configure([{uid:1n,caps:caps as Capabilities,status:{...owned,activeMask:0},hz:100,statusHz:0,heartbeatHz:0,targetTtlUs:100000,leaseTtlUs:300000,resources:[{endpoint:1,resource:1}]}],0);
    assert.throws(()=>scheduler.setTargets(1n,[]));
    scheduler.setTargets(1n,[{endpoint:1,resource:1,value:10}]);scheduler.start();
    wire.handler=f=>wire.reply(f);
    for(const at of [0,9900,19800,29700,39600,49500]){scheduler.tick(at);await spin();}
    check(wire.frames.length===5);scheduler.tick(200000);await spin();check(wire.frames.length===6);scheduler.tick(200001);check(wire.frames.length===6);
    await scheduler.stop();await client.close();
    const moved=setup(),playback=new RealtimeScheduler(moved.client),failures:bigint[]=[];
    playback.onError=uid=>failures.push(uid);
    playback.configure([1n,2n].map(uid=>({uid,caps:caps as Capabilities,status:{...owned,boot:uid*10n,activeMask:0},hz:100,statusHz:0,heartbeatHz:0,targetTtlUs:100000,leaseTtlUs:300000,resources:[{endpoint:1,resource:1}]})),0);
    playback.setTargets(1n,[{endpoint:1,resource:1,value:10}]);playback.setTargets(2n,[{endpoint:1,resource:1,value:20}]);playback.start();
    networkReplies(moved.wire,[{uid:1n,address:'dhcp-1',boot:10n,epoch:1n,state:State.Safe},{uid:2n,address:endpoint(2n).address,boot:20n,epoch:1n,state:State.Active}]);
    await moved.client.scan([1n],1);playback.tick(0);await spin();
    check(failures.length===1&&failures[0]===1n);
    check(moved.wire.frames.filter(f=>f.command===C.WriteTargets).every(f=>f.dstUid===2n));
    await moved.client.openSession(1n,10n,1n,1);
    playback.tick(10000);await spin();
    check(moved.wire.frames.filter(f=>f.command===C.WriteTargets).length===2);
    assert.throws(()=>playback.setTargets(1n,[{endpoint:1,resource:1,value:30}]),ConnectionChangedError);
    assert.throws(()=>playback.start());
    await assert.rejects(playback.finish(new Map([[1n,[{endpoint:1,resource:1,value:0}]],[2n,[{endpoint:1,resource:1,value:0}]]]),100000),ConnectionChangedError);
    await moved.client.close();
}
async function lifecycleTests(){
    const uid=0x9007000000000004n,{wire,client}=setup([uid]);let epoch=1n,applied=0;
    wire.handler=f=>{
        let payload:Uint8Array,kind=Kind.Ack;
        if(f.command===C.Status){
            payload=new Uint8Array(64);const v=view(payload);
            v.setBigUint64(0,10n,true);v.setUint32(8,1,true);payload[52]=1;v.setUint16(53,0xffff,true);v.setBigUint64(56,epoch,true);
            if(applied){payload[16]=State.Active;payload[17]=Source.Mobocom;v.setBigUint64(20,client.session,true);v.setUint32(28,applied,true);v.setUint32(32,applied,true);}
            kind=Kind.Response;
        }else if(f.command===C.Info){
            payload=new Bytes().u64(uid).u16(DeviceType.Gimbal).u32(1).u64(10n).u8(0x22).u16(1).u64(epoch).get();kind=Kind.Response;
        }else{
            if(f.command===C.SessionOpen){check(f.payload.length===20);epoch++;}
            if(f.command===C.WriteTargets)applied=f.sequence;
            if(f.command===C.Stop){check(view(f.payload).getBigUint64(0,true)===10n&&view(f.payload).getBigUint64(12,true)===epoch);applied=0;}
            payload=new Bytes().u64(10n).u32(1).u8(State.Safe).u8(Source.None).u16(0).u32(applied).u32(applied).u64(0n).u64(epoch).get();
        }
        wire.reply(f,payload,kind);
    };
    await client.getStatus(uid);
    (client as unknown as {sequence:number}).sequence=0xfffff000;
    check(client.needsSessionRenewal());await client.renewSession();
    check(!client.needsSessionRenewal()&&client.session!==100n&&epoch===2n);
    const finished=await client.finish(uid,10n,1,[{endpoint:1,resource:1,value:0}],100000,epoch);
    check(!!finished&&wire.frames.at(-1)?.command===C.Stop);
    await client.close();

    // A stop on UID 1 must not suppress UID 2's independent retry.
    const separate=setup();
    separate.wire.handler=f=>{if(f.dstUid===1n)separate.wire.reply(f);else if(separate.wire.frames.filter(x=>x.dstUid===2n).length===2)separate.wire.reply(f,Uint8Array.of(2),Kind.Response);};
    await separate.client.heartbeat(1n,10n,2,2n);
    const retry=separate.client.request(2n,C.Info);
    await separate.client.stop(1n);check((await retry)[0]===2);
    await separate.client.close();

    const scan=setup([],false);
    const scanning=scan.client.scan([],2).catch(e=>e);await spin();
    await scan.client.stop(0n,true);
    check(scan.wire.frames[1].command===C.Estop);
    check(await scanning instanceof SupersededError&&scan.wire.frames.length===2);await scan.client.close();
}
async function main(){
    codecTests();budgetTests();await concurrencyTests();await queueTests();await discoveryTests();await reconnectTests();await deadlineTests();await configurationTests();await schedulerTests();await lifecycleTests();
    console.log(`PASS TypeScript: ${checks} vector/behavior checks; UDP framing, bigint UID, concurrent/out-of-order replies, queue barriers, stop, discovery, retries, configuration, scheduler`);
}
void main().catch(e=>{console.error(e);require('process').exitCode=1;});
