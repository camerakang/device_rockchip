"""Deterministic device behavior. All time arguments are local monotonic microseconds.

Network binding v0x22: the node is addressed by its UID; the adapter maps the
datagram endpoint. Replies are available immediately after receive().
"""
from dataclasses import dataclass, field
from enum import IntEnum
import struct
from .wire import Frame, Kind, Command as C, Error as E, ALL_NODES, VERSION, decode, encode
from .device_types import is_registered_device_type
from .motion_profile import MOTION_PROFILE_SCHEMA, LIVE_MOTION_PROFILE_SCHEMA
from .focus_control import FOCUS_CONTROL_SCHEMA

# A reply not taken by the network adapter within this time is dropped, never sent late.
REPLY_HOLD_US = 20_000

class Source(IntEnum):
    NONE=0
    REMOTE=1
    MOBOCOM=2
class State(IntEnum):
    SAFE=0
    SWITCHING=1
    ACTIVE=2
    STOPPING=3
    FAULT=4
class Fault(IntEnum):
    NONE=0
    SOURCE_TIMEOUT=1
    TARGET_EXPIRED=2
    ACTION_TIMEOUT=3
    DRIVER=4
    STOP_TIMEOUT=5
    SWITCH_TIMEOUT=6
    ESTOP=7
    LOCAL_CLOCK=8
    OUTPUT_GAP=9
    GENERATION_EXHAUSTED=10
    LIMIT_TRIP=11
class Outcome(IntEnum):
    RUNNING=1
    SUCCESS=2
    FAILED=3
    CANCELLED=4

ACK = struct.Struct('<QIBBHIIQQ')
STATUS = struct.Struct('<QIIBBHQIIQIHHBHBQ')
ENTRY = struct.Struct('<HHiiIB')
INFO = struct.Struct('<QHIQBHQ')       # uid,type,firmware,boot,wire_version,profile_version,epoch
DISCOVERY = struct.Struct('<HIQQI')    # type,firmware,boot,epoch,generation; UID is src_uid

@dataclass(frozen=True)
class Resource:
    endpoint:int
    resource:int
    unit:int
    minimum:int
    maximum:int
    flags:int=1 # bit0 continuous, bit1 actual feedback supported
    group:int=0
    vmax:int=0
    amax:int=0

@dataclass(frozen=True)
class Action:
    opcode:int
    resource_mask:int
    parameter_bytes:int=0
    timeout_us:int=500_000

@dataclass
class Profile:
    # 使用公共DeviceType成员；实例身份由DeviceCore的uid管理，网络位置由适配器管理。
    device_type:int
    resources:list[Resource]
    actions:list[Action]=field(default_factory=list)
    version:int=1
    firmware:int=1
    source_mask:int=6
    max_target_hz:int=100
    update_gap_us:int=10_000
    target_min_us:int=20_000
    target_max_us:int=1_000_000
    lease_min_us:int=50_000
    lease_max_us:int=3_000_000
    safe_timeout_us:int=100_000
    switch_timeout_us:int=100_000
    auto_reset_mask:int=0

    def validate(self):
        # 与C++节点初始化保持一致：合法u16空位也不能擅自用作新设备类型。
        # 登记只确认类别身份；资源、范围、来源和安全期限仍独立校验。
        assert is_registered_device_type(self.device_type), 'unregistered device type: use the public registry'
        assert not self.auto_reset_mask & ~6
        assert self.version and self.firmware
        assert 0 < len(self.resources) <= 16 and len(self.actions) <= 16
        assert self.source_mask and not self.source_mask & ~6
        assert 0 < self.target_min_us <= self.target_max_us and 0 < self.lease_min_us <= self.lease_max_us
        assert self.safe_timeout_us and self.switch_timeout_us and self.update_gap_us and self.max_target_hz
        assert len({(r.endpoint,r.resource) for r in self.resources}) == len(self.resources)
        assert all(r.endpoint and r.resource and r.unit and r.minimum <= r.maximum and not r.flags & ~3 for r in self.resources)
        assert len({a.opcode for a in self.actions}) == len(self.actions)
        assert all(a.opcode and a.timeout_us and a.parameter_bytes <= 218 and not a.resource_mask >> len(self.resources) for a in self.actions)

