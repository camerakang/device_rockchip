"""设备本地超时恢复；不发送RESET_FAULT，不隐式接管。"""
import json
import struct
import unittest
from pathlib import Path
from ..test_reference import Harness
from ..reference import Fault,State,Source
from ..wire import Command as C,Kind,Frame,encode


def recovery_vectors():
    rows=[]
    for timeout in (Fault.TARGET_EXPIRED,Fault.SOURCE_TIMEOUT):
        h=Harness();h.node.profile.auto_reset_mask=6
        steps=[]
        # Harness已经建立会话；向量从同样的全新核心重放完整SESSION_OPEN。
        opening=struct.pack('<QQI',10,1,1)
        opening_raw=encode(Frame(Kind.REQUEST,0,1,1,C.SESSION_OPEN,100,opening))
        steps.append(dict(at=0,op='request',request=opening_raw.hex(),reply=h.node.transact(opening_raw,0).hex()))
        def send(command,payload=b''):
            raw=h.raw(command,payload);reply=h.node.transact(raw,h.now)
            steps.append(dict(at=h.now,op='request',request=raw.hex(),reply=reply.hex()))
            return raw
        send(C.SET_SOURCE,h.guarded()+struct.pack('<BII',2,100000,300000))
        h.node.tick(0);steps.append(dict(at=0,op='tick'))
        old_guard=h.guarded()
        if timeout==Fault.TARGET_EXPIRED:
            from ..wire import targets
            send(C.WRITE_TARGETS,targets(10,h.node.generation,[(1,1,500)],h.node.session_epoch))
            for at in range(0,100001,5000):
                h.node.tick(at);steps.append(dict(at=at,op='tick'))
            h.now=100001
        else:
            h.now=300001;h.node.tick(h.now);steps.append(dict(at=h.now,op='tick'))
        h.node.tick(h.now);steps.append(dict(at=h.now,op='tick'))
        assert h.node.fault==timeout
        assert h.node.recover_timeout_fault(h.now)
        steps.append(dict(at=h.now,op='recover',result=True))
        send(C.STATUS,b'\x00\x00')
        send(C.HEARTBEAT,old_guard)
        assert h.node.state==State.SAFE and h.node.owner==0
        rows.append(dict(name=timeout.name,steps=steps))
    return rows


class TimeoutRecoveryTests(unittest.TestCase):
    def timeout(self):
        h=Harness();h.node.profile.auto_reset_mask=6;h.source();h.target()
        h.driver.safe_delay_us=10000;h.step(100000)
        return h

    def test_waits_for_safety_then_clears_only_once(self):
        h=self.timeout();self.assertFalse(h.node.recover_timeout_fault(h.now))
        h.step(110000);h.driver.reset_ok=False
        self.assertFalse(h.node.recover_timeout_fault(h.now))
        h.driver.reset_ok=True;generation=h.node.generation
        self.assertTrue(h.node.recover_timeout_fault(h.now))
        self.assertEqual(h.node.generation,generation+1)
        self.assertEqual((h.node.state,h.node.source,h.node.owner,h.node.fault),(State.SAFE,Source.NONE,0,Fault.NONE))
        self.assertFalse(h.node.values or h.node.pending or h.node.active)
        self.assertFalse(h.node.recover_timeout_fault(h.now));self.assertEqual(h.node.generation,generation+1)
        h.step(900000);self.assertEqual(h.node.state,State.SAFE)

    def test_disabled_mask_and_exhausted_generation_never_clear(self):
        h=self.timeout();h.step(110000);h.node.profile.auto_reset_mask=0
        self.assertFalse(h.node.recover_timeout_fault(h.now))
        h.node.profile.auto_reset_mask=6;h.node.generation=0xffffffff
        self.assertFalse(h.node.recover_timeout_fault(h.now))
        self.assertEqual(h.node.fault,Fault.TARGET_EXPIRED)

    def test_hardware_and_estop_override_timeout_during_stop(self):
        for fault in (Fault.DRIVER,Fault.ESTOP,Fault.LOCAL_CLOCK,Fault.OUTPUT_GAP):
            with self.subTest(fault=fault):
                h=self.timeout();h.node.fault_input(100001,fault);h.step(120000)
                self.assertEqual(h.node.fault,fault)
                self.assertFalse(h.node.recover_timeout_fault(h.now))

    def test_unconfirmed_stop_never_recovers_even_if_output_later_recovers(self):
        h=self.timeout();h.driver.safe_delay_us=200000;h.step(201000)
        self.assertEqual(h.node.fault,Fault.STOP_TIMEOUT)
        h.step(400000);self.assertFalse(h.node.recover_timeout_fault(h.now))

    def test_faults_not_in_allowlist_remain_latched(self):
        for fault in Fault:
            if fault in (Fault.NONE,Fault.TARGET_EXPIRED,Fault.SOURCE_TIMEOUT):continue
            h=Harness();h.node.profile.auto_reset_mask=6;h.node.fault_input(0,fault)
            self.assertFalse(h.node.recover_timeout_fault(1000000));self.assertEqual(h.node.fault,fault)

    def test_saved_vectors(self):
        saved=json.loads((Path(__file__).parents[1]/'timeout_recovery_vectors.json').read_text())
        self.assertEqual(saved,recovery_vectors())


if __name__=='__main__':unittest.main()
