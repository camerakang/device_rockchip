import json
import struct
import unittest
from dataclasses import replace
from pathlib import Path
from mobocom_realtime.configuration import ConfigurationSnapshot, ConfigurationState as S, configuration_write
from mobocom_realtime.focus_control import FOCUS_CONTROL_SCHEMA, FocusControlSource as F, FocusControlStatus, focus_control, focus_source, focus_positions, focus_button, decode_focus_command
from mobocom_realtime.wire import ALL_NODES, Frame, Kind, Command as C, Error as E, encode, decode, guard
from mobocom_realtime.reference import DeviceCore, VirtualDriver, Fault, Source, State
from mobocom_realtime.profiles import gimbal

class FocusFixture:
    """Protocol fixture only. Actual bus, button and radio gates belong to the board."""
    def __init__(self):
        self.snapshot = ConfigurationSnapshot(4, 1, values=FocusControlStatus(F.WIRELESS, True, True, True).encode())
        self.writes = 0
        self.is_busy = False
    def busy(self): return self.is_busy
    def read(self, schema):
        return (E.OK, self.snapshot) if schema == 4 else (E.UNSUPPORTED, None)
    def write(self, schema, revision, values, session, sequence, now):
        if schema != 4: return E.UNSUPPORTED
        if len(values) != 1: return E.MALFORMED
        if values[0] > 2: return E.RANGE
        if revision != self.snapshot.revision: return E.CONFLICT
        if revision == 0xffffffff: return E.SPACE
        self.writes += 1
        self.snapshot = ConfigurationSnapshot(4, revision+1, S.APPLIED, E.OK, session, sequence,
            FocusControlStatus(F(values[0]), True, True, True).encode())
        return E.OK

def make_node():
    node = DeviceCore(gimbal(), 2, 20, VirtualDriver())
    node.configuration = FocusFixture()
    return node

def payload(revision=1, source=1, **changes):
    args = dict(uid=2, boot=20, generation=1, epoch=1, schema=4, revision=revision, values=bytes((source,)))
    args.update(changes)
    return configuration_write(**args)

def focus_vectors():
    node = make_node()
    cases = [(1,C.READ_CONFIG,b'\4\0'), (2,C.WRITE_CONFIG,payload()), (3,C.READ_CONFIG,b'\4\0'),
        (2,C.WRITE_CONFIG,payload()), (2,C.WRITE_CONFIG,payload(source=0)), (4,C.WRITE_CONFIG,payload()),
        (5,C.WRITE_CONFIG,payload(2,3)), (6,C.WRITE_CONFIG,payload(2,2)), (7,C.READ_CONFIG,b'\4\0'),
        (8,C.WRITE_CONFIG,payload(3,0)), (9,C.READ_CONFIG,b'\4\0'),
        (10,C.WRITE_CONFIG,payload(4,1,boot=21)), (11,C.WRITE_CONFIG,payload(4,1,generation=2)),
        (12,C.WRITE_CONFIG,payload(4,1,values=b''))]
    result = []
    for i,(seq,cmd,data) in enumerate(cases):
        raw = encode(Frame(Kind.REQUEST,0,2,seq,cmd,100,data)); now = (i+1)*2000
        result.append(dict(at_us=now,request=raw.hex(),reply=node.transact(raw,now).hex()))
    return result