class VirtualDriver:
    """Explicitly virtual delays/outputs. Never a production safety default."""
    def __init__(self):
        self.safe_delay_us=0
        self.safe_started=0
        self.safe_calls=0
        self.safe_fail=False
        self.apply_fail=False
        self.action_fail=False
        self.action_delay_us=20_000
        self.actions={}
        self.outputs={}
        self.feedback={}
        self.apply_calls=0
        self.action_calls=0
        self.flush_calls=0
        self.reset_ok=True
        self.fail_validation=False
        self.fail_action_validation=False

    def safe(self, now, reason=Fault.NONE):
        self.safe_started=now
        self.outputs.clear()
        self.safe_calls+=1
        return not self.safe_fail
    def safe_confirmed(self, now):
        return not self.safe_fail and now-self.safe_started >= self.safe_delay_us
    def apply(self, values, now):
        self.apply_calls+=1
        if self.apply_fail: return False
        self.outputs.update(values)
        return True
    def validate_targets(self, values): return not self.fail_validation
    def validate_action(self,spec,data):return not self.fail_action_validation
    def start_action(self, spec, data, op, now):
        self.action_calls+=1
        self.actions[op]=(now, spec)
        return not self.action_fail
    def poll_action(self, op, now):
        return Outcome.SUCCESS if now-self.actions[op][0] >= self.action_delay_us else Outcome.RUNNING
    def cancel(self, op): self.actions.pop(op,None)
    def flush_remote(self): self.flush_calls+=1

