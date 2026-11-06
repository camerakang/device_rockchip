import struct
import unittest
from copy import deepcopy
from .wire import *
from .reference import *
from .profiles import lens,gimbal,fans
from timecode.reference import HostTimeline,Packet,encode as tc_encode,decode as tc_decode

class Harness:
    def __init__(self,profile=None,uid=1,boot=10,session=100):
        self.driver=VirtualDriver();self.node=DeviceCore(profile or lens(),uid,boot,self.driver)
        self.seq=0;self.session=session;self.now=0
        self.send(Command.SESSION_OPEN,struct.pack("<QQI",boot,self.node.session_epoch,self.node.generation))
    def raw(self,command,payload=b'',seq=None,session=None,dst=None):
        if command in (Command.STOP,Command.ESTOP) and not payload and dst!=ALL_NODES:payload=self.guarded()
        if seq is None:self.seq+=1;seq=self.seq
        return encode(Frame(Kind.REQUEST,0,self.node.uid if dst is None else dst,seq,command,self.session if session is None else session,payload))
    def send(self,command,payload=b'',**kw):
        result=self.node.transact(self.raw(command,payload,**kw),self.now)
        return decode(result) if result else None
    def guarded(self):return guard(self.node.boot_id,self.node.generation,self.node.session_epoch)
    def source(self,source=Source.MOBOCOM):
        f=self.send(Command.SET_SOURCE,self.guarded()+struct.pack('<BII',source,100000,300000))
        self.node.tick(self.now)
        return f
    def target(self,values=None):return self.send(Command.WRITE_TARGETS,targets(self.node.boot_id,self.node.generation,values or [(1,1,500)],self.node.session_epoch))
    def step(self,until):
        while self.now+5000<until:self.now+=5000;self.node.tick(self.now)
        self.now=until;self.node.tick(until)

