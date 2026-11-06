import {ConfigurationSnapshot} from './configuration';
import {view} from './wire';

export const FOCUS_CONTROL_SCHEMA=4;
export enum FocusControlSource {Wireless=0,Wired=1,Stopped=2,Fault=3}
export type FocusControlStatus={source:FocusControlSource;outputIdle:boolean;radioReady:boolean;busQuiet:boolean;buttonRemainingMs:number;wiredCommands:boolean};
export function focusSource(source:FocusControlSource):Uint8Array {
    if(!Number.isInteger(source)||source<0||source>2)throw Error('跟焦来源只能为无线、有线或停止');
    return Uint8Array.of(source);
}
export function focusControl(snapshot:ConfigurationSnapshot):FocusControlStatus {
    const v=snapshot.values;
    if(snapshot.schema!==FOCUS_CONTROL_SCHEMA||v.length!==8||v[0]>3||(v[1]&~15)||view(v).getUint16(2,true))throw Error('跟焦切源状态布局');
    const buttonRemainingMs=view(v).getUint32(4,true);
    if((v[1]&1)&&buttonRemainingMs)throw Error('跟焦空闲与按钮忙状态冲突');
    return {source:v[0],outputIdle:!!(v[1]&1),radioReady:!!(v[1]&2),busQuiet:!!(v[1]&4),buttonRemainingMs,wiredCommands:!!(v[1]&8)};
}

export function focusPositions(mask:number,positions:readonly number[]):Uint8Array {
    if(!Number.isInteger(mask)||mask<1||mask>7||positions.length!==3||positions.some((v,i)=>!Number.isInteger(v)||v<0||v>65535||(!(mask&(1<<i))&&v!==0)))throw Error('跟焦归一化位置或通道掩码无效');
    const bytes=new Uint8Array(8);bytes[0]=0x10;bytes[1]=mask;
    positions.forEach((v,i)=>view(bytes).setUint16(2+2*i,v,true));return bytes;
}
export function focusButton(action:number):Uint8Array {
    if(!Number.isInteger(action)||action<0||action>1)throw Error('跟焦按钮动作无效');
    return Uint8Array.of(0x11,action);
}
