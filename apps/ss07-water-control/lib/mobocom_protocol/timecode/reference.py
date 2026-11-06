from dataclasses import dataclass
import struct

VERSION=0x12
PERIOD_US=10_000
LOSS_US=30_000

def crc16(data):
    c=0xffff
    for b in data:
        c^=b<<8
        for _ in range(8):c=((c<<1)^(0x1021 if c&0x8000 else 0))&0xffff
    return c

@dataclass(frozen=True)
class Packet:
    epoch:int
    sequence:int
    time_us:int

def encode(p):
    if not p.epoch:raise ValueError('zero epoch')
    raw=struct.pack('<2sBBQIQ',b'\xd5\x7c',VERSION,0,p.epoch,p.sequence,p.time_us)
    return raw+struct.pack('<H',crc16(raw[2:]))
def decode(raw):
    if len(raw)!=26:raise ValueError('timecode length')
    sof,version,flags,epoch,seq,time=struct.unpack_from('<2sBBQIQ',raw)
    if sof!=b'\xd5\x7c' or version!=VERSION or flags or not epoch or crc16(raw[2:24])!=struct.unpack_from('<H',raw,24)[0]:raise ValueError('timecode frame')
    return Packet(epoch,seq,time)

class HostTimeline:
    """Pinned playback anchor. origin_us is associated SOF time, not USB write completion."""
    def __init__(self):
        self.latest=None;self.origin=0;self.observed=0
        self.playing=False;self.fallback=False;self.play_origin=0;self.play_time=0
        self.play_epoch=0;self.last_local=0
    def observe(self,p,origin_us,observed_us):
        if not p.epoch or origin_us<0 or origin_us>observed_us or observed_us-origin_us>=LOSS_US or (self.latest and observed_us<self.observed):return False
        if self.latest and p.epoch==self.latest.epoch:
            k=(p.sequence-self.latest.sequence)&0xffffffff
            delta=p.time_us-self.latest.time_us
            if not 0<k<0x80000000 or not 9950*k<=delta<=10050*k:return False
        if self.playing and (observed_us-self.observed>=LOSS_US or p.epoch!=self.play_epoch):self.fallback=True
        self.latest=p;self.origin=origin_us;self.observed=observed_us
        return True
    def start(self,local_us):
        if self.playing:raise ValueError('stop current playback before aligning another')
        if not self.latest or local_us<self.observed or local_us-self.observed>=LOSS_US:raise ValueError('valid timecode required to align a new playback')
        self.playing=True;self.fallback=False;self.play_origin=local_us
        self.play_time=self.latest.time_us+local_us-self.origin
        self.play_epoch=self.latest.epoch;self.last_local=local_us
        return self.play_time
    def now(self,local_us):
        if not self.playing or local_us<self.last_local:raise ValueError('playback/local clock')
        self.last_local=local_us
        if local_us-self.observed>=LOSS_US:self.fallback=True
        return self.play_time+local_us-self.play_origin
    def stop(self):self.playing=False
