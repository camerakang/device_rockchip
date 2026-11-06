//! Fixed-capacity, single-owner device state machine. No heap, sockets, clocks or hardware defaults.
use crate::{configuration::*, model::*, wire::*};

/// Required safety hooks have no permissive defaults. All callbacks must be bounded/nonblocking.
/// Implement configuration methods only when the adapter owns the schema and its revision rules.
pub trait Driver {
    fn validate_targets(&mut self, candidate: &[Target]) -> bool;
    fn apply_targets(&mut self, values: &[Target], now: u64) -> bool;
    fn start_safe(&mut self, reason: Fault, now: u64) -> bool;
    fn safe_confirmed(&self, now: u64) -> bool;
    fn reset_allowed(&self) -> bool;
    fn flush_remote(&mut self);
    fn feedback(&self, _resource: Resource) -> Option<i32> {
        None
    }
    fn validate_action(&mut self, _spec: Action, _data: &[u8]) -> bool {
        false
    }
    fn start_action(&mut self, _spec: Action, _data: &[u8], _operation: u64, _now: u64) -> bool {
        false
    }
    fn poll_action(&mut self, _operation: u64, _now: u64) -> Outcome {
        Outcome::Failed
    }
    fn cancel_action(&mut self, _operation: u64) {}
    fn configuration_busy(&self) -> bool {
        false
    }
    fn read_configuration(&self, _schema: u16) -> Result<ConfigurationSnapshot> {
        Err(ErrorCode::Unsupported)
    }
    /// Success for schema 2/3/4 must be synchronous Applied; schema 1 may be Pending.
    /// Validate all values/revision before mutation. Failed admission must not consume revision.
    fn write_configuration(
        &mut self,
        _request: ConfigurationWrite<'_>,
        _session: u64,
        _sequence: u32,
        _now: u64,
    ) -> Result<()> {
        Err(ErrorCode::Unsupported)
    }
}
#[derive(Clone, Copy)]
struct Cache {
    session: u64,
    sequence: u32,
    request: Datagram,
    reply: Datagram,
}
#[derive(Clone, Copy, Debug)]
pub struct ActionResult {
    pub operation: u64,
    pub outcome: Outcome,
    pub error: u16,
    pub progress: u8,
}
impl ActionResult {
    pub fn encode(self) -> Payload {
        let mut b = Payload::default();
        b.u64(self.operation)
            .unwrap()
            .u8(self.outcome as u8)
            .unwrap()
            .u16(self.error)
            .unwrap()
            .u8(self.progress)
            .unwrap();
        b
    }
}

