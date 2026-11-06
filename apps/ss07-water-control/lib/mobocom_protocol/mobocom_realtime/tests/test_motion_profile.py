import json
import struct
import unittest
from dataclasses import replace
from pathlib import Path
from mobocom_realtime.configuration import ConfigurationSnapshot, ConfigurationState, configuration_write
from mobocom_realtime.motion_profile import MotionLimit, MotionLimitRange, motion_limits, decode_motion_limits, encode_motion_profile, motion_profile
from mobocom_realtime.reference import DeviceCore, VirtualDriver, State, Source, Fault
from mobocom_realtime.profiles import gimbal
from mobocom_realtime.wire import Frame, Kind, Command as C, Error as E, encode, decode, guard, targets


class MemoryMotionProfile:
    def __init__(self):
        self.values = [MotionLimitRange(1,i,50000,100000,1000,50000,1000,100000) for i in (1,2)]
        self.snapshot = ConfigurationSnapshot(2, values=encode_motion_profile(self.values))
        self.writes = 0
    def read(self, schema):
        return (E.OK, replace(self.snapshot,schema=schema)) if schema in (2,3) else (E.UNSUPPORTED, None)
    def busy(self):return False
    def write(self, schema, revision, data, session, sequence, now):
        if schema not in (2,3):return E.UNSUPPORTED
        try:values = decode_motion_limits(data)
        except ValueError:return E.MALFORMED
        if { (v.endpoint,v.resource) for v in values } != {(1,1),(1,2)}:return E.GROUP
        if revision != self.snapshot.revision:return E.CONFLICT
        if revision == 0xffffffff:return E.SPACE
        candidate = list(self.values)
        for v in values:
            r = candidate[v.resource-1]
            if not (r.min_vmax <= v.vmax <= r.max_vmax and r.min_amax <= v.amax <= r.max_amax):return E.RANGE
            candidate[v.resource-1] = replace(r, vmax=v.vmax, amax=v.amax)
        self.values = candidate;self.writes += 1
        self.snapshot = ConfigurationSnapshot(2,revision+1,ConfigurationState.APPLIED,0,session,sequence,encode_motion_profile(candidate))
        return E.OK


def motion_profile_vectors():
    node=DeviceCore(gimbal(),2,20,VirtualDriver());node.configuration=MemoryMotionProfile()
    values=[MotionLimit(1,i,25000,50000) for i in (1,2)]
    def payload(generation=1,revision=0,**kwargs):
        args=dict(uid=2,boot=20,generation=generation,epoch=1,schema=2,revision=revision,values=motion_limits(values));args.update(kwargs)
        return configuration_write(**args)
    steps=[
        ('read_default',1,C.READ_CONFIG,b'\2\0',None),
        ('write',2,C.WRITE_CONFIG,payload(),None),
        ('read_applied',3,C.READ_CONFIG,b'\2\0',None),
        ('lost_ack_retry',2,C.WRITE_CONFIG,payload(),None),
        ('same_sequence_conflict',2,C.WRITE_CONFIG,payload(values=motion_limits([MotionLimit(1,i,30000,50000) for i in (1,2)])),E.CONFLICT),
        ('old_sequence',1,C.WRITE_CONFIG,payload(),E.SEQUENCE),
        ('stale_generation',4,C.WRITE_CONFIG,payload(),E.GENERATION),
        ('stale_revision',5,C.WRITE_CONFIG,payload(2),E.CONFLICT),
        ('range_atomic',6,C.WRITE_CONFIG,payload(2,1,values=motion_limits([MotionLimit(1,1,20000,50000),MotionLimit(1,2,50001,50000)])),E.RANGE),
        ('partial_group',7,C.WRITE_CONFIG,payload(2,1,values=motion_limits(values[:1])),E.GROUP),
        ('stale_boot',8,C.WRITE_CONFIG,payload(2,1,boot=21),E.BOOT),
        ('stale_epoch',9,C.WRITE_CONFIG,payload(2,1,epoch=2),E.EPOCH),
        ('read_unchanged',10,C.READ_CONFIG,b'\2\0',None),
        ('estop',11,C.ESTOP,guard(20,2,1),None),
        ('fault_rejects_motion_profile',12,C.WRITE_CONFIG,payload(2,1),E.STATE),
    ]
    rows=[]
    for i,(name,seq,cmd,data,error) in enumerate(steps):
        at=(i+1)*2000;raw=encode(Frame(Kind.REQUEST,0,2,seq,cmd,100,data));reply=node.transact(raw,at)
        f=decode(reply)
        if error is not None:assert f.kind==Kind.ERROR and struct.unpack_from('<H',f.payload)[0]==error,(name,f)
        else:assert f.kind != Kind.ERROR,(name,f)
        rows.append(dict(name=name,at_us=at,request=raw.hex(),reply=reply.hex()))
    assert node.configuration.writes==1 and node.configuration.snapshot.revision==1
    assert node.driver.apply_calls==0
    return rows


