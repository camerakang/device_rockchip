export const VERSION=0x22, MAX_PAYLOAD=240, HEADER_SIZE=37, MAX_FRAME=HEADER_SIZE+MAX_PAYLOAD+2;
// dst_uid=0 addresses all nodes through the fixed multicast group. UIDs are u64: always bigint, never number.
export const ALL_NODES=0n;
export enum Kind {Request=1,Response=2,Ack=3,Error=4}
export enum Command {Hello=1,Discover=2,Info=4,Caps=5,SessionOpen=6,SetSource=0x10,Heartbeat=0x11,WriteTargets=0x20,ExecuteAction=0x21,ActionStatus=0x22,Status=0x30,Stop=0x40,Estop=0x41,ResetFault=0x42,ReadConfig=0x50,WriteConfig=0x51}
export enum ErrorCode {Ok=0,Malformed=1,Unsupported=2,State=3,Boot=4,Generation=5,Owner=6,Sequence=7,Conflict=8,Range=9,Group=10,Busy=11,Space=12,Safety=13,NotFound=14,Address=15,Capability=16,Bandwidth=17,Timeout=18,Driver=19,Epoch=20,Session=21}
export enum Source {None=0,Remote=1,Mobocom=2}
export enum State {Safe=0,Switching=1,Active=2,Stopping=3,Fault=4}
export enum Outcome {Running=1,Success=2,Failed=3,Cancelled=4}
export enum Fault {None=0,SourceTimeout=1,TargetExpired=2,ActionTimeout=3,Driver=4,StopTimeout=5,SwitchTimeout=6,Estop=7,LocalClock=8,OutputGap=9,GenerationExhausted=10,LimitTrip=11}
// One UDP datagram carries exactly one Frame.
export type Frame={kind:Kind;srcUid:bigint;dstUid:bigint;sequence:number;command:Command;session:bigint;payload:Uint8Array};
export const view=(b:Uint8Array)=>new DataView(b.buffer,b.byteOffset,b.byteLength);
function integer(n:number,min:number,max:number){if(!Number.isInteger(n)||n<min||n>max)throw Error('整数范围');return n;}
export class Bytes {
    private b:number[]=[];
    u8(n:number){this.b.push(integer(n,0,255));return this;}
    u16(n:number){integer(n,0,65535);return this.u8(n&255).u8(n>>>8);}
    u32(n:number){integer(n,0,0xffffffff);return this.u16(n&65535).u16(n>>>16);}
    i32(n:number){integer(n,-0x80000000,0x7fffffff);return this.u32(n>>>0);}
    u64(n:bigint){if(n<0n||n>0xffffffffffffffffn)throw Error('u64范围');return this.u32(Number(n&0xffffffffn)).u32(Number(n>>32n));}
    raw(b:Uint8Array){this.b.push(...b);return this;}
    get(){return Uint8Array.from(this.b);}
}
export function crc16(b:Uint8Array){let c=0xffff;for(const v of b){c^=v<<8;for(let i=0;i<8;i++)c=((c<<1)^((c&0x8000)?0x1021:0))&65535;}return c;}
export function encode(f:Frame){
    if(f.kind<1||f.kind>4||!f.sequence||!f.session||f.payload.length>MAX_PAYLOAD||typeof f.srcUid!=='bigint'||typeof f.dstUid!=='bigint')throw Error('报文头');
    const b=new Bytes().u8(0xd5).u8(0x7d).u8(VERSION).u8(f.kind).u8(0).u64(f.srcUid).u64(f.dstUid).u32(f.sequence).u16(f.command).u64(f.session).u16(f.payload.length).raw(f.payload).get();
    return new Bytes().raw(b).u16(crc16(b.subarray(2))).get();
}
/** Decode one complete datagram; trailing, missing or concatenated bytes are invalid. */
export function decode(b:Uint8Array):Frame{
    if(b.length<HEADER_SIZE+2)throw Error('短帧');const v=view(b),n=v.getUint16(35,true);
    if(b[0]!==0xd5||b[1]!==0x7d||b[2]!==VERSION||b[3]<1||b[3]>4||b[4]||!v.getUint32(21,true)||!v.getBigUint64(27,true)||n>MAX_PAYLOAD||b.length!==HEADER_SIZE+2+n||crc16(b.subarray(2,b.length-2))!==v.getUint16(b.length-2,true))throw Error('坏帧');
    return {kind:b[3],srcUid:v.getBigUint64(5,true),dstUid:v.getBigUint64(13,true),sequence:v.getUint32(21,true),command:v.getUint16(25,true),session:v.getBigUint64(27,true),payload:b.slice(37,-2)};
}
export function guard(boot:bigint,generation:number,epoch:bigint){return new Bytes().u64(boot).u32(generation).u64(epoch).get();}
export type Target={endpoint:number;resource:number;value:number};
export function targets(boot:bigint,generation:number,values:Target[],epoch:bigint){const b=new Bytes().raw(guard(boot,generation,epoch)).u8(values.length);for(const t of values)b.u16(t.endpoint).u16(t.resource).i32(t.value);return b.get();}
export type Ack={epoch:bigint;boot:bigint;generation:number;state:State;source:Source;fault:number;accepted:number;applied:number;operation:bigint};
export function ack(b:Uint8Array):Ack{if(b.length!==40)throw Error('ACK长度');const v=view(b);return {epoch:v.getBigUint64(32,true),boot:v.getBigUint64(0,true),generation:v.getUint32(8,true),state:b[12],source:b[13],fault:v.getUint16(14,true),accepted:v.getUint32(16,true),applied:v.getUint32(20,true),operation:v.getBigUint64(24,true)};}
// HELLO/INFO: uid,type,firmware,boot,wire_version,profile_version,epoch (33 bytes).
export type Info={uid:bigint;type:number;firmware:number;boot:bigint;profileVersion:number;epoch:bigint};
export function info(b:Uint8Array):Info{if(b.length!==33||b[22]!==VERSION)throw Error('INFO布局');const v=view(b);return {uid:v.getBigUint64(0,true),type:v.getUint16(8,true),firmware:v.getUint32(10,true),boot:v.getBigUint64(14,true),profileVersion:v.getUint16(23,true),epoch:v.getBigUint64(25,true)};}
// DISCOVER reply: type,firmware,boot,epoch,generation (26 bytes); the UID is the frame srcUid.
export type Discovery={type:number;firmware:number;boot:bigint;epoch:bigint;generation:number};
export function discovery(b:Uint8Array):Discovery{if(b.length!==26)throw Error('DISCOVER布局');const v=view(b);return {type:v.getUint16(0,true),firmware:v.getUint32(2,true),boot:v.getBigUint64(6,true),epoch:v.getBigUint64(14,true),generation:v.getUint32(22,true)};}
export type Resource={endpoint:number;resource:number;unit:number;flags:number;minimum:number;maximum:number;group:number;vmax:number;amax:number};
export type Action={opcode:number;mask:number;parameterBytes:number;timeoutUs:number};
export type Capabilities={autoResetMask:number;type:number;version:number;sourceMask:number;maxTargetHz:number;updateGapUs:number;targetMinUs:number;targetMaxUs:number;leaseMinUs:number;leaseMaxUs:number;safeTimeoutUs:number;switchTimeoutUs:number;resources:Resource[];actions:Action[]};
export function capsItem(b:Uint8Array,c:Partial<Capabilities>){
    if(b.length<7||b.length!==7+view(b).getUint16(5,true))throw Error('CAPS长度');const v=view(b.subarray(7));
    if(b[4]===1){if(v.byteLength!==43)throw Error('节点能力');Object.assign(c,{autoResetMask:v.getUint32(39,true),type:v.getUint16(0,true),version:v.getUint16(2,true),sourceMask:v.getUint8(4),maxTargetHz:v.getUint16(5,true),updateGapUs:v.getUint32(7,true),targetMinUs:v.getUint32(11,true),targetMaxUs:v.getUint32(15,true),leaseMinUs:v.getUint32(19,true),leaseMaxUs:v.getUint32(23,true),safeTimeoutUs:v.getUint32(27,true),switchTimeoutUs:v.getUint32(31,true)});}
    else if(b[4]===2){if(v.byteLength!==26)throw Error('资源能力');(c.resources??=[]).push({endpoint:v.getUint16(0,true),resource:v.getUint16(2,true),unit:v.getUint16(4,true),flags:v.getUint16(6,true),minimum:v.getInt32(8,true),maximum:v.getInt32(12,true),group:v.getUint16(16,true),vmax:v.getInt32(18,true),amax:v.getInt32(22,true)});}
    else if(b[4]===3){if(v.byteLength!==12)throw Error('动作能力');(c.actions??=[]).push({opcode:v.getUint16(0,true),mask:v.getUint32(2,true),parameterBytes:v.getUint16(6,true),timeoutUs:v.getUint32(8,true)});}else throw Error('未知能力记录');
    return {next:view(b).getUint16(0,true),total:view(b).getUint16(2,true)};
}
export type Status={epoch:bigint;boot:bigint;generation:number;version:number;state:State;source:Source;fault:number;owner:bigint;accepted:number;applied:number;operation:bigint;leaseRemainingUs:number;activeMask:number;pendingMask:number;safeConfirmed:boolean;next:number;resources:{endpoint:number;resource:number;target:number;feedback:number;ageUs:number;targetValid:boolean;feedbackValid:boolean}[]};
export function status(b:Uint8Array):Status{
    if(b.length<64||b.length!==64+17*b[55])throw Error('STATUS长度');const v=view(b);
    const s:Status={epoch:v.getBigUint64(56,true),boot:v.getBigUint64(0,true),generation:v.getUint32(8,true),version:v.getUint32(12,true),state:b[16],source:b[17],fault:v.getUint16(18,true),owner:v.getBigUint64(20,true),accepted:v.getUint32(28,true),applied:v.getUint32(32,true),operation:v.getBigUint64(36,true),leaseRemainingUs:v.getUint32(44,true),activeMask:v.getUint16(48,true),pendingMask:v.getUint16(50,true),safeConfirmed:!!b[52],next:v.getUint16(53,true),resources:[]};
    for(let i=0;i<b[55];i++){const o=64+17*i;s.resources.push({endpoint:v.getUint16(o,true),resource:v.getUint16(o+2,true),target:v.getInt32(o+4,true),feedback:v.getInt32(o+8,true),ageUs:v.getUint32(o+12,true),targetValid:!!(b[o+16]&1),feedbackValid:!!(b[o+16]&2)});}return s;
}