class DeviceCore:
    SESSION_LIMIT=1
    ACTION_HISTORY=8
    def __init__(self, profile:Profile, uid:int, boot_id:int, driver:VirtualDriver):
        profile.validate()
        if not 0 < uid <= 0xffffffffffffffff or not 0 < boot_id <= 0xffffffffffffffff: raise ValueError('identity')
        self.profile,self.uid,self.boot_id,self.driver=profile,uid,boot_id,driver
        self.state=State.SAFE
        self.source=Source.NONE
        self.owner=0
        self.generation=1
        self.state_version=1
        self.fault=Fault.NONE
        self.sessions={}
        self.session_epoch=1
        self.session_id=0
        self.opening=None
        self.safety=None
        self.configuration=None
        self.configuration_cache=None
        self.received={}
        self.values={}
        self.pending=set()
        self.active=set()
        self.accepted_sequence=self.applied_sequence=0
        self.last_apply=None
        self.lease_until=0
        self.target_ttl=self.lease_ttl=0
        self.transition_start=0
        self.next_source=Source.NONE
        self.next_owner=0
        self.last_now=0
        self.local_sequence=0
        self.next_operation=1
        self.operation=0
        self.operation_started=0
        self.operation_spec=None
        self.operation_results={}
        self.reply=None
        self.reply_at=self.reply_until=0

    def _changed(self): self.state_version=(self.state_version+1)&0xffffffff
    def _advance_generation(self):
        if self.generation==0xffffffff:
            return False
        self.generation+=1
        self.local_sequence=0
        return True
    def _ack(self):
        return ACK.pack(self.boot_id,self.generation,self.state,self.source,self.fault,
                        self.accepted_sequence,self.applied_sequence,self.operation,self.session_epoch)
    def _error(self,e,detail=0): return Kind.ERROR,struct.pack('<HH',e,detail)+self._ack()
    def _clear(self):
        self.pending.clear();self.active.clear();self.received.clear();self.values.clear();self.last_apply=None
        if self.operation:
            self.driver.cancel(self.operation)
            self.operation_results[self.operation]=(Outcome.CANCELLED,E.OK,0)
            self.operation=0;self.operation_spec=None
        self.source=Source.NONE;self.owner=0;self.lease_until=0
        self.accepted_sequence=self.applied_sequence=0
    def _stop(self,now,reason=Fault.NONE):
        timeout=self.fault in (Fault.SOURCE_TIMEOUT,Fault.TARGET_EXPIRED)
        if reason==Fault.ESTOP or reason and (not self.fault or
                timeout and reason not in (Fault.SOURCE_TIMEOUT,Fault.TARGET_EXPIRED)):
            self.fault=reason
        if self.state==State.STOPPING:
            return # never restart safety or extend deadline
        if self.state==State.FAULT:
            return
        if self.state==State.SAFE:
            if self.fault: self.state=State.FAULT
            self._changed()
            return
        self._clear();self.next_source=Source.NONE;self.next_owner=0
        self._advance_generation()
        self.state=State.STOPPING;self.transition_start=now;self._changed()
        if not self.driver.safe(now,reason): self.fault=Fault.DRIVER

    def fault_input(self,now,reason=Fault.DRIVER):
        self.tick(now,False);self._stop(now,reason);self.tick(now,False)

    def recover_timeout_fault(self,now):
        """设备任务显式启用的本地超时复位；不恢复队列、来源或持有者。"""
        if (now < self.last_now or (self.configuration and self.configuration.busy()) or self.state!=State.FAULT or
                self.fault not in (Fault.SOURCE_TIMEOUT,Fault.TARGET_EXPIRED) or
                not self.profile.auto_reset_mask & (1 << self.fault) or
                self.source!=Source.NONE or self.owner or self.active or self.pending or self.operation or
                self.generation==0xffffffff or not self.driver.safe_confirmed(now) or not self.driver.reset_ok):
            return False
        self.last_now=now;self._clear();self.next_source=Source.NONE;self.next_owner=0
        self._advance_generation();self.fault=Fault.NONE;self.state=State.SAFE;self._changed()
        return True

    def tick(self,now,apply_output=True):
        if now < self.last_now:
            self._stop(self.last_now,Fault.LOCAL_CLOCK)
            return
        self.last_now=now
        if self.state==State.SWITCHING:
            if now-self.transition_start >= self.profile.switch_timeout_us:
                self._stop(now,Fault.SWITCH_TIMEOUT)
            elif self.driver.safe_confirmed(now):
                if not self._advance_generation():self._stop(now);return
                self.source,self.owner=self.next_source,self.next_owner
                self.state=State.SAFE if self.source==Source.NONE else State.ACTIVE
                self.lease_until=now+self.lease_ttl if self.source else 0
                self.driver.flush_remote();self._changed()
        if self.state==State.STOPPING:
            if self.driver.safe_confirmed(now):
                self.state=State.FAULT if self.fault else State.SAFE;self._changed()
            elif now-self.transition_start >= self.profile.safe_timeout_us:
                self.fault=Fault.STOP_TIMEOUT
                self.state=State.FAULT;self._changed()
        if self.state!=State.ACTIVE:return
        if now>=self.lease_until:self._stop(now,Fault.SOURCE_TIMEOUT);return
        if any(now-self.received[i]>=self.target_ttl for i in self.active):
            self._stop(now,Fault.TARGET_EXPIRED);return
        if self.operation:
            op=self.operation
            if now-self.operation_started>=self.operation_spec.timeout_us:
                self.driver.cancel(op);self.operation_results[op]=(Outcome.FAILED,E.TIMEOUT,0)
                self.operation=0;self.operation_spec=None;self._stop(now,Fault.ACTION_TIMEOUT);return
            outcome=self.driver.poll_action(op,now)
            if outcome!=Outcome.RUNNING:
                self.operation_results[op]=(outcome,E.OK if outcome==Outcome.SUCCESS else E.DRIVER,100 if outcome==Outcome.SUCCESS else 0)
                self.operation=0;self.operation_spec=None;self._changed()
                if outcome!=Outcome.SUCCESS:self._stop(now,Fault.DRIVER);return
        if (self.active or self.pending) and self.last_apply is not None and now-self.last_apply > self.profile.update_gap_us:
            self._stop(now,Fault.OUTPUT_GAP);return
        # Applying latest targets is separate from accepting a command.
        if apply_output and (self.pending or self.active):
            indices=self.pending|self.active
            batch={(self.profile.resources[i].endpoint,self.profile.resources[i].resource):self.values[i] for i in indices}
            if not self.driver.apply(batch,now):self._stop(now,Fault.DRIVER);return
            if self.applied_sequence!=self.accepted_sequence:self._changed()
            self.applied_sequence=self.accepted_sequence;self.pending.clear();self.last_apply=now

    def _check_guard(self,p):
        if len(p)<20:return E.MALFORMED
        boot,gen,epoch=struct.unpack_from('<QIQ',p)
        return E.BOOT if boot!=self.boot_id else E.EPOCH if epoch!=self.session_epoch else E.GENERATION if gen!=self.generation else E.OK
    def _is_owner(self,session):
        return self.state==State.ACTIVE and self.source==Source.MOBOCOM and self.owner==session
    def _switch(self,source,session,target,lease,now):
        if self.configuration and self.configuration.busy():return E.BUSY
        if self.state not in (State.SAFE,State.ACTIVE):return E.STATE
        if self.fault:return E.SAFETY
        if source not in (0,1,2) or (source and not self.profile.source_mask & (1<<source)):return E.CAPABILITY
        if not self.profile.target_min_us<=target<=self.profile.target_max_us or not self.profile.lease_min_us<=lease<=self.profile.lease_max_us:return E.RANGE
        if self.generation>=0xfffffffe:return E.SAFETY
        self._clear();self.target_ttl=target;self.lease_ttl=lease
        self.next_source=Source(source);self.next_owner=session if source==Source.MOBOCOM else 0
        self.transition_start=now;self.state=State.SWITCHING;self._changed()
        if not self.driver.safe(now):self._stop(now,Fault.DRIVER)
        return E.OK
    def select_local_source(self,source,epoch,generation,target,lease,now):
        self.tick(now,False)
        if epoch!=self.session_epoch:return E.EPOCH
        if generation!=self.generation:return E.GENERATION
        if source==Source.MOBOCOM:return E.OWNER
        return self._switch(source,0,target,lease,now)

    def _targets(self,entries,seq,now):
        if not entries or len(entries)>len(self.profile.resources):return E.MALFORMED
        selected={}
        lookup={(r.endpoint,r.resource):i for i,r in enumerate(self.profile.resources)}
        for ep,res,value in entries:
            i=lookup.get((ep,res))
            if i is None:return E.CAPABILITY
            if i in selected:return E.MALFORMED
            r=self.profile.resources[i]
            if not r.minimum<=value<=r.maximum:return E.RANGE
            if self.operation_spec and self.operation_spec.resource_mask & (1<<i):return E.BUSY
            selected[i]=value
        for i in selected:
            group=self.profile.resources[i].group
            if group and any(j not in selected for j,r in enumerate(self.profile.resources) if r.group==group):return E.GROUP
        candidate=self.values|selected
        validation={(self.profile.resources[i].endpoint,self.profile.resources[i].resource):v for i,v in candidate.items()}
        if not self.driver.validate_targets(validation):return E.RANGE
        if self.last_apply is None or not (self.pending or self.active):self.last_apply=now # 从空闲受理目标即重新建立交付期限；非持续资源交付后不再被服务，不能拿旧交付时间判 gap
        for i,value in selected.items():
            self.values[i]=value;self.received[i]=now;self.pending.add(i)
            if self.profile.resources[i].flags&1:self.active.add(i)
        self.accepted_sequence=seq;self.lease_until=now+self.lease_ttl;self._changed()
        return E.OK

    def local_targets(self,epoch,generation,sequence,entries,now):
        self.tick(now,False)
        if epoch!=self.session_epoch:return E.EPOCH
        if generation!=self.generation:return E.GENERATION
        if self.state!=State.ACTIVE or self.source!=Source.REMOTE:return E.OWNER
        if not self.local_sequence < sequence <= 0xffffffff:return E.SEQUENCE
        result=self._targets(entries,sequence,now)
        if result==E.OK:self.local_sequence=sequence
        return result
    def local_heartbeat(self,epoch,generation,now):
        self.tick(now,False)
        if epoch!=self.session_epoch:return E.EPOCH
        if generation!=self.generation:return E.GENERATION
        if self.state!=State.ACTIVE or self.source!=Source.REMOTE:return E.OWNER
        self.lease_until=now+self.lease_ttl;return E.OK

    def _action(self,opcode,data,now):
        spec=next((a for a in self.profile.actions if a.opcode==opcode),None)
        if spec is None:return E.CAPABILITY
        if len(data)!=spec.parameter_bytes:return E.MALFORMED
        if self.operation or any(spec.resource_mask&(1<<i) for i in self.active|self.pending):return E.BUSY
        if not self.driver.validate_action(spec,data):return E.RANGE
        if self.next_operation>0xffffffffffffffff:return E.SPACE
        op=self.next_operation;self.next_operation+=1
        if len(self.operation_results)>=self.ACTION_HISTORY:self.operation_results.pop(next(iter(self.operation_results)))
        self.operation_results[op]=(Outcome.RUNNING,E.OK,0)
        self.operation,self.operation_started,self.operation_spec=op,now,spec
        if not self.driver.start_action(spec,data,op,now):
            self.operation_results[op]=(Outcome.FAILED,E.SAFETY,0)
            self.driver.cancel(op)
            self.operation=0;self.operation_spec=None;self._stop(now,Fault.DRIVER);return E.SAFETY
        self.lease_until=now+self.lease_ttl;self._changed();return E.OK
    def local_action(self,epoch,generation,sequence,opcode,data,now):
        self.tick(now,False)
        if epoch!=self.session_epoch:return E.EPOCH
        if generation!=self.generation:return E.GENERATION
        if self.state!=State.ACTIVE or self.source!=Source.REMOTE:return E.OWNER
        if not self.local_sequence < sequence <= 0xffffffff:return E.SEQUENCE
        e=self._action(opcode,data,now)
        if e==E.OK:self.local_sequence=sequence
        return e

    def _caps(self,cursor):
        p=self.profile
        items=[(1,struct.pack('<HHBHIIIIIIIHHI',p.device_type,p.version,p.source_mask,p.max_target_hz,p.update_gap_us,
            p.target_min_us,p.target_max_us,p.lease_min_us,p.lease_max_us,p.safe_timeout_us,p.switch_timeout_us,
            len(p.resources),len(p.actions),p.auto_reset_mask))]
        items.extend((2,struct.pack('<HHHHiiHii',r.endpoint,r.resource,r.unit,r.flags,r.minimum,r.maximum,r.group,r.vmax,r.amax)) for r in p.resources)
        items.extend((3,struct.pack('<HIHI',a.opcode,a.resource_mask,a.parameter_bytes,a.timeout_us)) for a in p.actions)
        if cursor>=len(items):return self._error(E.RANGE)
        kind,data=items[cursor]
        return Kind.RESPONSE,struct.pack('<HHBH',cursor+1 if cursor+1<len(items) else 0xffff,len(items),kind,len(data))+data
    def _status(self,cursor,now):
        if cursor>=len(self.profile.resources):return self._error(E.RANGE)
        indices=range(cursor,min(cursor+8,len(self.profile.resources)))
        next_cursor=cursor+8 if cursor+8<len(self.profile.resources) else 0xffff
        data=STATUS.pack(self.boot_id,self.generation,self.state_version,self.state,self.source,self.fault,self.owner,
            self.accepted_sequence,self.applied_sequence,self.operation,max(0,self.lease_until-now),
            sum(1<<i for i in self.active),sum(1<<i for i in self.pending),int(self.driver.safe_confirmed(now)),next_cursor,len(indices),self.session_epoch)
        for i in indices:
            r=self.profile.resources[i];key=(r.endpoint,r.resource)
            feedback=self.driver.feedback.get(key) if r.flags&2 else None
            age=min(0xffffffff,now-self.received[i]) if i in self.received else 0xffffffff
            data+=ENTRY.pack(*key,self.values.get(i,0),feedback or 0,age,(1 if i in self.values else 0)|(2 if feedback is not None else 0))
        return Kind.RESPONSE,data

    def _command(self,f,now):
        p=f.payload;c=f.command
        if c==C.READ_CONFIG:
            if len(p)!=2:return self._error(E.MALFORMED)
            if self.configuration is None:return self._error(E.UNSUPPORTED)
            error,snapshot=self.configuration.read(struct.unpack('<H',p)[0])
            return self._error(error) if error else (Kind.RESPONSE,snapshot.encode())
        if c==C.WRITE_CONFIG:
            if len(p)<34 or struct.unpack_from('<Q',p)[0]!=self.uid:return self._error(E.MALFORMED)
            error=self._check_guard(p[8:])
            if error:return self._error(error)
            if self.configuration is None:return self._error(E.UNSUPPORTED)
            if self.configuration.busy():return self._error(E.BUSY)
            schema,revision=struct.unpack_from('<HI',p,28)
            live=schema==LIVE_MOTION_PROFILE_SCHEMA
            if live:
                if f.session!=self.session_id:return self._error(E.SESSION)
                if self.state!=State.ACTIVE or self.source!=Source.MOBOCOM or self.owner!=f.session or self.fault!=Fault.NONE:return self._error(E.OWNER)
                if self.operation:return self._error(E.BUSY)
            elif schema!=FOCUS_CONTROL_SCHEMA and (self.state not in (State.SAFE,State.FAULT) or self.source!=Source.NONE or self.owner):return self._error(E.STATE)
            motion=schema==MOTION_PROFILE_SCHEMA
            if motion and (self.state!=State.SAFE or self.fault!=Fault.NONE or not self.driver.safe_confirmed(now)):return self._error(E.STATE)
            if motion and self.generation>=0xfffffffe:return self._error(E.SPACE)
            error=self.configuration.write(schema,revision,p[34:],f.session,f.sequence,now)
            if not error and motion:
                self._clear();self._advance_generation();self.driver.flush_remote();self._changed()
            if not error and live:self._changed()
            return self._error(error) if error else (Kind.ACK,self._ack())
        if c in (C.HELLO,C.INFO):
            if p:return self._error(E.MALFORMED)
            return Kind.RESPONSE,INFO.pack(self.uid,self.profile.device_type,self.profile.firmware,self.boot_id,VERSION,self.profile.version,self.session_epoch)
        if c==C.CAPS:
            return self._caps(struct.unpack('<H',p)[0]) if len(p)==2 else self._error(E.MALFORMED)
        if c==C.STATUS:
            return self._status(struct.unpack('<H',p)[0],now) if len(p)==2 else self._error(E.MALFORMED)
        if c==C.ACTION_STATUS:
            if len(p)!=8:return self._error(E.MALFORMED)
            op=struct.unpack('<Q',p)[0];result=self.operation_results.get(op)
            return (Kind.RESPONSE,struct.pack('<QBHB',op,*result)) if result else self._error(E.NOT_FOUND)
        if c in (C.STOP,C.ESTOP):
            if len(p)!=20:return self._error(E.MALFORMED)
            e=self._check_guard(p)
            if e:return self._error(e)
            self._stop(now,Fault.ESTOP if c==C.ESTOP else Fault.NONE)
            return Kind.ACK,self._ack()
        if c not in (C.SET_SOURCE,C.HEARTBEAT,C.WRITE_TARGETS,C.EXECUTE_ACTION,C.RESET_FAULT):return self._error(E.UNSUPPORTED)
        e=self._check_guard(p)
        if e:return self._error(e)
        if c==C.SET_SOURCE:
            if self.configuration and self.configuration.busy():return self._error(E.BUSY)
            if len(p)!=29:return self._error(E.MALFORMED)
            source,target,lease=struct.unpack_from('<BII',p,20)
            e=self._switch(source,f.session,target,lease,now)
        elif c==C.RESET_FAULT:
            if self.configuration and self.configuration.busy():return self._error(E.BUSY)
            if len(p)!=20:return self._error(E.MALFORMED)
            if self.state!=State.FAULT or not self.driver.safe_confirmed(now) or not self.driver.reset_ok:return self._error(E.SAFETY)
            self._advance_generation();self.fault=Fault.NONE;self.state=State.SAFE;self._changed();e=E.OK
        else:
            if not self._is_owner(f.session):return self._error(E.OWNER)
            if c==C.HEARTBEAT:
                if len(p)!=20:return self._error(E.MALFORMED)
                self.lease_until=now+self.lease_ttl;e=E.OK
            elif c==C.WRITE_TARGETS:
                if len(p)<21 or len(p)!=21+p[20]*8:return self._error(E.MALFORMED)
                e=self._targets([struct.unpack_from('<HHi',p,21+i*8) for i in range(p[20])],f.sequence,now)
            else:
                if len(p)<22:return self._error(E.MALFORMED)
                e=self._action(struct.unpack_from('<H',p,20)[0],p[22:],now)
        return self._error(e) if e else (Kind.ACK,self._ack())

    def transact(self,raw,now):
        """Process one request datagram; return the reply datagram or None (dropped/no reply)."""
        self.tick(now,False)
        if now<self.last_now:return None
        try:f=decode(raw)
        except ValueError:return None
        if f.kind!=Kind.REQUEST or f.src_uid!=0:return None
        p=f.payload;cmd=f.command
        read=cmd in (C.HELLO,C.INFO,C.CAPS,C.STATUS,C.ACTION_STATUS,C.READ_CONFIG)
        config=cmd==C.WRITE_CONFIG
        live=config and len(p)>=34 and struct.unpack_from('<H',p,28)[0]==LIVE_MOTION_PROFILE_SCHEMA
        focus=config and len(p)>=34 and struct.unpack_from('<H',p,28)[0]==FOCUS_CONTROL_SCHEMA
        opening=cmd==C.SESSION_OPEN
        safety=cmd in (C.STOP,C.ESTOP,C.RESET_FAULT)
        if f.dst_uid==ALL_NODES:
            # Multicast carries only unguarded all-node STOP/ESTOP and DISCOVER.
            if cmd in (C.STOP,C.ESTOP) and not p:
                self._stop(now,Fault.ESTOP if cmd==C.ESTOP else Fault.NONE);return None
            if cmd==C.DISCOVER and not p:
                payload=DISCOVERY.pack(self.profile.device_type,self.profile.firmware,self.boot_id,self.session_epoch,self.generation)
                return encode(Frame(Kind.RESPONSE,self.uid,ALL_NODES,f.sequence,cmd,f.session,payload))
            return None
        if f.dst_uid!=self.uid or cmd==C.DISCOVER:return None
        error=E.OK;cache=False;slot=None
        if opening:
            if len(p)!=20:error=E.MALFORMED
            elif struct.unpack_from('<Q',p)[0]!=self.boot_id:error=E.BOOT
            elif self.opening and self.opening[0]==f.session and self.opening[1]==f.sequence:
                if raw==self.opening[2] and f.session==self.session_id:return self.opening[3]
                error=E.CONFLICT
            elif struct.unpack_from('<Q',p,8)[0]!=self.session_epoch:error=E.EPOCH
            elif struct.unpack_from('<I',p,16)[0]!=self.generation:error=E.GENERATION
            elif self.configuration and self.configuration.busy():error=E.BUSY
            elif self.state!=State.SAFE or self.source!=Source.NONE or self.owner or not self.driver.safe_confirmed(now):error=E.STATE
            elif self.session_epoch==0xffffffffffffffff:error=E.SPACE
            else:
                self._clear();self.session_epoch+=1;self.generation=1;self.local_sequence=0;self.session_id=f.session
                self.sessions.clear();self.opening=None;self.safety=None;self.configuration_cache=None;self.driver.flush_remote();self._changed();slot='opening'
        else:
            if config and (len(p)<34 or struct.unpack_from('<Q',p)[0]!=self.uid):error=E.MALFORMED
            elif not read:
                error=self._check_guard(p[8:] if config else p)
                if error==E.GENERATION and not live and not focus:error=E.OK
                if not error and not safety and not config and f.session!=self.session_id:error=E.SESSION
                if not error and live:
                    if f.session!=self.session_id:error=E.SESSION
                    elif self.state!=State.ACTIVE or self.source!=Source.MOBOCOM or self.owner!=f.session or self.fault!=Fault.NONE:error=E.OWNER
                    elif self.operation:error=E.BUSY
            saved=self.sessions.get(f.session) if f.session==self.session_id else self.safety if safety and self.safety and self.safety[0]==f.session else None
            cached=saved if f.session==self.session_id else saved[1:] if saved else None
            if config:cached=self.configuration_cache[1:] if self.configuration_cache and self.configuration_cache[0]==f.session else None
            if not error and cached and f.sequence<=cached[0]:
                if f.sequence==cached[0] and raw==cached[1]:return cached[2]
                error=E.CONFLICT if f.sequence==cached[0] else E.SEQUENCE
            elif not error:
                slot='configuration' if config else 'session' if f.session==self.session_id else 'safety' if safety else None
        if error:kind,data=self._error(error)
        elif opening:kind,data=Kind.ACK,self._ack()
        else:kind,data=self._command(f,now)
        reply=encode(Frame(kind,self.uid,ALL_NODES,f.sequence,cmd,f.session,data))
        if slot=='opening':self.opening=(f.session,f.sequence,raw,reply);self.sessions[f.session]=(f.sequence,raw,reply)
        elif slot=='session':self.sessions[f.session]=(f.sequence,raw,reply)
        elif slot=='safety':self.safety=(f.session,f.sequence,raw,reply)
        elif slot=='configuration':self.configuration_cache=(f.session,f.sequence,raw,reply)
        return reply

    def receive(self,raw,now):
        """Adapter entry for one datagram. True when a reply is ready for take_reply().

        Datagrams are independent: an untaken older reply is discarded and the
        host retries with identical bytes, which returns the cached result.
        """
        self.reply=None
        reply=self.transact(raw,now)
        if reply is None:return False
        self.reply,self.reply_at,self.reply_until=reply,now,now+REPLY_HOLD_US
        return True
    def take_reply(self,now):
        if self.reply is None or now<self.reply_at:return None
        if now>self.reply_until:self.reply=None;return None
        raw=self.reply;self.reply=None;return raw

class LocalControlInput:
    def __init__(self,node): self.node=node
    def targets(self,epoch,generation,sequence,values,now):return self.node.local_targets(epoch,generation,sequence,values,now)
    def action(self,epoch,generation,sequence,opcode,data,now):return self.node.local_action(epoch,generation,sequence,opcode,data,now)
    def heartbeat(self,epoch,generation,now):return self.node.local_heartbeat(epoch,generation,now)