class WireTests(unittest.TestCase):
    def test_header_crc(self):
        self.assertEqual(crc16(b'123456789'),0x29b1)
        f=Frame(Kind.REQUEST,0,0x9006000000000001,0xffffffff,Command.WRITE_TARGETS,0xffffffffffffffff,b'\xd5\x7d')
        b=encode(f);self.assertEqual(len(b),HEADER_SIZE+2+2);self.assertEqual(decode(b),f)
        self.assertEqual(b[2],0x22);self.assertEqual(struct.unpack_from('<QQ',b,5),(0,0x9006000000000001))
        for offset in range(len(b)):
            bad=bytearray(b);bad[offset]^=1
            with self.assertRaises(ValueError):decode(bytes(bad))
    def test_datagram_is_exactly_one_frame(self):
        b=encode(Frame(1,0,1,1,Command.INFO,1))
        for bad in (b+b,b[:-1],b+b'\0',b'\0'+b):
            with self.assertRaises(ValueError):decode(bad)
        with self.assertRaises(ValueError):encode(Frame(1,0,1<<64,1,Command.INFO,1))
        self.assertEqual(len(encode(Frame(1,0,1,1,Command.WRITE_TARGETS,1,bytes(240)))),MAX_FRAME)
    def test_multicast_discovery_reports_uid_in_header(self):
        h=Harness(uid=0x9006000000000001);reply=h.send(Command.DISCOVER,dst=ALL_NODES)
        self.assertEqual((reply.kind,reply.src_uid,reply.dst_uid),(Kind.RESPONSE,0x9006000000000001,0))
        self.assertEqual(struct.unpack('<HIQQI',reply.payload),(h.node.profile.device_type,1,10,h.node.session_epoch,h.node.generation))
        self.assertIsNone(h.send(Command.DISCOVER,b'\0',dst=ALL_NODES))
        self.assertIsNone(h.send(Command.DISCOVER))   # DISCOVER is multicast only
        h.source()
        before=(h.node.state,h.node.source,h.node.owner,h.node.generation,h.node.fault,
                dict(h.node.values),h.driver.apply_calls)
        active=h.send(Command.DISCOVER,dst=ALL_NODES)
        self.assertEqual(active.kind,Kind.RESPONSE)
        self.assertEqual(before,(h.node.state,h.node.source,h.node.owner,h.node.generation,h.node.fault,
                                 dict(h.node.values),h.driver.apply_calls))
    def test_discovery_all_states_is_readonly(self):
        h=Harness();seen=set()
        def probe():
            # Snapshot all persistent control state, including cache, timers, actions and outputs.
            # Normal tick transitions have already run at this same monotonic time.
            h.node.tick(h.now,False)
            node={k:deepcopy(v) for k,v in vars(h.node).items()
                  if k not in ('profile','driver','reply','reply_at','reply_until')}
            driver=deepcopy(vars(h.driver));seen.add(h.node.state)
            self.assertEqual(h.send(Command.DISCOVER,dst=ALL_NODES,session=999).kind,Kind.RESPONSE)
            self.assertEqual(node,{k:v for k,v in vars(h.node).items() if k in node})
            self.assertEqual(driver,vars(h.driver))
        probe()  # SAFE
        h.driver.safe_delay_us=100
        h.source();self.assertEqual(h.node.state,State.SWITCHING);probe()
        h.now=100;h.node.tick(h.now);h.target();h.node.tick(h.now);probe()  # ACTIVE, real target
        h.send(Command.ESTOP)
        self.assertEqual(h.node.state,State.STOPPING);probe()
        h.now=200;h.node.tick(h.now);probe()  # FAULT
        self.assertEqual(seen,set(State))
        h=Harness();h.source()
        h.send(Command.EXECUTE_ACTION,h.guarded()+struct.pack('<H',1))
        self.assertNotEqual(h.node.operation,0);probe()  # A running action must not be cancelled.
    def test_discovery_does_not_renew_target_or_lease(self):
        for target in (False,True):
            h=Harness();h.source()
            if target:h.target()
            for at in range(0,(100000 if target else 300000),5000):
                h.now=at;h.node.tick(at)
                self.assertEqual(h.send(Command.DISCOVER,dst=ALL_NODES).kind,Kind.RESPONSE)
            h.step(100000 if target else 300000)
            self.assertEqual(h.node.fault,Fault.TARGET_EXPIRED if target else Fault.SOURCE_TIMEOUT)
    def test_uid_addressing_and_multicast_scope(self):
        h=Harness(uid=7)
        self.assertIsNone(h.send(Command.INFO,dst=8))
        self.assertIsNone(h.send(Command.INFO,dst=ALL_NODES))
        self.assertIsNone(h.send(Command.SESSION_OPEN,struct.pack('<QQI',10,h.node.session_epoch,1),dst=ALL_NODES,session=5))
        self.assertIsNone(h.node.transact(encode(Frame(Kind.REQUEST,9,7,99,Command.INFO,100)),0))
        info=h.send(Command.INFO)
        self.assertEqual(struct.unpack('<QHIQBHQ',info.payload),(7,h.node.profile.device_type,1,10,0x22,1,h.node.session_epoch))
        self.assertEqual((info.src_uid,info.dst_uid),(7,0))
        self.assertEqual(struct.unpack_from('<H',h.send(3,h.guarded()).payload)[0],Error.UNSUPPORTED)
    def test_session_open_is_unicast_and_guarded(self):
        h=Harness();epoch=h.node.session_epoch
        for payload,code in ((struct.pack('<QQQI',1,10,epoch,1),Error.MALFORMED),(struct.pack('<QQI',11,epoch,1),Error.BOOT),
                             (struct.pack('<QQI',10,epoch+1,1),Error.EPOCH),(struct.pack('<QQI',10,epoch,2),Error.GENERATION)):
            self.assertEqual(struct.unpack_from('<H',h.send(Command.SESSION_OPEN,payload,session=200).payload)[0],code)
        self.assertEqual(h.send(Command.SESSION_OPEN,struct.pack('<QQI',10,epoch,1),session=200).kind,Kind.ACK)
        self.assertEqual((h.node.session_id,h.node.session_epoch),(200,epoch+1))

