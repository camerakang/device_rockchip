import {Bytes,view} from './wire';
import {ConfigurationSnapshot} from './configuration';

export const MOTION_PROFILE_SCHEMA=2;
export const LIVE_MOTION_PROFILE_SCHEMA=3;
export const MAX_MOTION_PROFILE_RESOURCES=7;
export type MotionLimit={endpoint:number;resource:number;vmax:number;amax:number};
export type MotionLimitRange=MotionLimit&{minVmax:number;maxVmax:number;minAmax:number;maxAmax:number};
function validate(values:readonly MotionLimit[],ranges=false){
    if(values.length<1||values.length>MAX_MOTION_PROFILE_RESOURCES)throw Error('调速资源数量');
    const seen=new Set<string>();
    for(const v of values){
        const key=`${v.endpoint}/${v.resource}`;
        if(seen.has(key)||![v.endpoint,v.resource].every(x=>Number.isInteger(x)&&x>=0&&x<=65535))throw Error('调速资源重复或非法');
        seen.add(key);
        const r=v as MotionLimitRange;
        const numbers=ranges?[v.vmax,v.amax,r.minVmax,r.maxVmax,r.minAmax,r.maxAmax]:[v.vmax,v.amax];
        if(!numbers.every(x=>Number.isInteger(x)&&x>0&&x<=0x7fffffff))throw Error('调速值必须为正 int32');
        if(ranges&&!(r.minVmax<=v.vmax&&v.vmax<=r.maxVmax&&r.minAmax<=v.amax&&v.amax<=r.maxAmax))throw Error('调速范围');
    }
}
export function motionLimits(values:readonly MotionLimit[]):Uint8Array {
    validate(values);const b=new Bytes().u8(2).u8(0).u8(values.length);
    for(const v of values)b.u16(v.endpoint).u16(v.resource).i32(v.vmax).i32(v.amax);
    return b.get();
}
export function decodeMotionLimits(data:Uint8Array):MotionLimit[]{
    if(data.length<3||data[0]!==2||data[1]!==0||data.length!==3+12*data[2])throw Error('调速写入布局');
    const d=view(data),values:MotionLimit[]=[];
    for(let i=0;i<data[2];i++){const p=3+12*i;values.push({endpoint:d.getUint16(p,true),resource:d.getUint16(p+2,true),vmax:d.getInt32(p+4,true),amax:d.getInt32(p+8,true)});}
    validate(values);return values;
}
export function motionProfile(snapshot:ConfigurationSnapshot):MotionLimitRange[]{
    const data=snapshot.values;
    if(![MOTION_PROFILE_SCHEMA,LIVE_MOTION_PROFILE_SCHEMA].includes(snapshot.schema)||data.length<3||data[0]!==2||data[1]!==0||data.length!==3+28*data[2])throw Error('调速读取布局');
    const d=view(data),values:MotionLimitRange[]=[];
    for(let i=0;i<data[2];i++){const p=3+28*i;values.push({endpoint:d.getUint16(p,true),resource:d.getUint16(p+2,true),vmax:d.getInt32(p+4,true),amax:d.getInt32(p+8,true),minVmax:d.getInt32(p+12,true),maxVmax:d.getInt32(p+16,true),minAmax:d.getInt32(p+20,true),maxAmax:d.getInt32(p+24,true)});}
    validate(values,true);return values;
}
