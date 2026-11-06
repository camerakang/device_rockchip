use crate::device_types::is_registered_device_type;
use crate::wire::*;
macro_rules! wire_enum {($name:ident,$ty:ty,{$($v:ident=$n:literal),+})=>{
    #[derive(Clone,Copy,Debug,PartialEq,Eq)] #[repr($ty)] pub enum $name {$($v=$n),+}
    impl $name {pub fn decode(v:$ty)->Result<Self>{match v {$($n=>Ok(Self::$v)),+,_=>Err(ErrorCode::Malformed)}}}
};}
wire_enum!(Source,u8,{None=0,Remote=1,Mobocom=2});
wire_enum!(State,u8,{Safe=0,Switching=1,Active=2,Stopping=3,Fault=4});
wire_enum!(Fault,u16,{None=0,SourceTimeout=1,TargetExpired=2,ActionTimeout=3,Driver=4,StopTimeout=5,SwitchTimeout=6,Estop=7,LocalClock=8,OutputGap=9,GenerationExhausted=10,LimitTrip=11});
wire_enum!(Outcome,u8,{Running=1,Success=2,Failed=3,Cancelled=4});
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Resource {
    pub endpoint: u16,
    pub resource: u16,
    pub unit: u16,
    pub flags: u16,
    pub minimum: i32,
    pub maximum: i32,
    pub group: u16,
    pub vmax: i32,
    pub amax: i32,
}
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Action {
    pub opcode: u16,
    pub resource_mask: u32,
    pub parameter_bytes: u16,
    pub timeout_us: u32,
}
/// Device-specific values must come from the real category contract, never a virtual fixture.
#[derive(Clone, Copy, Debug)]
pub struct Profile<'a> {
    pub device_type: u16,
    pub resources: &'a [Resource],
    pub actions: &'a [Action],
    pub version: u16,
    pub firmware: u32,
    pub source_mask: u8,
    pub max_target_hz: u16,
    pub update_gap_us: u32,
    pub target_min_us: u32,
    pub target_max_us: u32,
    pub lease_min_us: u32,
    pub lease_max_us: u32,
    pub safe_timeout_us: u32,
    pub switch_timeout_us: u32,
    pub auto_reset_mask: u32,
}
impl Profile<'_> {
    pub fn validate(&self) -> Result<()> {
        let p = self;
        if !is_registered_device_type(p.device_type)
            || p.version == 0
            || p.firmware == 0
            || p.resources.is_empty()
            || p.resources.len() > 16
            || p.actions.len() > 16
            || p.source_mask == 0
            || p.source_mask & !6 != 0
            || p.auto_reset_mask & !6 != 0
            || p.target_min_us == 0
            || p.target_min_us > p.target_max_us
            || p.lease_min_us == 0
            || p.lease_min_us > p.lease_max_us
            || p.safe_timeout_us == 0
            || p.switch_timeout_us == 0
            || p.update_gap_us == 0
            || p.max_target_hz == 0
        {
            return Err(ErrorCode::Capability);
        }
        for (i, r) in p.resources.iter().enumerate() {
            if r.endpoint == 0
                || r.resource == 0
                || r.unit == 0
                || r.minimum > r.maximum
                || r.flags & !3 != 0
                || p.resources[..i]
                    .iter()
                    .any(|x| x.endpoint == r.endpoint && x.resource == r.resource)
            {
                return Err(ErrorCode::Capability);
            }
        }
        for (i, a) in p.actions.iter().enumerate() {
            if a.opcode == 0
                || a.timeout_us == 0
                || a.parameter_bytes > 218
                || a.resource_mask >> p.resources.len() != 0
                || p.actions[..i].iter().any(|x| x.opcode == a.opcode)
            {
                return Err(ErrorCode::Capability);
            }
        }
        Ok(())
    }
    pub fn caps(&self, cursor: u16) -> Result<Payload> {
        let total = 1 + self.resources.len() + self.actions.len();
        let c = cursor as usize;
        if c >= total {
            return Err(ErrorCode::Range);
        }
        let mut item = Payload::default();
        let kind = if c == 0 {
            item.u16(self.device_type)?
                .u16(self.version)?
                .u8(self.source_mask)?
                .u16(self.max_target_hz)?
                .u32(self.update_gap_us)?
                .u32(self.target_min_us)?
                .u32(self.target_max_us)?
                .u32(self.lease_min_us)?
                .u32(self.lease_max_us)?
                .u32(self.safe_timeout_us)?
                .u32(self.switch_timeout_us)?
                .u16(self.resources.len() as u16)?
                .u16(self.actions.len() as u16)?
                .u32(self.auto_reset_mask)?;
            1
        } else if c <= self.resources.len() {
            let r = self.resources[c - 1];
            item.u16(r.endpoint)?
                .u16(r.resource)?
                .u16(r.unit)?
                .u16(r.flags)?
                .i32(r.minimum)?
                .i32(r.maximum)?
                .u16(r.group)?
                .i32(r.vmax)?
                .i32(r.amax)?;
            2
        } else {
            let a = self.actions[c - 1 - self.resources.len()];
            item.u16(a.opcode)?
                .u32(a.resource_mask)?
                .u16(a.parameter_bytes)?
                .u32(a.timeout_us)?;
            3
        };
        let mut b = Payload::default();
        b.u16(if c + 1 < total { cursor + 1 } else { u16::MAX })?
            .u16(total as u16)?
            .u8(kind)?
            .u16(item.len() as u16)?
            .raw(item.as_slice())?;
        Ok(b)
    }
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Ack {
    pub guard: Guard,
    pub state: State,
    pub source: Source,
    pub fault: u16,
    pub accepted: u32,
    pub applied: u32,
    pub operation: u64,
}
impl Ack {
    pub fn encode(self) -> Payload {
        let mut b = Payload::default();
        b.u64(self.guard.boot)
            .unwrap()
            .u32(self.guard.generation)
            .unwrap()
            .u8(self.state as u8)
            .unwrap()
            .u8(self.source as u8)
            .unwrap()
            .u16(self.fault)
            .unwrap()
            .u32(self.accepted)
            .unwrap()
            .u32(self.applied)
            .unwrap()
            .u64(self.operation)
            .unwrap()
            .u64(self.guard.epoch)
            .unwrap();
        b
    }
    pub fn decode(data: &[u8]) -> Result<Self> {
        let mut r = Reader::new(data);
        let boot = r.u64()?;
        let generation = r.u32()?;
        let state = State::decode(r.u8()?)?;
        let source = Source::decode(r.u8()?)?;
        let fault = r.u16()?;
        let accepted = r.u32()?;
        let applied = r.u32()?;
        let operation = r.u64()?;
        let epoch = r.u64()?;
        r.end()?;
        Ok(Self {
            guard: Guard {
                boot,
                generation,
                epoch,
            },
            state,
            source,
            fault,
            accepted,
            applied,
            operation,
        })
    }
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Info {
    pub uid: u64,
    pub device_type: u16,
    pub firmware: u32,
    pub boot: u64,
    pub profile_version: u16,
    pub epoch: u64,
}
impl Info {
    pub fn encode(self) -> Payload {
        let mut b = Payload::default();
        b.u64(self.uid)
            .unwrap()
            .u16(self.device_type)
            .unwrap()
            .u32(self.firmware)
            .unwrap()
            .u64(self.boot)
            .unwrap()
            .u8(VERSION)
            .unwrap()
            .u16(self.profile_version)
            .unwrap()
            .u64(self.epoch)
            .unwrap();
        b
    }
    pub fn decode(b: &[u8]) -> Result<Self> {
        let mut r = Reader::new(b);
        let uid = r.u64()?;
        let device_type = r.u16()?;
        let firmware = r.u32()?;
        let boot = r.u64()?;
        if r.u8()? != VERSION {
            return Err(ErrorCode::Malformed);
        }
        let profile_version = r.u16()?;
        let epoch = r.u64()?;
        r.end()?;
        if uid == 0 || boot == 0 || epoch == 0 {
            return Err(ErrorCode::Malformed);
        }
        Ok(Self {
            uid,
            device_type,
            firmware,
            boot,
            profile_version,
            epoch,
        })
    }
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Discovery {
    pub device_type: u16,
    pub firmware: u32,
    pub boot: u64,
    pub epoch: u64,
    pub generation: u32,
}
impl Discovery {
    pub fn encode(self) -> Payload {
        let mut b = Payload::default();
        b.u16(self.device_type)
            .unwrap()
            .u32(self.firmware)
            .unwrap()
            .u64(self.boot)
            .unwrap()
            .u64(self.epoch)
            .unwrap()
            .u32(self.generation)
            .unwrap();
        b
    }
    pub fn decode(b: &[u8]) -> Result<Self> {
        let mut r = Reader::new(b);
        let s = Self {
            device_type: r.u16()?,
            firmware: r.u32()?,
            boot: r.u64()?,
            epoch: r.u64()?,
            generation: r.u32()?,
        };
        r.end()?;
        if s.boot == 0 || s.epoch == 0 || s.generation == 0 {
            return Err(ErrorCode::Malformed);
        }
        Ok(s)
    }
}
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct ResourceStatus {
    pub endpoint: u16,
    pub resource: u16,
    pub target: i32,
    pub feedback: i32,
    pub age_us: u32,
    pub flags: u8,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Status {
    pub ack: Ack,
    pub version: u32,
    pub owner: u64,
    pub lease_remaining_us: u32,
    pub active_mask: u16,
    pub pending_mask: u16,
    pub safe_confirmed: bool,
    pub next: u16,
    pub count: u8,
    pub resources: [ResourceStatus; 8],
}
impl Status {
    pub fn encode(&self) -> Result<Payload> {
        if self.count > 8 {
            return Err(ErrorCode::Malformed);
        }
        let a = self.ack;
        let mut b = Payload::default();
        b.u64(a.guard.boot)?
            .u32(a.guard.generation)?
            .u32(self.version)?
            .u8(a.state as u8)?
            .u8(a.source as u8)?
            .u16(a.fault)?
            .u64(self.owner)?
            .u32(a.accepted)?
            .u32(a.applied)?
            .u64(a.operation)?
            .u32(self.lease_remaining_us)?
            .u16(self.active_mask)?
            .u16(self.pending_mask)?
            .u8(self.safe_confirmed as u8)?
            .u16(self.next)?
            .u8(self.count)?
            .u64(a.guard.epoch)?;
        for r in &self.resources[..self.count as usize] {
            b.u16(r.endpoint)?
                .u16(r.resource)?
                .i32(r.target)?
                .i32(r.feedback)?
                .u32(r.age_us)?
                .u8(r.flags)?;
        }
        Ok(b)
    }
    pub fn decode(b: &[u8]) -> Result<Self> {
        let mut r = Reader::new(b);
        let boot = r.u64()?;
        let generation = r.u32()?;
        let version = r.u32()?;
        let state = State::decode(r.u8()?)?;
        let source = Source::decode(r.u8()?)?;
        let fault = r.u16()?;
        let owner = r.u64()?;
        let accepted = r.u32()?;
        let applied = r.u32()?;
        let operation = r.u64()?;
        let lease_remaining_us = r.u32()?;
        let active_mask = r.u16()?;
        let pending_mask = r.u16()?;
        let safe = r.u8()?;
        let next = r.u16()?;
        let count = r.u8()?;
        let epoch = r.u64()?;
        if safe > 1 || count > 8 || boot == 0 || generation == 0 || epoch == 0 {
            return Err(ErrorCode::Malformed);
        }
        let mut resources = [ResourceStatus::default(); 8];
        for s in &mut resources[..count as usize] {
            *s = ResourceStatus {
                endpoint: r.u16()?,
                resource: r.u16()?,
                target: r.i32()?,
                feedback: r.i32()?,
                age_us: r.u32()?,
                flags: r.u8()?,
            };
            if s.flags & !3 != 0 {
                return Err(ErrorCode::Malformed);
            }
        }
        r.end()?;
        Ok(Self {
            ack: Ack {
                guard: Guard {
                    boot,
                    generation,
                    epoch,
                },
                state,
                source,
                fault,
                accepted,
                applied,
                operation,
            },
            version,
            owner,
            lease_remaining_us,
            active_mask,
            pending_mask,
            safe_confirmed: safe != 0,
            next,
            count,
            resources,
        })
    }
}
/// Bounded CAPS collector. Cursor/order/count checks prevent malformed endless pagination.
#[derive(Clone, Debug, Default)]
pub struct Capabilities {
    pub device_type: u16,
    pub version: u16,
    pub source_mask: u8,
    pub max_target_hz: u16,
    pub update_gap_us: u32,
    pub target_min_us: u32,
    pub target_max_us: u32,
    pub lease_min_us: u32,
    pub lease_max_us: u32,
    pub safe_timeout_us: u32,
    pub switch_timeout_us: u32,
    pub auto_reset_mask: u32,
    pub resource_count: usize,
    pub action_count: usize,
    pub resources: [Resource; 16],
    pub actions: [Action; 16],
    cursor: u16,
    complete: bool,
}
impl Capabilities {
    pub fn complete(&self) -> bool {
        self.complete
    }
    pub fn cursor(&self) -> u16 {
        self.cursor
    }
    pub fn push(&mut self, b: &[u8]) -> Result<()> {
        // Parse on a copy so errors leave the collector unchanged.
        let mut next = self.clone();
        next.push_inner(b)?;
        *self = next;
        Ok(())
    }
    fn push_inner(&mut self, b: &[u8]) -> Result<()> {
        if self.complete {
            return Err(ErrorCode::State);
        }
        let mut h = Reader::new(b);
        let next = h.u16()?;
        let total = h.u16()?;
        let kind = h.u8()?;
        let n = h.u16()? as usize;
        let mut r = Reader::new(h.raw(n)?);
        h.end()?;
        if total == 0
            || total > 33
            || self.cursor >= total
            || next
                != if self.cursor + 1 == total {
                    u16::MAX
                } else {
                    self.cursor + 1
                }
        {
            return Err(ErrorCode::Malformed);
        }
        if self.cursor == 0 {
            if kind != 1 {
                return Err(ErrorCode::Malformed);
            }
            self.device_type = r.u16()?;
            self.version = r.u16()?;
            self.source_mask = r.u8()?;
            self.max_target_hz = r.u16()?;
            self.update_gap_us = r.u32()?;
            self.target_min_us = r.u32()?;
            self.target_max_us = r.u32()?;
            self.lease_min_us = r.u32()?;
            self.lease_max_us = r.u32()?;
            self.safe_timeout_us = r.u32()?;
            self.switch_timeout_us = r.u32()?;
            self.resource_count = r.u16()? as usize;
            self.action_count = r.u16()? as usize;
            self.auto_reset_mask = r.u32()?;
            if self.resource_count == 0 || self.resource_count > 16 || self.action_count > 16 {
                return Err(ErrorCode::Malformed);
            }
        } else if self.cursor as usize <= self.resource_count {
            if kind != 2 {
                return Err(ErrorCode::Malformed);
            }
            self.resources[self.cursor as usize - 1] = Resource {
                endpoint: r.u16()?,
                resource: r.u16()?,
                unit: r.u16()?,
                flags: r.u16()?,
                minimum: r.i32()?,
                maximum: r.i32()?,
                group: r.u16()?,
                vmax: r.i32()?,
                amax: r.i32()?,
            };
        } else {
            if kind != 3 {
                return Err(ErrorCode::Malformed);
            }
            let i = self.cursor as usize - 1 - self.resource_count;
            if i >= self.action_count {
                return Err(ErrorCode::Malformed);
            }
            self.actions[i] = Action {
                opcode: r.u16()?,
                resource_mask: r.u32()?,
                parameter_bytes: r.u16()?,
                timeout_us: r.u32()?,
            };
        }
        r.end()?;
        if total as usize != 1 + self.resource_count + self.action_count {
            return Err(ErrorCode::Malformed);
        }
        self.cursor = next;
        self.complete = next == u16::MAX;
        Ok(())
    }
}
