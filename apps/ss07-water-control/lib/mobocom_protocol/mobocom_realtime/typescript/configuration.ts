import {Bytes,guard,view} from './wire';

export const DUAL_RADIO_SCHEMA=1;
export enum ConfigurationState {Idle=0,Pending=1,Applied=2,Failed=3}
export type ConfigurationSnapshot={schema:number;revision:number;state:ConfigurationState;error:number;requestSession:bigint;requestSequence:number;values:Uint8Array};
export function configurationSnapshot(data:Uint8Array):ConfigurationSnapshot {
    if(data.length<23)throw Error('配置响应头不完整');
    const d=view(data),length=d.getUint16(21,true);
    if(length>217||data.length!==23+length||data[6]>3)throw Error('配置响应布局');
    return {schema:d.getUint16(0,true),revision:d.getUint32(2,true),state:data[6],error:d.getUint16(7,true),requestSession:d.getBigUint64(9,true),requestSequence:d.getUint32(17,true),values:data.slice(23)};
}
export function configurationWrite(uid:bigint,boot:bigint,generation:number,epoch:bigint,schema:number,revision:number,values:Uint8Array) {
    if(values.length>206)throw Error('配置值过长');
    return new Bytes().u64(uid).raw(guard(boot,generation,epoch)).u16(schema).u32(revision).raw(values).get();
}
export function radioChannels(focus:number,wheel:number) {
    if(![focus,wheel].every(v=>Number.isInteger(v)&&v>=0&&v<=15))throw Error('逻辑频道必须为 0..15');
    return Uint8Array.of(focus,wheel);
}
export function dualRadioConfiguration(snapshot:ConfigurationSnapshot) {
    const v=snapshot.values;
    if(snapshot.schema!==DUAL_RADIO_SCHEMA||v.length!==4||v[0]>15||v[1]>15||(v[2]&~3)||v[3]>3)throw Error('双无线配置布局');
    return {focusChannel:v[0],wheelChannel:v[1],focusReady:!!(v[2]&1),wheelReady:!!(v[2]&2),focusSource:v[3]};
}