class FocusControlTests(unittest.TestCase):
    def setUp(self): self.node=make_node(); self.sequence=0; self.now=0
    def send(self, data=None, command=C.WRITE_CONFIG, sequence=None, session=100):
        self.sequence+=1; self.now+=2000
        raw=encode(Frame(Kind.REQUEST,0,2,sequence or self.sequence,command,session,payload() if data is None else data))
        return decode(self.node.transact(raw,self.now))
    def error(self, response, code):
        self.assertEqual(response.kind,Kind.ERROR); self.assertEqual(struct.unpack_from('<H',response.payload)[0],code)
    def test_golden_vectors(self):
        path=Path(__file__).parents[1]/'focus_control_vectors.json'
        self.assertEqual(json.loads(path.read_text()),focus_vectors())
    def test_runtime_states_do_not_gate_or_change_gimbal(self):
        for state in State:
            node=make_node(); node.state=state; node.source=Source.REMOTE; node.owner=0
            node.fault=Fault.DRIVER if state==State.FAULT else Fault.NONE
            node.transition_start=0; node.driver.safe_delay_us=100000; node.lease_until=300000
            before=(node.generation,node.session_epoch,node.source,node.owner,node.fault,node.lease_until,node.driver.flush_calls,node.driver.safe_calls)
            raw=encode(Frame(Kind.REQUEST,0,2,1,C.WRITE_CONFIG,100,payload()))
            self.assertEqual(decode(node.transact(raw,2000)).kind,Kind.ACK)
            self.assertEqual(before,(node.generation,node.session_epoch,node.source,node.owner,node.fault,node.lease_until,node.driver.flush_calls,node.driver.safe_calls))
    def test_active_owner_targets_and_ttl_are_unchanged(self):
        n=self.node
        self.assertEqual(self.send(struct.pack('<QQI',20,1,1),C.SESSION_OPEN).kind,Kind.ACK)
        self.assertEqual(self.send(guard(20,1,2)+struct.pack('<BII',2,100000,300000),C.SET_SOURCE).kind,Kind.ACK)
        self.now+=20; n.tick(self.now)
        targets=guard(20,n.generation,2)+bytes((2,))+struct.pack('<HHiHHi',1,1,100,1,2,200)
        self.assertEqual(self.send(targets,C.WRITE_TARGETS).kind,Kind.ACK)
        n.tick(self.now+20)
        before=(n.state,n.source,n.owner,n.generation,n.session_epoch,n.lease_until,dict(n.received),dict(n.values),n.driver.flush_calls)
        # Separate maintenance session need not own the gimbal.
        self.assertEqual(self.send(payload(generation=n.generation,epoch=2),session=200).kind,Kind.ACK)
        self.assertEqual(before,(n.state,n.source,n.owner,n.generation,n.session_epoch,n.lease_until,dict(n.received),dict(n.values),n.driver.flush_calls))
    def test_guards_busy_unknown_and_broadcast(self):
        for data,code in [(payload(uid=3),E.MALFORMED),(payload(boot=21),E.BOOT),(payload(epoch=2),E.EPOCH),
                          (payload(generation=2),E.GENERATION),(payload(schema=99),E.UNSUPPORTED),(payload(2),E.CONFLICT),
                          (payload(source=3),E.RANGE),(payload(values=b'\1\0'),E.MALFORMED)]:
            self.error(self.send(data),code)
        self.node.configuration.is_busy=True; self.error(self.send(),E.BUSY)
        self.assertEqual(self.node.configuration.writes,0)
        raw=encode(Frame(Kind.REQUEST,0,ALL_NODES,100,C.WRITE_CONFIG,100,payload()))
        self.assertIsNone(self.node.transact(raw,self.now+2000))
    def test_cached_write_and_stale_replay(self):
        first=self.send(sequence=1)
        self.send(b'\4\0',C.READ_CONFIG)
        self.assertEqual(self.send(sequence=1),first); self.assertEqual(self.node.configuration.writes,1)
        self.assertEqual(self.node.select_local_source(Source.REMOTE,1,1,100000,300000,self.now+1),E.OK)
        self.node.tick(self.now+2)
        self.node.fault_input(self.now+3,Fault.ESTOP)
        self.error(self.send(sequence=1),E.GENERATION)
        self.assertEqual(self.node.configuration.writes,1)
    def test_wired_command_vectors(self):
        rows=json.loads((Path(__file__).parents[1]/'focus_command_vectors.json').read_text())
        for row in rows:
            data=focus_positions(row['mask'],row['positions']) if row['operation']=='positions' else focus_button(row['action'])
            self.assertEqual(data.hex(),row['hex'])
            self.assertEqual(decode_focus_command(data)[0],0x10 if row['operation']=='positions' else 0x11)
        for data in [b'',bytes.fromhex('1000000000000000'),bytes.fromhex('1008000000000000'),bytes.fromhex('1001000001000000'),b'\x11\x02',b'\x12\x00',b'\x10',b'\x11\x00\x00']:
            with self.assertRaises(ValueError):decode_focus_command(data)
        status=FocusControlStatus(F.WIRED,True,True,True,0,True)
        self.assertTrue(FocusControlStatus.decode(status.encode()).wired_commands)
        self.assertFalse(FocusControlStatus.decode(bytes.fromhex('0107000000000000')).wired_commands)

    def test_codec_strictness(self):
        status=FocusControlStatus(F.WIRED,False,True,True,3120)
        self.assertEqual(FocusControlStatus.decode(status.encode()),status)
        self.assertEqual(focus_control(ConfigurationSnapshot(4,values=status.encode())),status)
        for data in [b'',status.encode()[:-1],status.encode()+b'\0',b'\4\0'+b'\0'*6,b'\1\x80'+b'\0'*6,b'\1\0\1'+b'\0'*5,b'\1\1\0\0\1\0\0\0']:
            with self.assertRaises(ValueError):FocusControlStatus.decode(data)
        for value in [-1,3,256,1.5]:
            with self.assertRaises(ValueError):focus_source(value)
        self.assertEqual(focus_source(F.WIRED),b'\1')
        with self.assertRaises(ValueError):focus_control(ConfigurationSnapshot(1,values=status.encode()))

if __name__=='__main__': unittest.main()