pub struct DeviceCore<'a, D: Driver> {
    profile: Profile<'a>,
    driver: D,
    uid: u64,
    boot: u64,
    state: State,
    source: Source,
    owner: u64,
    generation: u32,
    version: u32,
    fault: Fault,
    epoch: u64,
    session_id: u64,
    session: Option<Cache>,
    opening: Option<Cache>,
    safety: Option<Cache>,
    configuration: Option<Cache>,
    values: [i32; 16],
    received: [u64; 16],
    known: u16,
    pending: u16,
    active: u16,
    accepted: u32,
    applied: u32,
    last_apply: Option<u64>,
    lease_until: u64,
    target_ttl: u32,
    lease_ttl: u32,
    transition_start: u64,
    next_source: Source,
    next_owner: u64,
    last_now: u64,
    local_sequence: u32,
    next_operation: Option<u64>,
    operation: u64,
    operation_started: u64,
    operation_spec: Option<Action>,
    results: [Option<ActionResult>; 8],
    result_cursor: usize,
    reply: Option<Datagram>,
    reply_at: u64,
}
impl<'a, D: Driver> DeviceCore<'a, D> {
    pub fn new(profile: Profile<'a>, uid: u64, boot: u64, driver: D) -> Result<Self> {
        profile.validate()?;
        if uid == 0 || boot == 0 {
            return Err(ErrorCode::Address);
        }
        Ok(Self {
            profile,
            driver,
            uid,
            boot,
            state: State::Safe,
            source: Source::None,
            owner: 0,
            generation: 1,
            version: 1,
            fault: Fault::None,
            epoch: 1,
            session_id: 0,
            session: None,
            opening: None,
            safety: None,
            configuration: None,
            values: [0; 16],
            received: [0; 16],
            known: 0,
            pending: 0,
            active: 0,
            accepted: 0,
            applied: 0,
            last_apply: None,
            lease_until: 0,
            target_ttl: 0,
            lease_ttl: 0,
            transition_start: 0,
            next_source: Source::None,
            next_owner: 0,
            last_now: 0,
            local_sequence: 0,
            next_operation: Some(1),
            operation: 0,
            operation_started: 0,
            operation_spec: None,
            results: [None; 8],
            result_cursor: 0,
            reply: None,
            reply_at: 0,
        })
    }
    pub fn profile(&self) -> Profile<'a> {
        self.profile
    }
    pub fn driver(&self) -> &D {
        &self.driver
    }
    /// Only adapter state is mutable; protocol credentials and state remain private.
    pub fn driver_mut(&mut self) -> &mut D {
        &mut self.driver
    }
    pub fn uid(&self) -> u64 {
        self.uid
    }
    pub fn guard(&self) -> Guard {
        Guard {
            boot: self.boot,
            generation: self.generation,
            epoch: self.epoch,
        }
    }
    pub fn state(&self) -> State {
        self.state
    }
    pub fn source(&self) -> Source {
        self.source
    }
    pub fn fault(&self) -> Fault {
        self.fault
    }
    pub fn owner(&self) -> u64 {
        self.owner
    }
    pub fn ack(&self) -> Ack {
        Ack {
            guard: self.guard(),
            state: self.state,
            source: self.source,
            fault: self.fault as u16,
            accepted: self.accepted,
            applied: self.applied,
            operation: self.operation,
        }
    }
    fn changed(&mut self) {
        self.version = self.version.wrapping_add(1);
    }
    fn advance_generation(&mut self) -> bool {
        if let Some(n) = self.generation.checked_add(1) {
            self.generation = n;
            self.local_sequence = 0;
            true
        } else {
            false
        }
    }
    fn set_result(&mut self, outcome: Outcome, error: ErrorCode, progress: u8) {
        for r in self.results.iter_mut().flatten() {
            if r.operation == self.operation {
                r.outcome = outcome;
                r.error = error as u16;
                r.progress = progress;
            }
        }
    }
    fn clear(&mut self) {
        self.pending = 0;
        self.active = 0;
        self.known = 0;
        self.values = [0; 16];
        self.received = [0; 16];
        self.last_apply = None;
        if self.operation != 0 {
            self.driver.cancel_action(self.operation);
            self.set_result(Outcome::Cancelled, ErrorCode::Ok, 0);
            self.operation = 0;
            self.operation_spec = None;
        }
        self.source = Source::None;
        self.owner = 0;
        self.lease_until = 0;
        self.accepted = 0;
        self.applied = 0;
    }
    fn stop(&mut self, now: u64, reason: Fault) {
        let timeout = matches!(self.fault, Fault::SourceTimeout | Fault::TargetExpired);
        if reason == Fault::Estop
            || (reason != Fault::None
                && (self.fault == Fault::None
                    || (timeout && !matches!(reason, Fault::SourceTimeout | Fault::TargetExpired))))
        {
            self.fault = reason;
        }
        if matches!(self.state, State::Stopping | State::Fault) {
            return;
        }
        if self.state == State::Safe {
            if self.fault != Fault::None {
                self.state = State::Fault;
                self.changed();
            }
            return;
        }
        self.clear();
        self.next_source = Source::None;
        self.next_owner = 0;
        self.advance_generation();
        self.state = State::Stopping;
        self.transition_start = now;
        self.changed();
        if !self.driver.start_safe(reason, now) {
            self.fault = Fault::Driver;
        }
    }
    pub fn fault_input(&mut self, now: u64, reason: Fault) {
        self.tick_inner(now, false);
        self.stop(self.last_now, reason);
        self.tick_inner(now, false);
    }
    pub fn recover_timeout_fault(&mut self, now: u64) -> bool {
        if now < self.last_now
            || self.driver.configuration_busy()
            || self.state != State::Fault
            || !matches!(self.fault, Fault::SourceTimeout | Fault::TargetExpired)
            || self.profile.auto_reset_mask & (1 << (self.fault as u16)) == 0
            || self.source != Source::None
            || self.owner != 0
            || self.active != 0
            || self.pending != 0
            || self.operation != 0
            || self.generation == u32::MAX
            || !self.driver.safe_confirmed(now)
            || !self.driver.reset_allowed()
        {
            return false;
        }
        self.last_now = now;
        self.clear();
        self.next_source = Source::None;
        self.next_owner = 0;
        self.advance_generation();
        self.fault = Fault::None;
        self.state = State::Safe;
        self.changed();
        true
    }
    pub fn tick(&mut self, now: u64) {
        self.tick_inner(now, true)
    }
    fn tick_inner(&mut self, now: u64, apply: bool) {
        if now < self.last_now {
            self.stop(self.last_now, Fault::LocalClock);
            return;
        }
        self.last_now = now;
        if self.state == State::Switching {
            if now - self.transition_start >= self.profile.switch_timeout_us as u64 {
                self.stop(now, Fault::SwitchTimeout);
            } else if self.driver.safe_confirmed(now) {
                if !self.advance_generation() {
                    self.stop(now, Fault::GenerationExhausted);
                    return;
                }
                self.source = self.next_source;
                self.owner = self.next_owner;
                self.state = if self.source == Source::None {
                    State::Safe
                } else {
                    State::Active
                };
                self.lease_until = if self.source == Source::None {
                    0
                } else {
                    now.saturating_add(self.lease_ttl as u64)
                };
                self.driver.flush_remote();
                self.changed();
            }
        }
        if self.state == State::Stopping {
            if self.driver.safe_confirmed(now) {
                self.state = if self.fault == Fault::None {
                    State::Safe
                } else {
                    State::Fault
                };
                self.changed();
            } else if now - self.transition_start >= self.profile.safe_timeout_us as u64 {
                self.fault = Fault::StopTimeout;
                self.state = State::Fault;
                self.changed();
            }
        }
        if self.state != State::Active {
            return;
        }
        if now >= self.lease_until {
            self.stop(now, Fault::SourceTimeout);
            return;
        }
        for i in 0..self.profile.resources.len() {
            if self.active & (1 << i) != 0 && now - self.received[i] >= self.target_ttl as u64 {
                self.stop(now, Fault::TargetExpired);
                return;
            }
        }
        if let Some(spec) = self.operation_spec {
            if now - self.operation_started >= spec.timeout_us as u64 {
                self.driver.cancel_action(self.operation);
                self.set_result(Outcome::Failed, ErrorCode::Timeout, 0);
                self.operation = 0;
                self.operation_spec = None;
                self.stop(now, Fault::ActionTimeout);
                return;
            }
            let outcome = self.driver.poll_action(self.operation, now);
            if outcome != Outcome::Running {
                self.set_result(
                    outcome,
                    if outcome == Outcome::Success {
                        ErrorCode::Ok
                    } else {
                        ErrorCode::Driver
                    },
                    if outcome == Outcome::Success { 100 } else { 0 },
                );
                self.operation = 0;
                self.operation_spec = None;
                self.changed();
                if outcome != Outcome::Success {
                    self.stop(now, Fault::Driver);
                    return;
                }
            }
        }
        if self.active | self.pending != 0 {
            if let Some(last) = self.last_apply {
                if now - last > self.profile.update_gap_us as u64 {
                    self.stop(now, Fault::OutputGap);
                    return;
                }
            }
            if apply {
                let (batch, n) = self.batch(self.active | self.pending, &self.values);
                if !self.driver.apply_targets(&batch[..n], now) {
                    self.stop(now, Fault::Driver);
                    return;
                }
                if self.applied != self.accepted {
                    self.changed();
                }
                self.applied = self.accepted;
                self.pending = 0;
                self.last_apply = Some(now);
            }
        }
    }
    fn batch(&self, mask: u16, values: &[i32; 16]) -> ([Target; 16], usize) {
        let mut b = [Target::default(); 16];
        let mut n = 0;
        for (i, r) in self.profile.resources.iter().enumerate() {
            if mask & (1 << i) != 0 {
                b[n] = Target {
                    endpoint: r.endpoint,
                    resource: r.resource,
                    value: values[i],
                };
                n += 1;
            }
        }
        (b, n)
    }
    fn check_guard(&self, data: &[u8]) -> Result<()> {
        let g = Guard::read(&mut Reader::new(data))?;
        if g.boot != self.boot {
            Err(ErrorCode::Boot)
        } else if g.epoch != self.epoch {
            Err(ErrorCode::Epoch)
        } else if g.generation != self.generation {
            Err(ErrorCode::Generation)
        } else {
            Ok(())
        }
    }
    fn check_local(&self, epoch: u64, generation: u32) -> Result<()> {
        if epoch != self.epoch {
            return Err(ErrorCode::Epoch);
        }
        if generation != self.generation {
            return Err(ErrorCode::Generation);
        }
        if self.state != State::Active || self.source != Source::Remote {
            return Err(ErrorCode::Owner);
        }
        Ok(())
    }
    fn switch(
        &mut self,
        source: Source,
        session: u64,
        target: u32,
        lease: u32,
        now: u64,
    ) -> Result<()> {
        if self.driver.configuration_busy() {
            return Err(ErrorCode::Busy);
        }
        if !matches!(self.state, State::Safe | State::Active) {
            return Err(ErrorCode::State);
        }
        if self.fault != Fault::None || self.generation >= u32::MAX - 1 {
            return Err(ErrorCode::Safety);
        }
        if source != Source::None && self.profile.source_mask & (1 << (source as u8)) == 0 {
            return Err(ErrorCode::Capability);
        }
        if !(self.profile.target_min_us..=self.profile.target_max_us).contains(&target)
            || !(self.profile.lease_min_us..=self.profile.lease_max_us).contains(&lease)
        {
            return Err(ErrorCode::Range);
        }
        self.clear();
        self.target_ttl = target;
        self.lease_ttl = lease;
        self.next_source = source;
        self.next_owner = if source == Source::Mobocom {
            session
        } else {
            0
        };
        self.transition_start = now;
        self.state = State::Switching;
        self.changed();
        if !self.driver.start_safe(Fault::None, now) {
            self.stop(now, Fault::Driver)
        }
        Ok(())
    }
    pub fn select_local_source(
        &mut self,
        source: Source,
        g: Guard,
        target: u32,
        lease: u32,
        now: u64,
    ) -> Result<()> {
        self.tick_inner(now, false);
        if now < self.last_now {
            return Err(ErrorCode::Safety);
        }
        self.check_guard(g.encode().as_slice())?;
        if source == Source::Mobocom {
            return Err(ErrorCode::Owner);
        }
        self.switch(source, 0, target, lease, now)
    }
    fn accept_targets(&mut self, entries: &[Target], sequence: u32, now: u64) -> Result<()> {
        if entries.is_empty() || entries.len() > self.profile.resources.len() {
            return Err(ErrorCode::Malformed);
        }
        let mut selected = 0u16;
        let mut candidate = self.values;
        for t in entries {
            let i = self
                .profile
                .resources
                .iter()
                .position(|r| r.endpoint == t.endpoint && r.resource == t.resource)
                .ok_or(ErrorCode::Capability)?;
            let r = self.profile.resources[i];
            let bit = 1 << i;
            if selected & bit != 0 {
                return Err(ErrorCode::Malformed);
            }
            if !(r.minimum..=r.maximum).contains(&t.value) {
                return Err(ErrorCode::Range);
            }
            if self
                .operation_spec
                .is_some_and(|a| a.resource_mask & bit as u32 != 0)
            {
                return Err(ErrorCode::Busy);
            }
            selected |= bit;
            candidate[i] = t.value;
        }
        for (i, r) in self.profile.resources.iter().enumerate() {
            if selected & (1 << i) != 0
                && r.group != 0
                && self
                    .profile
                    .resources
                    .iter()
                    .enumerate()
                    .any(|(j, other)| other.group == r.group && selected & (1 << j) == 0)
            {
                return Err(ErrorCode::Group);
            }
        }
        let (batch, n) = self.batch(self.known | selected, &candidate);
        if !self.driver.validate_targets(&batch[..n]) {
            return Err(ErrorCode::Range);
        }
        if self.last_apply.is_none() || self.active | self.pending == 0 {
            self.last_apply = Some(now)
        }
        self.values = candidate;
        self.known |= selected;
        self.pending |= selected;
        for (i, r) in self.profile.resources.iter().enumerate() {
            if selected & (1 << i) != 0 {
                self.received[i] = now;
                if r.flags & 1 != 0 {
                    self.active |= 1 << i;
                }
            }
        }
        self.accepted = sequence;
        self.lease_until = now.saturating_add(self.lease_ttl as u64);
        self.changed();
        Ok(())
    }
    pub fn local_targets(
        &mut self,
        epoch: u64,
        generation: u32,
        sequence: u32,
        entries: &[Target],
        now: u64,
    ) -> Result<()> {
        self.tick_inner(now, false);
        if now < self.last_now {
            return Err(ErrorCode::Safety);
        }
        self.check_local(epoch, generation)?;
        if sequence <= self.local_sequence {
            return Err(ErrorCode::Sequence);
        }
        self.accept_targets(entries, sequence, now)?;
        self.local_sequence = sequence;
        Ok(())
    }
    pub fn local_heartbeat(&mut self, epoch: u64, generation: u32, now: u64) -> Result<()> {
        self.tick_inner(now, false);
        if now < self.last_now {
            return Err(ErrorCode::Safety);
        }
        self.check_local(epoch, generation)?;
        self.lease_until = now.saturating_add(self.lease_ttl as u64);
        Ok(())
    }
    fn action(&mut self, opcode: u16, data: &[u8], now: u64) -> Result<()> {
        let spec = *self
            .profile
            .actions
            .iter()
            .find(|a| a.opcode == opcode)
            .ok_or(ErrorCode::Capability)?;
        if data.len() != spec.parameter_bytes as usize {
            return Err(ErrorCode::Malformed);
        }
        if self.operation != 0 || spec.resource_mask & (self.active | self.pending) as u32 != 0 {
            return Err(ErrorCode::Busy);
        }
        if !self.driver.validate_action(spec, data) {
            return Err(ErrorCode::Range);
        }
        let op = self.next_operation.ok_or(ErrorCode::Space)?;
        self.next_operation = op.checked_add(1);
        self.results[self.result_cursor] = Some(ActionResult {
            operation: op,
            outcome: Outcome::Running,
            error: 0,
            progress: 0,
        });
        self.result_cursor = (self.result_cursor + 1) % 8;
        self.operation = op;
        self.operation_spec = Some(spec);
        self.operation_started = now;
        if !self.driver.start_action(spec, data, op, now) {
            self.set_result(Outcome::Failed, ErrorCode::Safety, 0);
            self.driver.cancel_action(op);
            self.operation = 0;
            self.operation_spec = None;
            self.stop(now, Fault::Driver);
            return Err(ErrorCode::Safety);
        }
        self.lease_until = now.saturating_add(self.lease_ttl as u64);
        self.changed();
        Ok(())
    }
    pub fn local_action(
        &mut self,
        epoch: u64,
        generation: u32,
        sequence: u32,
        opcode: u16,
        data: &[u8],
        now: u64,
    ) -> Result<()> {
        self.tick_inner(now, false);
        if now < self.last_now {
            return Err(ErrorCode::Safety);
        }
        self.check_local(epoch, generation)?;
        if sequence <= self.local_sequence {
            return Err(ErrorCode::Sequence);
        }
        self.action(opcode, data, now)?;
        self.local_sequence = sequence;
        Ok(())
    }
    pub fn status(&self, cursor: u16, now: u64) -> Result<Status> {
        let c = cursor as usize;
        if c >= self.profile.resources.len() {
            return Err(ErrorCode::Range);
        }
        let end = (c + 8).min(self.profile.resources.len());
        let mut resources = [ResourceStatus::default(); 8];
        for i in c..end {
            let r = self.profile.resources[i];
            let feedback = if r.flags & 2 != 0 {
                self.driver.feedback(r)
            } else {
                None
            };
            let known = self.known & (1 << i) != 0;
            resources[i - c] = ResourceStatus {
                endpoint: r.endpoint,
                resource: r.resource,
                target: if known { self.values[i] } else { 0 },
                feedback: feedback.unwrap_or(0),
                age_us: if known {
                    now.saturating_sub(self.received[i]).min(u32::MAX as u64) as u32
                } else {
                    u32::MAX
                },
                flags: known as u8 | ((feedback.is_some() as u8) << 1),
            };
        }
        Ok(Status {
            ack: self.ack(),
            version: self.version,
            owner: self.owner,
            lease_remaining_us: self.lease_until.saturating_sub(now).min(u32::MAX as u64) as u32,
            active_mask: self.active,
            pending_mask: self.pending,
            safe_confirmed: self.driver.safe_confirmed(now),
            next: if end < self.profile.resources.len() {
                end as u16
            } else {
                u16::MAX
            },
            count: (end - c) as u8,
            resources,
        })
    }
    fn command(&mut self, f: &Frame, now: u64) -> Result<(Kind, Payload)> {
        let p = f.payload.as_slice();
        let c = f.command;
        let mut r = Reader::new(p);
        if c == Command::ReadConfig as u16 {
            let schema = r.u16()?;
            r.end()?;
            return Ok((
                Kind::Response,
                self.driver.read_configuration(schema)?.encode(),
            ));
        }
        if c == Command::WriteConfig as u16 {
            let uid = r.u64()?;
            let guard = Guard::read(&mut r)?;
            let schema = r.u16()?;
            let revision = r.u32()?;
            if uid != self.uid {
                return Err(ErrorCode::Malformed);
            }
            self.check_guard(&p[8..])?;
            // Unsupported adapters fail before state admission, matching the reference.
            self.driver.read_configuration(schema)?;
            if self.driver.configuration_busy() {
                return Err(ErrorCode::Busy);
            }
            if schema == 3 {
                self.check_live(f.session)?;
            } else if schema != 4
                && (!matches!(self.state, State::Safe | State::Fault)
                    || self.source != Source::None
                    || self.owner != 0)
            {
                return Err(ErrorCode::State);
            }
            if schema == 2 {
                if self.state != State::Safe
                    || self.fault != Fault::None
                    || !self.driver.safe_confirmed(now)
                {
                    return Err(ErrorCode::State);
                }
                if self.generation >= u32::MAX - 1 {
                    return Err(ErrorCode::Space);
                }
            }
            self.driver.write_configuration(
                ConfigurationWrite {
                    uid,
                    guard,
                    schema,
                    revision,
                    values: r.rest(),
                },
                f.session,
                f.sequence,
                now,
            )?;
            if schema == 2 {
                self.clear();
                self.advance_generation();
                self.driver.flush_remote();
                self.changed();
            }
            if schema == 3 {
                self.changed();
            }
            return Ok((Kind::Ack, self.ack().encode()));
        }
        if c == Command::Hello as u16 || c == Command::Info as u16 {
            r.end()?;
            return Ok((
                Kind::Response,
                Info {
                    uid: self.uid,
                    device_type: self.profile.device_type,
                    firmware: self.profile.firmware,
                    boot: self.boot,
                    profile_version: self.profile.version,
                    epoch: self.epoch,
                }
                .encode(),
            ));
        }
        if c == Command::Caps as u16 {
            let cursor = r.u16()?;
            r.end()?;
            return Ok((Kind::Response, self.profile.caps(cursor)?));
        }
        if c == Command::Status as u16 {
            let cursor = r.u16()?;
            r.end()?;
            return Ok((Kind::Response, self.status(cursor, now)?.encode()?));
        }
        if c == Command::ActionStatus as u16 {
            let op = r.u64()?;
            r.end()?;
            return Ok((
                Kind::Response,
                self.results
                    .iter()
                    .flatten()
                    .find(|x| x.operation == op)
                    .ok_or(ErrorCode::NotFound)?
                    .encode(),
            ));
        }
        if c == Command::Stop as u16 || c == Command::Estop as u16 {
            if p.len() != 20 {
                return Err(ErrorCode::Malformed);
            }
            self.check_guard(p)?;
            self.stop(
                now,
                if c == Command::Estop as u16 {
                    Fault::Estop
                } else {
                    Fault::None
                },
            );
            return Ok((Kind::Ack, self.ack().encode()));
        }
        if ![
            Command::SetSource,
            Command::Heartbeat,
            Command::WriteTargets,
            Command::ExecuteAction,
            Command::ResetFault,
        ]
        .iter()
        .any(|x| *x as u16 == c)
        {
            return Err(ErrorCode::Unsupported);
        }
        self.check_guard(p)?;
        r.raw(20)?;
        if c == Command::SetSource as u16 {
            if self.driver.configuration_busy() {
                return Err(ErrorCode::Busy);
            }
            if p.len() != 29 {
                return Err(ErrorCode::Malformed);
            }
            let source = Source::decode(r.u8()?).map_err(|_| ErrorCode::Capability)?;
            let target = r.u32()?;
            let lease = r.u32()?;
            self.switch(source, f.session, target, lease, now)?;
        } else if c == Command::ResetFault as u16 {
            if self.driver.configuration_busy() {
                return Err(ErrorCode::Busy);
            }
            r.end()?;
            if self.state != State::Fault
                || !self.driver.safe_confirmed(now)
                || !self.driver.reset_allowed()
            {
                return Err(ErrorCode::Safety);
            }
            if !self.advance_generation() {
                return Err(ErrorCode::Space);
            }
            self.fault = Fault::None;
            self.state = State::Safe;
            self.changed();
        } else {
            if self.state != State::Active
                || self.source != Source::Mobocom
                || self.owner != f.session
            {
                return Err(ErrorCode::Owner);
            }
            if c == Command::Heartbeat as u16 {
                r.end()?;
                self.lease_until = now.saturating_add(self.lease_ttl as u64);
            } else if c == Command::WriteTargets as u16 {
                let n = r.u8()? as usize;
                if n > 16 {
                    return Err(ErrorCode::Malformed);
                }
                let mut entries = [Target::default(); 16];
                for t in &mut entries[..n] {
                    *t = Target {
                        endpoint: r.u16()?,
                        resource: r.u16()?,
                        value: r.i32()?,
                    };
                }
                r.end()?;
                self.accept_targets(&entries[..n], f.sequence, now)?;
            } else {
                let opcode = r.u16()?;
                self.action(opcode, r.rest(), now)?;
            }
        }
        Ok((Kind::Ack, self.ack().encode()))
    }
    fn check_live(&self, session: u64) -> Result<()> {
        if session != self.session_id {
            return Err(ErrorCode::Session);
        }
        if self.state != State::Active
            || self.source != Source::Mobocom
            || self.owner != session
            || self.fault != Fault::None
        {
            return Err(ErrorCode::Owner);
        }
        if self.operation != 0 {
            return Err(ErrorCode::Busy);
        }
        Ok(())
    }
    fn error(&self, e: ErrorCode) -> (Kind, Payload) {
        let mut b = Payload::default();
        b.u16(e as u16)
            .unwrap()
            .u16(0)
            .unwrap()
            .raw(self.ack().encode().as_slice())
            .unwrap();
        (Kind::Error, b)
    }
    fn open(&mut self, f: &Frame, raw: &[u8], now: u64) -> Result<Option<Datagram>> {
        let mut r = Reader::new(f.payload.as_slice());
        let boot = r.u64()?;
        let epoch = r.u64()?;
        let generation = r.u32()?;
        r.end()?;
        if boot != self.boot {
            return Err(ErrorCode::Boot);
        }
        if let Some(saved) = self.opening {
            if saved.session == f.session && saved.sequence == f.sequence {
                if raw == saved.request.as_slice() && f.session == self.session_id {
                    return Ok(Some(saved.reply));
                }
                return Err(ErrorCode::Conflict);
            }
        }
        if epoch != self.epoch {
            return Err(ErrorCode::Epoch);
        }
        if generation != self.generation {
            return Err(ErrorCode::Generation);
        }
        if self.driver.configuration_busy() {
            return Err(ErrorCode::Busy);
        }
        if self.state != State::Safe
            || self.source != Source::None
            || self.owner != 0
            || !self.driver.safe_confirmed(now)
        {
            return Err(ErrorCode::State);
        }
        let epoch = self.epoch.checked_add(1).ok_or(ErrorCode::Space)?;
        self.clear();
        self.epoch = epoch;
        self.generation = 1;
        self.local_sequence = 0;
        self.session_id = f.session;
        self.session = None;
        self.opening = None;
        self.safety = None;
        self.configuration = None;
        self.driver.flush_remote();
        self.changed();
        Ok(None)
    }
    /// Test/adapter convenience. Prefer receive/take_reply to enforce reply lifetime.
    pub fn transact(&mut self, raw: &[u8], now: u64) -> Option<Datagram> {
        self.tick_inner(now, false);
        if now < self.last_now {
            return None;
        }
        let f = Frame::decode(raw).ok()?;
        if f.kind != Kind::Request || f.src_uid != 0 {
            return None;
        }
        let c = f.command;
        let p = f.payload.as_slice();
        if f.dst_uid == 0 {
            if (c == Command::Stop as u16 || c == Command::Estop as u16) && p.is_empty() {
                self.stop(
                    now,
                    if c == Command::Estop as u16 {
                        Fault::Estop
                    } else {
                        Fault::None
                    },
                );
                return None;
            }
            if c == Command::Discover as u16 && p.is_empty() {
                return Frame {
                    kind: Kind::Response,
                    src_uid: self.uid,
                    dst_uid: 0,
                    sequence: f.sequence,
                    command: c,
                    session: f.session,
                    payload: Discovery {
                        device_type: self.profile.device_type,
                        firmware: self.profile.firmware,
                        boot: self.boot,
                        epoch: self.epoch,
                        generation: self.generation,
                    }
                    .encode(),
                }
                .encode()
                .ok();
            }
            return None;
        }
        if f.dst_uid != self.uid || c == Command::Discover as u16 {
            return None;
        }
        let read = [
            Command::Hello,
            Command::Info,
            Command::Caps,
            Command::Status,
            Command::ActionStatus,
            Command::ReadConfig,
        ]
        .iter()
        .any(|x| *x as u16 == c);
        let config = c == Command::WriteConfig as u16;
        let opening = c == Command::SessionOpen as u16;
        let safety = [Command::Stop, Command::Estop, Command::ResetFault]
            .iter()
            .any(|x| *x as u16 == c);
        let schema = if config && p.len() >= 34 {
            u16::from_le_bytes([p[28], p[29]])
        } else {
            0
        };
        let mut slot = 0u8;
        let mut error = None;
        if opening {
            match self.open(&f, raw, now) {
                Ok(Some(reply)) => return Some(reply),
                Ok(None) => slot = 1,
                Err(e) => error = Some(e),
            }
        } else {
            if config && (p.len() < 34 || Reader::new(p).u64().ok() != Some(self.uid)) {
                error = Some(ErrorCode::Malformed)
            } else if !read {
                error = self.check_guard(if config { &p[8..] } else { p }).err();
                if error == Some(ErrorCode::Generation) && schema != 3 && schema != 4 {
                    error = None;
                }
                if error.is_none() && !safety && !config && f.session != self.session_id {
                    error = Some(ErrorCode::Session)
                }
                if error.is_none() && schema == 3 {
                    error = self.check_live(f.session).err();
                }
            }
            let cached = if config {
                self.configuration
            } else if f.session == self.session_id {
                self.session
            } else if safety {
                self.safety
            } else {
                None
            };
            if error.is_none() {
                if let Some(saved) =
                    cached.filter(|x| x.session == f.session && f.sequence <= x.sequence)
                {
                    if f.sequence == saved.sequence && raw == saved.request.as_slice() {
                        return Some(saved.reply);
                    }
                    error = Some(if f.sequence == saved.sequence {
                        ErrorCode::Conflict
                    } else {
                        ErrorCode::Sequence
                    });
                } else {
                    slot = if config {
                        4
                    } else if f.session == self.session_id {
                        2
                    } else if safety {
                        3
                    } else {
                        0
                    };
                }
            }
        }
        let (kind, payload) = if let Some(e) = error {
            self.error(e)
        } else if opening {
            (Kind::Ack, self.ack().encode())
        } else {
            match self.command(&f, now) {
                Ok(r) => r,
                Err(e) => self.error(e),
            }
        };
        let reply = Frame {
            kind,
            src_uid: self.uid,
            dst_uid: 0,
            sequence: f.sequence,
            command: c,
            session: f.session,
            payload,
        }
        .encode()
        .ok()?;
        let cache = Some(Cache {
            session: f.session,
            sequence: f.sequence,
            request: Datagram::from_slice(raw).ok()?,
            reply,
        });
        match slot {
            1 => {
                self.opening = cache;
                self.session = cache;
            }
            2 => self.session = cache,
            3 => self.safety = cache,
            4 => self.configuration = cache,
            _ => {}
        }
        Some(reply)
    }
    pub fn receive(&mut self, raw: &[u8], now: u64) -> bool {
        self.reply = None;
        self.reply = self.transact(raw, now);
        self.reply_at = now;
        self.reply.is_some()
    }
    pub fn take_reply(&mut self, now: u64) -> Option<Datagram> {
        if now < self.reply_at {
            return None;
        }
        let reply = self.reply.take();
        if now - self.reply_at > 20_000 {
            None
        } else {
            reply
        }
    }
}
/// Borrowed local-input facade; queued messages must retain their captured epoch/generation.
pub struct LocalControlInput<'n, 'p, D: Driver> {
    node: &'n mut DeviceCore<'p, D>,
}
impl<'n, 'p, D: Driver> LocalControlInput<'n, 'p, D> {
    pub fn new(node: &'n mut DeviceCore<'p, D>) -> Self {
        Self { node }
    }
    pub fn targets(
        &mut self,
        epoch: u64,
        generation: u32,
        sequence: u32,
        entries: &[Target],
        now: u64,
    ) -> Result<()> {
        self.node
            .local_targets(epoch, generation, sequence, entries, now)
    }
    pub fn heartbeat(&mut self, epoch: u64, generation: u32, now: u64) -> Result<()> {
        self.node.local_heartbeat(epoch, generation, now)
    }
    pub fn action(
        &mut self,
        epoch: u64,
        generation: u32,
        sequence: u32,
        opcode: u16,
        data: &[u8],
        now: u64,
    ) -> Result<()> {
        self.node
            .local_action(epoch, generation, sequence, opcode, data, now)
    }
}
