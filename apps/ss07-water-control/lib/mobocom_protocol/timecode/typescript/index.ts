// Host-only 0x12 timecode: no device-control dependency.
export const TIMECODE_VERSION=0x12, TIMECODE_PERIOD_US=10000n, TIMECODE_LOSS_US=30000n;
export type TimeCodePacket={epoch:bigint;sequence:number;timeUs:bigint};
function crc(b:Uint8Array){let c=65535;for(const v of b){c^=v<<8;for(let i=0;i<8;i++)c=((c<<1)^((c&0x8000)?0x1021:0))&65535;}return c;}
export function encodeTimeCode(p:TimeCodePacket){
    if(p.epoch<=0n||p.epoch>0xffffffffffffffffn||!Number.isInteger(p.sequence)||p.sequence<0||p.sequence>0xffffffff||p.timeUs<0n||p.timeUs>0xffffffffffffffffn)throw Error('授时字段');
    const b=new Uint8Array(26),v=new DataView(b.buffer);b.set([0xd5,0x7c,0x12,0]);v.setBigUint64(4,p.epoch,true);v.setUint32(12,p.sequence,true);v.setBigUint64(16,p.timeUs,true);v.setUint16(24,crc(b.subarray(2,24)),true);return b;
}
export function decodeTimeCode(b:Uint8Array):TimeCodePacket{
    if(b.length!==26)throw Error('授时长度');const v=new DataView(b.buffer,b.byteOffset,b.byteLength);
    if(b[0]!==0xd5||b[1]!==0x7c||b[2]!==0x12||b[3]||!v.getBigUint64(4,true)||v.getUint16(24,true)!==crc(b.subarray(2,24)))throw Error('授时帧');
    return {epoch:v.getBigUint64(4,true),sequence:v.getUint32(12,true),timeUs:v.getBigUint64(16,true)};
}
export class TimeCodeParser {
    private bytes:number[]=[];private last:bigint|null=null;
    feed(data:Uint8Array,localUs:bigint){if(this.last!==null&&(localUs<this.last||localUs-this.last>100000n))this.bytes=[];if(data.length)this.last=localUs;this.bytes.push(...data);const out:TimeCodePacket[]=[];
        while(this.bytes.length){if(this.bytes[0]!==0xd5||(this.bytes.length>=2&&this.bytes[1]!==0x7c)){this.bytes.shift();continue;}if(this.bytes.length<26)break;try{out.push(decodeTimeCode(Uint8Array.from(this.bytes.slice(0,26))));this.bytes.splice(0,26);}catch{this.bytes.shift();}}return out;}
}
export class HostTimeline {
    private latest:TimeCodePacket|null=null;private origin=0n;private observed=0n;private playOrigin=0n;private playTime=0n;private playEpoch=0n;private lastLocal=0n;
    playing=false;fallback=false;
    observe(p:TimeCodePacket,originUs:bigint,observedUs:bigint){
        if(!p.epoch||originUs<0n||originUs>observedUs||observedUs-originUs>=TIMECODE_LOSS_US||(this.latest&&observedUs<this.observed))return false;
        if(this.latest&&p.epoch===this.latest.epoch){const k=(p.sequence-this.latest.sequence)>>>0,delta=p.timeUs-this.latest.timeUs;if(!k||k>=0x80000000||delta<BigInt(k)*9950n||delta>BigInt(k)*10050n)return false;}
        if(this.playing&&(observedUs-this.observed>=TIMECODE_LOSS_US||p.epoch!==this.playEpoch))this.fallback=true;
        this.latest=p;this.origin=originUs;this.observed=observedUs;return true;
    }
    start(localUs:bigint){if(this.playing||!this.latest||localUs<this.observed||localUs-this.observed>=TIMECODE_LOSS_US)throw Error('先结束本场，且新播放需要有效时码');this.playing=true;this.fallback=false;this.playOrigin=localUs;this.playTime=this.latest.timeUs+localUs-this.origin;this.playEpoch=this.latest.epoch;this.lastLocal=localUs;return this.playTime;}
    now(localUs:bigint){if(!this.playing||localUs<this.lastLocal)throw Error('播放或本地时钟异常');this.lastLocal=localUs;if(localUs-this.observed>=TIMECODE_LOSS_US)this.fallback=true;return this.playTime+localUs-this.playOrigin;}
    stop(){this.playing=false;}
}
