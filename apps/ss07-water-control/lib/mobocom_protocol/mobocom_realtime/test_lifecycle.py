"""V2.2生命周期与防重放；只用虚拟驱动，不作为硬件安全参数。"""
import struct
import unittest
from .wire import Frame, Kind, Command as C, Error as E, encode, decode, guard, targets
from .reference import DeviceCore, VirtualDriver, State, Source, Fault
from .profiles import fans


class LifecycleTests(unittest.TestCase):
    def node(self):
        return DeviceCore(fans(), 2, 10, VirtualDriver())

    def call(self, n, sid, seq, cmd, payload=b'', now=0):
        raw = encode(Frame(Kind.REQUEST, 0, n.uid, seq, cmd, sid, payload))
        return raw, decode(n.transact(raw, now))

    def open(self, n, sid, now=0):
        return self.call(n, sid, 1, C.SESSION_OPEN,
                         struct.pack('<QQI', n.boot_id, n.session_epoch, n.generation), now)

    def test_ten_thousand_rotations_and_retired_replay(self):
        n = self.node()
        retired = None
        for sid in range(1, 10001):
            raw, f = self.open(n, sid)
            self.assertEqual(f.kind, Kind.ACK)
            self.assertEqual(n.transact(raw, 0), encode(f))
            self.assertEqual(n.session_epoch, sid + 1)
            self.assertEqual(len(n.sessions), 1)
            _, reply = self.call(n, sid + 100000, 1, C.INFO)
            self.assertEqual(reply.kind, Kind.RESPONSE)
            _, reply = self.call(n, sid, 2, C.STOP, guard(10, n.generation, n.session_epoch))
            self.assertEqual(reply.kind, Kind.ACK)
            if retired:
                self.assertEqual(decode(n.transact(retired, 0)).kind, Kind.ERROR)
            retired = raw
        self.assertEqual(n.boot_id, 10)
        self.assertEqual((n.state, n.source, n.fault), (State.SAFE, Source.NONE, Fault.NONE))

    def test_old_targets_stop_and_open_cannot_change_new_context(self):
        n = self.node()
        old_open, _ = self.open(n, 100)
        old_guard = guard(10, n.generation, n.session_epoch)
        old_targets = encode(Frame(Kind.REQUEST, 0, n.uid, 3, C.WRITE_TARGETS, 100,
                                   targets(10, n.generation, [(1, 1, 100)], n.session_epoch)))
        old_stop = encode(Frame(Kind.REQUEST, 0, n.uid, 4, C.STOP, 100, old_guard))
        self.open(n, 200)
        epoch = n.session_epoch
        for raw in (old_open, old_targets, old_stop):
            f = decode(n.transact(raw, 0))
            self.assertEqual(f.kind, Kind.ERROR)
            self.assertEqual(n.session_epoch, epoch)
            self.assertEqual(n.state, State.SAFE)
            self.assertFalse(n.values)

    def test_generation_boundary_renews_without_boot(self):
        n = self.node()
        self.open(n, 100)
        n.generation = 0xffffffff
        self.call(n, 100, 2, C.STOP, guard(10, n.generation, n.session_epoch))
        self.assertEqual(n.state, State.SAFE)
        self.open(n, 200)
        self.assertEqual(n.generation, 1)
        self.assertEqual(n.boot_id, 10)

    def test_safety_failure_overrides_recoverable_reason(self):
        n = self.node()
        n.profile.auto_reset_mask = 6
        self.open(n, 100)
        self.call(n, 100, 2, C.SET_SOURCE,
                  guard(10, n.generation, n.session_epoch) + struct.pack('<BII', 2, 100000, 300000))
        n.tick(0)
        n.driver.safe_fail = True
        n.fault_input(1, Fault.TARGET_EXPIRED)
        self.assertEqual(n.fault, Fault.DRIVER)
        self.assertFalse(n.profile.auto_reset_mask & (1 << n.fault))

    def test_local_old_epoch_is_rejected_after_renewal(self):
        n = self.node()
        epoch = n.session_epoch
        self.open(n, 100)
        self.assertEqual(n.local_targets(epoch, 1, 1, [(1, 1, 0)], 0), E.EPOCH)

    def test_nonowner_can_clear_only_safe_fault_with_current_guard(self):
        n = self.node()
        self.open(n, 100)
        n.fault_input(0, Fault.TARGET_EXPIRED)
        raw, f = self.call(n, 200, 1, C.RESET_FAULT, guard(10, n.generation, n.session_epoch))
        self.assertEqual(f.kind, Kind.ACK)
        self.assertEqual(n.transact(raw, 0), encode(f))
        self.assertEqual(n.state, State.SAFE)
        self.assertEqual(n.owner, 0)

    def test_sixteen_nodes_independent_owner_queues_faults_and_clocks(self):
        # 网络寻址只用UID；index只用于测试内部的序号和故障节点选择。
        nodes = [DeviceCore(fans(), i + 100, i + 200, VirtualDriver()) for i in range(1, 17)]
        index = {n.uid: i + 1 for i, n in enumerate(nodes)}
        sequences = [0] * 16
        def send(n, cmd, payload, now):
            i=index[n.uid]-1;sequences[i]+=1
            raw=encode(Frame(Kind.REQUEST,0,n.uid,sequences[i],cmd,1000,payload))
            f=decode(n.transact(raw,now));self.assertEqual(f.kind,Kind.ACK)
        for n in nodes:
            send(n,C.SESSION_OPEN,struct.pack('<QQI',n.boot_id,1,1),0)
            send(n,C.SET_SOURCE,guard(n.boot_id,1,n.session_epoch)+struct.pack('<BII',2,100000,300000),0)
            n.tick(0)
        for now in range(0,2000001,5000):
            for n in nodes:
                k=index[n.uid]
                if k==8 and now>=250000:
                    if now==250000:n.fault_input(now,Fault.DRIVER)
                elif now%10000==0:
                    values=[(r.endpoint,r.resource,k) for r in n.profile.resources]
                    send(n,C.WRITE_TARGETS,targets(n.boot_id,n.generation,values,n.session_epoch),now)
                n.tick(now)
                if k!=8:self.assertEqual((n.state,n.source,n.owner),(State.ACTIVE,Source.MOBOCOM,1000))
                elif now>250000:self.assertEqual(n.state,State.FAULT)
        self.assertTrue(all(n.boot_id==index[n.uid]+200 for n in nodes))
        self.assertEqual(nodes[7].fault,Fault.DRIVER)
        self.assertTrue(all(n.values and len(n.sessions)==1 for n in nodes if index[n.uid]!=8))