def live_motion_profile_vectors():
    node=DeviceCore(gimbal(),2,20,VirtualDriver());node.configuration=MemoryMotionProfile()
    values=motion_limits([MotionLimit(1,i,25000,50000) for i in (1,2)])
    write=configuration_write(2,20,2,2,3,0,values)
    rows=[]
    def request(name,seq,cmd,data,session=100,error=None,at=None):
        at=at or (len(rows)+1)*2000
        node.tick(at-1)
        raw=encode(Frame(Kind.REQUEST,0,2,seq,cmd,session,data));reply=node.transact(raw,at);f=decode(reply)
        assert (f.kind==Kind.ERROR and struct.unpack_from('<H',f.payload)[0]==error) if error else f.kind!=Kind.ERROR,(name,f)
        rows.append(dict(name=name,at_us=at,request=raw.hex(),reply=reply.hex()))
    request('open',1,C.SESSION_OPEN,struct.pack('<QQI',20,1,1))
    request('source',2,C.SET_SOURCE,guard(20,1,2)+struct.pack('<BII',2,100000,300000))
    request('target',3,C.WRITE_TARGETS,targets(20,2,[(1,1,40000),(1,2,40000)],2))
    lease=node.lease_until;received=dict(node.received)
    request('read_live_capability',4,C.READ_CONFIG,b'\3\0')
    request('live_write',5,C.WRITE_CONFIG,write)
    assert node.generation==2 and node.accepted_sequence==3 and node.lease_until==lease and node.received==received
    request('read_applied',6,C.READ_CONFIG,b'\3\0')
    request('lost_ack_retry',5,C.WRITE_CONFIG,write)
    request('foreign_session',7,C.WRITE_CONFIG,write,101,E.SESSION)
    request('same_owner_retry_after_foreign',5,C.WRITE_CONFIG,write)
    request('stale_revision',8,C.WRITE_CONFIG,write,error=E.CONFLICT)
    request('stopped_schema_in_active',9,C.WRITE_CONFIG,configuration_write(2,20,2,2,2,1,values),error=E.STATE)
    request('stop',10,C.STOP,guard(20,2,2))
    request('replay_after_stop',5,C.WRITE_CONFIG,write,error=E.GENERATION)
    request('live_write_while_safe',11,C.WRITE_CONFIG,configuration_write(2,20,node.generation,2,3,1,values),error=E.OWNER)
    assert node.configuration.writes==1
    return rows

