import struct
import unittest
from dataclasses import replace
from mobocom_realtime.configuration import ConfigurationSnapshot, ConfigurationState as S, configuration_write, radio_channels
from mobocom_realtime.wire import ALL_NODES, Frame, Kind, Command as C, Error as E, encode, decode, guard
from mobocom_realtime.reference import DeviceCore, VirtualDriver, Fault, Source
from mobocom_realtime.profiles import gimbal


class MemoryConfiguration:
    """Only a test adapter; production persistence is never implemented in the SDK."""
    def __init__(self):
        self.snapshot = ConfigurationSnapshot(1, values=bytes((9, 2, 1, 2)))
        self.writes = 0
    def read(self, schema):
        return (E.OK, self.snapshot) if schema == 1 else (E.UNSUPPORTED, None)
    def busy(self):
        return self.snapshot.state == S.PENDING
    def write(self, schema, revision, values, session, sequence, now):
        if schema != 1:return E.UNSUPPORTED
        if len(values) != 2:return E.MALFORMED
        if max(values) > 15:return E.RANGE
        if revision != self.snapshot.revision:return E.CONFLICT
        self.writes += 1
        self.snapshot = replace(self.snapshot, revision=revision+1, state=S.PENDING, request_session=session, request_sequence=sequence)
        return E.OK


def configuration_vectors():
    node = DeviceCore(gimbal(), 2, 20, VirtualDriver())
    node.configuration = MemoryConfiguration()
    node.fault_input(0, Fault.DRIVER)
    payload = configuration_write(2, 20, 1, 1, 1, 0, radio_channels(0, 2))
    frames = [
        Frame(Kind.REQUEST,0,2,1,C.READ_CONFIG,100,b'\x01\0'),
        Frame(Kind.REQUEST,0,2,2,C.WRITE_CONFIG,100,payload),
        Frame(Kind.REQUEST,0,2,3,C.READ_CONFIG,100,b'\x01\0'),
        Frame(Kind.REQUEST,0,2,2,C.WRITE_CONFIG,100,payload),
        Frame(Kind.REQUEST,0,2,2,C.WRITE_CONFIG,100,payload[:-1]+b'\x03'),
        Frame(Kind.REQUEST,0,2,1,C.WRITE_CONFIG,100,payload),
        Frame(Kind.REQUEST,0,2,4,C.WRITE_CONFIG,100,payload),
        Frame(Kind.REQUEST,0,2,5,C.READ_CONFIG,200,b'\x02\0'),
    ]
    return [dict(at_us=(i+1)*2000,request=encode(f).hex(),reply=node.transact(encode(f),(i+1)*2000).hex()) for i,f in enumerate(frames)]


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.node=DeviceCore(gimbal(),2,20,VirtualDriver())
        self.adapter=MemoryConfiguration();self.node.configuration=self.adapter
        self.sequence=0;self.now=0
    def send(self,command,payload,session=100,sequence=None):
        self.sequence+=1;self.now+=2000
        raw=encode(Frame(Kind.REQUEST,0,2,sequence or self.sequence,command,session,payload))
        return decode(self.node.transact(raw,self.now))
    def payload(self,**changes):
        args=dict(uid=2,boot=20,generation=1,epoch=1,schema=1,revision=0,values=b'\0\x02');args.update(changes)
        return configuration_write(**args)
    def error(self,frame,code):
        self.assertEqual(frame.kind,Kind.ERROR);self.assertEqual(struct.unpack_from('<H',frame.payload)[0],code)
    def test_fault_allows_configuration_without_motion_session(self):
        self.node.fault_input(0,Fault.DRIVER)
        self.assertEqual(self.send(C.WRITE_CONFIG,self.payload()).kind,Kind.ACK)
        self.assertEqual(self.node.fault,Fault.DRIVER)
        self.assertEqual(self.node.session_id,0)
        self.assertEqual(self.node.source,Source.NONE)
        self.assertEqual(self.node.driver.apply_calls,0)
    def test_guards_range_schema_and_length(self):
        for changes,code in [({'uid':3},E.MALFORMED),({'boot':21},E.BOOT),({'epoch':2},E.EPOCH),({'generation':2},E.GENERATION),({'schema':2},E.UNSUPPORTED),({'revision':1},E.CONFLICT),({'values':b'\x10\0'},E.RANGE),({'values':b'\0'},E.MALFORMED)]:
            self.error(self.send(C.WRITE_CONFIG,self.payload(**changes)),code)
        self.assertEqual(self.adapter.writes,0)
    def test_cached_write_survives_polling_and_busy_blocks_sources(self):
        payload=self.payload();first=self.send(C.WRITE_CONFIG,payload,sequence=1)
        self.send(C.READ_CONFIG,b'\1\0')
        self.assertEqual(self.send(C.WRITE_CONFIG,payload,sequence=1),first)
        self.assertEqual(self.adapter.writes,1)
        self.error(self.send(C.WRITE_CONFIG,self.payload(values=b'\1\2'),sequence=1),E.CONFLICT)
        self.error(self.send(C.WRITE_CONFIG,payload),E.BUSY)
        self.assertEqual(self.node.select_local_source(Source.REMOTE,1,1,100000,300000,self.now),E.BUSY)
        self.error(self.send(C.SESSION_OPEN,struct.pack('<QQI',20,1,1)),E.BUSY)
    def test_unimplemented_broadcast_and_active(self):
        self.node.configuration=None
        self.error(self.send(C.READ_CONFIG,b'\1\0'),E.UNSUPPORTED)
        self.node.configuration=self.adapter
        raw=encode(Frame(Kind.REQUEST,0,ALL_NODES,1,C.WRITE_CONFIG,100,self.payload()))
        self.assertIsNone(self.node.transact(raw,self.now+1))
        self.assertEqual(self.node.select_local_source(Source.REMOTE,1,1,100000,300000,self.now+2),E.OK)
        self.node.tick(self.now+3)
        self.error(self.send(C.WRITE_CONFIG,self.payload(generation=2)),E.STATE)
    def test_snapshot_codec_and_limits(self):
        snapshot=ConfigurationSnapshot(1,7,S.FAILED,E.DRIVER,100,9,b'\0\2\1\2')
        self.assertEqual(ConfigurationSnapshot.decode(snapshot.encode()),snapshot)
        for raw in [b'',snapshot.encode()[:-1],snapshot.encode()+b'\0']:
            with self.assertRaises(ValueError):ConfigurationSnapshot.decode(raw)
        for values in [(-1,2),(16,2),(1.5,2)]:
            with self.assertRaises(ValueError):radio_channels(*values)

if __name__=='__main__':unittest.main()