class DeviceTests(unittest.TestCase):
    def test_latest_targets_are_coalesced(self):
        h=Harness();h.source();h.target([(1,1,100)]);h.target([(1,1,200)])
        self.assertEqual(h.driver.apply_calls,0)
        h.node.tick(1);self.assertEqual(h.driver.outputs[(1,1)],200);self.assertEqual(h.driver.apply_calls,1)
    def test_retry_does_not_refresh_timers(self):
        h=Harness();h.source();raw=h.raw(Command.WRITE_TARGETS,targets(10,h.node.generation,[(1,1,50)],h.node.session_epoch))
        a=h.node.transact(raw,0);h.node.tick(0);h.step(90000)
        self.assertEqual(h.node.transact(raw,90000),a);h.step(100000)
        self.assertEqual(h.node.fault,Fault.TARGET_EXPIRED)
    def test_sequence_conflict_old_and_exhaustion(self):
        h=Harness();raw=h.raw(Command.INFO);h.node.transact(raw,0)
        same_seq=h.raw(Command.STATUS,b'\x00\x00',seq=h.seq)
        self.assertEqual(struct.unpack_from('<H',h.node.transact(same_seq,1),HEADER_SIZE)[0],Error.CONFLICT)
        h.now=1
        h.seq=0xfffffffe;h.send(Command.INFO)
        self.assertEqual(struct.unpack_from('<H',h.send(Command.INFO,seq=1).payload)[0],Error.SEQUENCE)
    def test_readonly_sessions_do_not_consume_capacity(self):
        h=Harness()
        for sid in range(1,9):self.assertEqual(h.send(Command.INFO,session=sid).kind,Kind.RESPONSE)
        self.assertEqual(h.send(Command.INFO,session=9).kind,Kind.RESPONSE)
        self.assertEqual(len(h.node.sessions),1)
        self.assertEqual(h.send(Command.INFO,session=1).kind,Kind.RESPONSE)
    def test_atomic_range_and_coordination(self):
        h=Harness(gimbal());h.source()
        self.assertEqual(struct.unpack_from('<H',h.target([(1,1,1)]).payload)[0],Error.GROUP)
        self.assertFalse(h.node.values)
        self.assertEqual(struct.unpack_from('<H',h.target([(1,1,1),(1,2,200000)]).payload)[0],Error.RANGE)
        self.assertFalse(h.node.values)
        self.assertEqual(h.target([(1,1,100),(1,2,200)]).kind,Kind.ACK)
        h.node.tick(1);self.assertEqual(len(h.driver.outputs),2)
    def test_private_validation_sees_pending_candidate(self):
        h=Harness();h.source()
        h.driver.validate_targets=lambda values: sum(values.values())<=150
        self.assertEqual(h.target([(1,1,100)]).kind,Kind.ACK)
        self.assertEqual(struct.unpack_from('<H',h.target([(1,2,100)]).payload)[0],Error.RANGE)
        self.assertEqual(h.node.values,{0:100})
    def test_first_target_driver_deadline(self):
        h=Harness();h.source();h.target();h.now=10001
        h.send(Command.STATUS,b'\0\0')
        self.assertEqual(h.node.fault,Fault.OUTPUT_GAP)
        self.assertEqual(h.driver.apply_calls,0)
    def test_action_validation_and_failed_start_cancel(self):
        h=Harness();h.source();h.driver.fail_action_validation=True
        self.assertEqual(struct.unpack_from('<H',h.send(Command.EXECUTE_ACTION,h.guarded()+struct.pack('<H',1)).payload)[0],Error.RANGE)
        self.assertEqual(h.driver.action_calls,0)
        h.driver.fail_action_validation=False;h.driver.action_fail=True
        h.send(Command.EXECUTE_ACTION,h.guarded()+struct.pack('<H',1))
        self.assertEqual(h.node.fault,Fault.DRIVER);self.assertFalse(h.driver.actions)
    def test_limit_trip_is_a_distinct_latched_fault(self):
        # 设备的物理限位开关触发、驱动器自行停车并报警：与通用 DRIVER 区分，主控据此按类别契约离开限位。
        h=Harness();h.source();h.node.fault_input(1,Fault.LIMIT_TRIP);h.node.tick(2)
        self.assertEqual(h.node.fault,Fault.LIMIT_TRIP);self.assertEqual(h.node.state,State.FAULT)
        self.assertEqual(int(Fault.LIMIT_TRIP),11)
        h.node.fault_input(3,Fault.DRIVER);self.assertEqual(h.node.fault,Fault.LIMIT_TRIP)   # 首个原因保持锁存
    def test_non_owner_and_independent_nodes(self):
        a,b=Harness(),Harness(gimbal(),2,20)
        a.source();b.source(Source.REMOTE)
        self.assertEqual(struct.unpack_from('<H',b.target([(1,1,0),(1,2,0)]).payload)[0],Error.OWNER)
        local=LocalControlInput(b.node)
        self.assertEqual(local.targets(b.node.session_epoch,b.node.generation,1,[(1,1,0),(1,2,0)],0),Error.OK)
        a.node.fault_input(1);self.assertEqual(b.node.state,State.ACTIVE)
        self.assertEqual(b.node.source,Source.REMOTE)
    def test_boot_generation_and_old_session(self):
        h=Harness();h.source();old_generation=h.node.generation
        h.send(Command.STOP);h.node.tick(0)
        self.assertEqual(struct.unpack_from('<H',h.send(Command.HEARTBEAT,guard(10,old_generation,h.node.session_epoch)).payload)[0],Error.GENERATION)
        self.assertEqual(struct.unpack_from('<H',h.send(Command.SET_SOURCE,guard(999,h.node.generation,h.node.session_epoch)+struct.pack('<BII',2,100000,300000)).payload)[0],Error.BOOT)
        h.source();self.assertEqual(struct.unpack_from('<H',h.send(Command.HEARTBEAT,h.guarded(),session=200).payload)[0],Error.SESSION)
    def test_switch_failure_timeout_and_flush(self):
        h=Harness();h.driver.safe_delay_us=50000;h.source()
        self.assertEqual(h.node.state,State.SWITCHING)
        self.assertEqual(struct.unpack_from('<H',h.target().payload)[0],Error.OWNER)
        h.step(50000);self.assertEqual(h.node.state,State.ACTIVE);self.assertEqual(h.driver.flush_calls,2)
        h=Harness();h.driver.safe_delay_us=200000;h.source();h.step(100000)
        self.assertEqual(h.node.fault,Fault.SWITCH_TIMEOUT)
        h=Harness();h.driver.safe_fail=True;h.source();self.assertEqual(h.node.fault,Fault.DRIVER)
    def test_heartbeat_query_not_target_freshness(self):
        h=Harness();h.source();h.target();h.node.tick(0)
        h.step(90000);h.send(Command.HEARTBEAT,h.guarded());h.send(Command.STATUS,b'\0\0');h.step(100000)
        self.assertEqual(h.node.fault,Fault.TARGET_EXPIRED)
    def test_source_loss_without_targets(self):
        h=Harness();h.source();h.step(300000);self.assertEqual(h.node.fault,Fault.SOURCE_TIMEOUT)
        h.node.tick(300001);self.assertEqual(h.node.state,State.FAULT);self.assertEqual(h.node.source,Source.NONE)
    def test_action_dedupe_cancel_busy_and_timeout(self):
        h=Harness();h.source();raw=h.raw(Command.EXECUTE_ACTION,h.guarded()+struct.pack('<H',1))
        result=h.node.transact(raw,0);h.node.transact(raw,10);h.now=10;self.assertEqual(h.driver.action_calls,1)
        self.assertEqual(struct.unpack_from('<H',h.target().payload)[0],Error.BUSY)
        h.step(20000);op=ACK.unpack(decode(result).payload)[-2]
        self.assertEqual(h.node.operation_results[op][0],Outcome.SUCCESS)
        h.send(Command.EXECUTE_ACTION,h.guarded()+struct.pack('<H',1));op=h.node.operation;h.send(Command.STOP)
        self.assertEqual(h.node.operation_results[op][0],Outcome.CANCELLED)
        h=Harness();h.driver.action_delay_us=999999;h.source();h.send(Command.EXECUTE_ACTION,h.guarded()+struct.pack('<H',1))
        for t in (100000,200000,300000,400000):h.now=t;h.send(Command.HEARTBEAT,h.guarded())
        h.now=500000;h.node.tick(h.now);self.assertEqual(h.node.fault,Fault.ACTION_TIMEOUT)
    def test_stop_no_deadline_extension_reset_safety(self):
        h=Harness();h.source();h.driver.safe_delay_us=200000;h.send(Command.STOP);calls=h.driver.safe_calls
        h.now=90000;h.send(Command.STOP);self.assertEqual(h.driver.safe_calls,calls)
        h.node.tick(100000);h.now=100000;self.assertEqual(h.node.fault,Fault.STOP_TIMEOUT)
        self.assertEqual(struct.unpack_from('<H',h.send(Command.RESET_FAULT,h.guarded()).payload)[0],Error.SAFETY)
        h.now=200000;self.assertEqual(h.send(Command.RESET_FAULT,h.guarded()).kind,Kind.ACK)
        self.assertEqual(h.node.source,Source.NONE)
    def test_estop_broadcast_and_reset(self):
        h=Harness();h.source();self.assertIsNone(h.send(Command.ESTOP,dst=ALL_NODES));h.node.tick(0)
        self.assertEqual(h.node.state,State.FAULT);h.driver.reset_ok=False
        self.assertEqual(struct.unpack_from('<H',h.send(Command.RESET_FAULT,h.guarded()).payload)[0],Error.SAFETY)
        h.driver.reset_ok=True;h.send(Command.RESET_FAULT,h.guarded());self.assertEqual(h.node.state,State.SAFE)
    def test_reply_is_immediate_and_never_sent_late(self):
        h=Harness();raw=h.raw(Command.INFO)
        self.assertTrue(h.node.receive(raw,0));self.assertIsNotNone(h.node.take_reply(0));self.assertIsNone(h.node.take_reply(1))
        self.assertTrue(h.node.receive(raw,100));self.assertIsNone(h.node.take_reply(100+REPLY_HOLD_US+1))
        # A newer datagram replaces an untaken reply; no request is refused while one is pending.
        self.assertTrue(h.node.receive(raw,200000));self.assertTrue(h.node.receive(h.raw(Command.STATUS,b'\0\0'),200001))
        self.assertEqual(decode(h.node.take_reply(200001)).command,Command.STATUS)
        self.assertFalse(h.node.receive(h.raw(Command.STOP,dst=ALL_NODES),200002));self.assertIsNone(h.node.take_reply(200002))
    def test_sparse_targets_on_non_continuous_resource_do_not_trip_output_gap(self):
        # flags bit0=0 的资源（电平类设备）：交付后不再被服务，远晚于 update_gap 的下一个目标必须正常受理（稀疏写入）。
        import dataclasses
        p=lens();p.resources=[dataclasses.replace(r,flags=0) for r in p.resources]
        h=Harness(p);h.source();self.assertEqual(h.target().kind,Kind.ACK);h.node.tick(0);self.assertEqual(h.driver.apply_calls,1)
        h.node.tick(p.update_gap_us*5);self.assertEqual(h.node.fault,Fault.NONE) # 没有待交付目标时不判 gap
        h.now=200000;self.assertEqual(h.target([(1,1,600)]).kind,Kind.ACK);h.node.tick(200000)
        self.assertEqual(h.node.fault,Fault.NONE);self.assertEqual(h.driver.apply_calls,2);self.assertEqual(h.driver.outputs[(1,1)],600)
        # 受理后仍须在 update_gap 内交付。
        h.now=250000;self.assertEqual(h.target([(1,1,700)]).kind,Kind.ACK);h.node.tick(250000+p.update_gap_us+1)
        self.assertEqual(h.node.fault,Fault.OUTPUT_GAP)
    def test_output_gap_and_driver_fault(self):
        h=Harness();h.source();h.target();h.node.tick(0);h.node.tick(10001)
        self.assertEqual(h.node.fault,Fault.OUTPUT_GAP)
        h=Harness();h.source();h.target();h.driver.apply_fail=True;h.node.tick(0);self.assertEqual(h.node.fault,Fault.DRIVER)
    def test_local_input_generation_sequence(self):
        h=Harness();h.source(Source.REMOTE);local=LocalControlInput(h.node)
        gen=h.node.generation;self.assertEqual(local.targets(h.node.session_epoch,gen,1,[(1,1,30)],0),Error.OK)
        self.assertEqual(local.targets(h.node.session_epoch,gen,1,[(1,1,40)],1),Error.SEQUENCE)
        h.send(Command.STOP);h.node.tick(2)
        self.assertEqual(local.targets(h.node.session_epoch,gen,2,[(1,1,50)],2),Error.GENERATION)
    def test_caps_status_and_no_fake_feedback(self):
        h=Harness(fans())
        for i in range(11):self.assertEqual(h.send(Command.CAPS,struct.pack('<H',i)).kind,Kind.RESPONSE)
        s=h.send(Command.STATUS,b'\0\0');self.assertEqual(s.payload[55],8)
        self.assertEqual(struct.unpack_from('<H',s.payload,53)[0],8)
        self.assertFalse(s.payload[80]&2)
        s=h.send(Command.STATUS,struct.pack('<H',8));self.assertEqual(s.payload[55],1)

class TimeCodeTests(unittest.TestCase):
    def test_wire_unchanged_and_local_fallback(self):
        packet=Packet(1,0,1000000);self.assertEqual(tc_decode(tc_encode(packet)),packet)
        h=HostTimeline();self.assertTrue(h.observe(packet,0,260));self.assertEqual(h.start(1000),1001000)
        self.assertEqual(h.now(40000),1040000);self.assertTrue(h.fallback)
        h.observe(Packet(2,0,500),40000,40260)
        self.assertEqual(h.now(41000),1041000)
        h.stop();self.assertEqual(h.start(41000),1500);self.assertFalse(h.fallback)
    def test_duplicates_do_not_restore_age(self):
        h=HostTimeline();p=Packet(1,1,10000);h.observe(p,1000,1260)
        self.assertFalse(h.observe(p,2000,2260));self.assertEqual(h.observed,1260)

if __name__=='__main__':unittest.main()