def write_vectors():
    root=Path(__file__).resolve().parents[1];rows=motion_profile_vectors()
    (root/'motion_profile_vectors.json').write_text(json.dumps(rows,indent=2)+'\n',encoding='utf-8')
    lines=['#pragma once','#include <cstdint>','#include <string_view>',
           'struct MotionProfileVector {std::uint64_t at;std::string_view request,reply;};',
           'inline constexpr MotionProfileVector motion_profile_vectors[]={']
    lines += ['{%d,"%s","%s"},'%(r['at_us'],r['request'],r['reply']) for r in rows]
    (root/'tests/motion_profile_vectors.hpp').write_text('\n'.join(lines+['};'])+'\n',encoding='utf-8')
    rows=live_motion_profile_vectors()
    (root/'live_motion_profile_vectors.json').write_text(json.dumps(rows,indent=2)+'\n',encoding='utf-8')
    lines=['#pragma once','inline constexpr MotionProfileVector live_motion_profile_vectors[]={']
    lines += ['{%d,"%s","%s"},'%(r['at_us'],r['request'],r['reply']) for r in rows]
    (root/'tests/live_motion_profile_vectors.hpp').write_text('\n'.join(lines+['};'])+'\n',encoding='utf-8')


class MotionProfileTests(unittest.TestCase):
    def test_live_golden_vectors(self):
        path=Path(__file__).resolve().parents[1]/'live_motion_profile_vectors.json'
        self.assertEqual(json.loads(path.read_text()),live_motion_profile_vectors())
    def test_golden_vectors(self):
        path=Path(__file__).resolve().parents[1]/'motion_profile_vectors.json'
        self.assertEqual(json.loads(path.read_text()),motion_profile_vectors())
    def test_codec_strict_layout_and_ranges(self):
        v=[MotionLimit(1,i,25000,50000) for i in (1,2,3)]
        self.assertEqual(decode_motion_limits(motion_limits(v)),v)
        for bad in [[],v*3,[v[0],v[0]],[replace(v[0],vmax=0)],[replace(v[0],amax=-1)],[replace(v[0],vmax=1.5)],[replace(v[0],vmax=2**31)]]:
            with self.assertRaises(ValueError):motion_limits(bad)
        raw=motion_limits(v)
        for bad in [raw[:-1],raw+b'\0',b'\1'+raw[1:],raw[:1]+b'\1'+raw[2:],b'\2\0\0']:
            with self.assertRaises(ValueError):decode_motion_limits(bad)
        adapter=MemoryMotionProfile();self.assertEqual(motion_profile(adapter.snapshot),adapter.values)
        for bad in [replace(adapter.snapshot,schema=1),replace(adapter.snapshot,values=adapter.snapshot.values[:-1])]:
            with self.assertRaises(ValueError):motion_profile(bad)
        with self.assertRaises(ValueError):encode_motion_profile([replace(adapter.values[0],min_vmax=60000)])
    def test_safe_gate_and_generation_exhaustion(self):
        for mode in ('unsafe','fault','active','exhausted'):
            n=DeviceCore(gimbal(),2,20,VirtualDriver());n.configuration=MemoryMotionProfile()
            if mode=='unsafe':n.driver.safe_fail=True
            if mode=='fault':n.state=State.FAULT;n.fault=Fault.DRIVER
            if mode=='active':n.state=State.ACTIVE;n.source=Source.REMOTE;n.lease_until=1000000
            if mode=='exhausted':n.generation=0xfffffffe
            data=configuration_write(2,20,n.generation,1,2,0,motion_limits([MotionLimit(1,i,25000,50000) for i in (1,2)]))
            f=decode(n.transact(encode(Frame(Kind.REQUEST,0,2,1,C.WRITE_CONFIG,100,data)),2000))
            self.assertEqual(f.kind,Kind.ERROR)
            self.assertEqual(struct.unpack_from('<H',f.payload)[0],E.SPACE if mode=='exhausted' else E.STATE)
            self.assertEqual(n.configuration.writes,0)
    def test_reboot_discards_profile_and_old_boot(self):
        a=MemoryMotionProfile();data=motion_limits([MotionLimit(1,i,25000,50000) for i in (1,2)])
        self.assertEqual(a.write(2,0,data,100,1,0),E.OK)
        fresh=MemoryMotionProfile();self.assertEqual(fresh.snapshot.revision,0)
        self.assertEqual(fresh.values[0].vmax,50000)

if __name__=='__main__':unittest.main()
