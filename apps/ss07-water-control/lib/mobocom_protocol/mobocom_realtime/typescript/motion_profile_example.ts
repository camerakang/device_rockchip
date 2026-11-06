// Host-side example only. The caller must stop its publisher and confirm an explicit STOP first.
// Never calls STOP, RESET_FAULT, SESSION_OPEN, SET_SOURCE, or a motion action implicitly.
import {Client,ConfigurationRequest,ProtocolError} from './client';
import {ConfigurationState} from './configuration';
import {MotionLimit} from './motion_profile';
import {Source,State} from './wire';

export async function applyMotionProfile(client:Client,uid:bigint,values:readonly MotionLimit[]){
    const before=await client.inspect(uid),s=before.status;
    if(before.info.uid!==uid||before.info.boot!==s.boot||before.info.epoch!==s.epoch)throw Error('设备身份或上下文已变化');
    if(s.state!==State.Safe||s.source!==Source.None||s.owner!==0n||s.fault||!s.safeConfirmed)throw Error('请先明确停止并确认 SAFE/NONE');
    const initial=await client.readMotionProfile(uid);
    const intent=values.map(v=>({...v}));
    const saved:{request?:ConfigurationRequest}={};
    try{
        await client.writeMotionProfile(uid,s.boot,s.generation,s.epoch,initial.configuration.revision,intent,
            request=>{saved.request=request;});
    }catch(error){
        if(error instanceof ProtocolError||!saved.request)throw error;
        // Write outcome is unknown. Only query, never create another write transaction.
    }
    const request=saved.request!;
    const deadline=performance.now()+2000;
    while(performance.now()<deadline){
        try{
            const result=await client.readMotionProfile(uid),current=await client.inspect(uid),c=result.configuration;
            if(current.info.uid!==uid||current.info.boot!==s.boot||current.info.epoch!==s.epoch||current.status.boot!==s.boot||current.status.epoch!==s.epoch)throw Error('调速结果未确认：设备上下文已变化');
            if(c.revision>initial.configuration.revision){
                if(c.revision!==initial.configuration.revision+1||c.requestSession!==request.session||c.requestSequence!==request.sequence)throw Error('调速结果未确认：已被其他事务覆盖');
                if(c.state!==ConfigurationState.Applied||c.error)throw Error(`调速未完成，状态 ${c.state}，错误 ${c.error}`);
                if(result.limits.length!==intent.length||!intent.every(v=>result.limits.some(r=>r.endpoint===v.endpoint&&r.resource===v.resource&&r.vmax===v.vmax&&r.amax===v.amax)))throw Error('调速读回值与请求不符');
                return {configuration:c,limits:result.limits,status:current.status};
            }
        }catch(error){
            // Preserve identity/conflict/protocol failures; transport failures may be retried only as reads.
            if(error instanceof ProtocolError||!(error instanceof Error)||!['发送超时','响应超时'].includes(error.message))throw error;
        }
        await new Promise(resolve=>setTimeout(resolve,100));
    }
    throw Error('调速结果未确认：查询超时');
}
