//! Explicit-tick latest-snapshot scheduler. Software budgeting is not a physical timing guarantee.
use crate::{client::*, device_types::is_registered_device_type, model::*, wire::*};
#[derive(Clone, Copy, Debug)]
pub struct BudgetNode {
    pub uid: u64,
    pub hz: u32,
    pub max_target_hz: u16,
    pub resources: u8,
    pub status_hz: u32,
    pub status_pages: u8,
    pub heartbeat_hz: u32,
}
#[derive(Clone, Copy, Debug)]
pub struct Budget {
    pub bytes_per_second: u64,
    pub utilization: f64,
}
pub fn ethernet_bytes(datagram: u64) -> u64 {
    (datagram + 28).max(46) + 38
}
pub fn budget(nodes: &[BudgetNode]) -> Result<Budget> {
    let mut total = 0u64;
    for (i, n) in nodes.iter().enumerate() {
        if n.uid == 0
            || n.hz == 0
            || n.hz > n.max_target_hz as u32
            || n.resources == 0
            || n.resources > 16
            || n.status_pages == 0
            || n.status_pages > 2
            || nodes[..i].iter().any(|x| x.uid == n.uid)
        {
            return Err(ErrorCode::Range);
        }
        let targets =
            (ethernet_bytes(60 + 8 * n.resources as u64) + ethernet_bytes(79)) * n.hz as u64;
        let status = (ethernet_bytes(41) + ethernet_bytes(103 + 17 * 8))
            * n.status_hz as u64
            * n.status_pages as u64;
        let heartbeat = (ethernet_bytes(59) + ethernet_bytes(79)) * n.heartbeat_hz as u64;
        total = total
            .checked_add(targets + status + heartbeat)
            .ok_or(ErrorCode::Bandwidth)?;
    }
    if total > 10_000_000 {
        return Err(ErrorCode::Bandwidth);
    }
    Ok(Budget {
        bytes_per_second: total,
        utilization: total as f64 / 12_500_000.0,
    })
}
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct ResourceKey {
    pub endpoint: u16,
    pub resource: u16,
}
#[derive(Clone, Debug)]
pub struct Schedule {
    pub uid: u64,
    pub caps: Capabilities,
    pub status: Status,
    pub hz: u32,
    pub status_hz: u32,
    pub heartbeat_hz: u32,
    pub target_ttl_us: u32,
    pub lease_ttl_us: u32,
    pub resources: [ResourceKey; 16],
    pub resource_count: usize,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Phase {
    Ready,
    Running,
    FinalTarget,
    WaitApplied,
    Stop,
    WaitSafe,
    Finished,
    Failed,
}
#[derive(Clone, Copy)]
enum Job {
    Targets,
    Status,
    Heartbeat,
    Stop,
}
struct Slot {
    schedule: Schedule,
    version: u64,
    values: [Target; 16],
    has_values: bool,
    phase: Phase,
    target_due: u64,
    status_due: u64,
    heartbeat_due: u64,
    job: Option<(Ticket, Job)>,
    error: Option<ClientError>,
    deadline: u64,
    final_sequence: u32,
    status_cursor: u16,
    last_status: Status,
}
pub struct RealtimeScheduler<const N: usize = 32> {
    slots: [Option<Slot>; N],
    last_now: u64,
}
impl<const N: usize> Default for RealtimeScheduler<N> {
    fn default() -> Self {
        Self {
            slots: core::array::from_fn(|_| None),
            last_now: 0,
        }
    }
}
impl<const N: usize> RealtimeScheduler<N> {
    /// Reconfiguration replaces all slots only after full validation. Caller must first stop old runs.
    pub fn configure<const P: usize>(
        &mut self,
        client: &Client<P>,
        schedules: &[Schedule],
        host_overhead_us: u32,
    ) -> Result<Budget> {
        if schedules.is_empty()
            || schedules.len() > N
            || self
                .slots
                .iter()
                .flatten()
                .any(|s| !matches!(s.phase, Phase::Ready | Phase::Finished | Phase::Failed))
        {
            return Err(ErrorCode::State);
        }
        let mut nodes = [BudgetNode {
            uid: 0,
            hz: 0,
            max_target_hz: 0,
            resources: 0,
            status_hz: 0,
            status_pages: 0,
            heartbeat_hz: 0,
        }; N];
        for (i, s) in schedules.iter().enumerate() {
            let c = &s.caps;
            let a = s.status.ack;
            if client.needs_rearm(s.uid)
                || !c.complete()
                || !is_registered_device_type(c.device_type)
                || a.state != State::Active
                || a.source != Source::Mobocom
                || s.status.owner != client.session()
                || a.fault != 0
            {
                return Err(ErrorCode::Owner);
            }
            if s.hz == 0
                || s.resource_count == 0
                || s.resource_count > 16
                || !(c.target_min_us..=c.target_max_us).contains(&s.target_ttl_us)
                || !(c.lease_min_us..=c.lease_max_us).contains(&s.lease_ttl_us)
            {
                return Err(ErrorCode::Range);
            }
            let period = 1_000_000u64.div_ceil(s.hz as u64);
            let lease_period = period.min(if s.heartbeat_hz > 0 {
                1_000_000u64.div_ceil(s.heartbeat_hz as u64)
            } else {
                u64::MAX
            });
            if period + host_overhead_us as u64 >= s.target_ttl_us as u64
                || lease_period + host_overhead_us as u64 >= s.lease_ttl_us as u64
            {
                return Err(ErrorCode::Bandwidth);
            }
            let keys = &s.resources[..s.resource_count];
            let mut mask = 0u16;
            for (j, k) in keys.iter().enumerate() {
                if keys[..j].contains(k) {
                    return Err(ErrorCode::Malformed);
                }
                let ri = c.resources[..c.resource_count]
                    .iter()
                    .position(|r| r.endpoint == k.endpoint && r.resource == k.resource)
                    .ok_or(ErrorCode::Capability)?;
                mask |= 1 << ri;
            }
            if s.status.active_mask & !mask != 0 {
                return Err(ErrorCode::State);
            }
            for (j, r) in c.resources[..c.resource_count].iter().enumerate() {
                if mask & (1 << j) != 0
                    && r.group != 0
                    && c.resources[..c.resource_count]
                        .iter()
                        .enumerate()
                        .any(|(k, x)| x.group == r.group && mask & (1 << k) == 0)
                {
                    return Err(ErrorCode::Group);
                }
            }
            nodes[i] = BudgetNode {
                uid: s.uid,
                hz: s.hz,
                max_target_hz: c.max_target_hz,
                resources: s.resource_count as u8,
                status_hz: s.status_hz,
                status_pages: c.resource_count.div_ceil(8) as u8,
                heartbeat_hz: s.heartbeat_hz,
            };
        }
        let report = budget(&nodes[..schedules.len()])?;
        self.slots = core::array::from_fn(|i| {
            schedules.get(i).map(|s| Slot {
                schedule: s.clone(),
                version: client.connection_version(s.uid).unwrap(),
                values: [Target::default(); 16],
                has_values: false,
                phase: Phase::Ready,
                target_due: 0,
                status_due: 0,
                heartbeat_due: 0,
                job: None,
                error: None,
                deadline: 0,
                final_sequence: 0,
                status_cursor: 0,
                last_status: s.status,
            })
        });
        Ok(report)
    }
    fn slot(&mut self, uid: u64) -> Result<&mut Slot> {
        self.slots
            .iter_mut()
            .flatten()
            .find(|s| s.schedule.uid == uid)
            .ok_or(ErrorCode::Address)
    }
    fn validate(s: &Slot, values: &[Target]) -> Result<()> {
        if values.len() != s.schedule.resource_count {
            return Err(ErrorCode::Malformed);
        }
        for (i, v) in values.iter().enumerate() {
            if values[..i]
                .iter()
                .any(|x| x.endpoint == v.endpoint && x.resource == v.resource)
                || !s.schedule.resources[..s.schedule.resource_count].contains(&ResourceKey {
                    endpoint: v.endpoint,
                    resource: v.resource,
                })
            {
                return Err(ErrorCode::Capability);
            }
            let r = s.schedule.caps.resources[..s.schedule.caps.resource_count]
                .iter()
                .find(|r| r.endpoint == v.endpoint && r.resource == v.resource)
                .ok_or(ErrorCode::Capability)?;
            if !(r.minimum..=r.maximum).contains(&v.value) {
                return Err(ErrorCode::Range);
            }
        }
        Ok(())
    }
    pub fn set_targets<const P: usize>(
        &mut self,
        client: &mut Client<P>,
        uid: u64,
        values: &[Target],
    ) -> Result<()> {
        let s = self.slot(uid)?;
        if client.connection_version(uid) != Some(s.version) || client.needs_rearm(uid) {
            return Err(ErrorCode::State);
        }
        if !matches!(s.phase, Phase::Ready | Phase::Running) {
            return Err(ErrorCode::State);
        }
        Self::validate(s, values)?;
        if s.has_values && s.values[..values.len()] != *values {
            client.cancel_target_retries(uid)?;
        }
        s.values[..values.len()].copy_from_slice(values);
        s.has_values = true;
        Ok(())
    }
    pub fn start<const P: usize>(&mut self, client: &Client<P>) -> Result<()> {
        if self.slots.iter().all(Option::is_none)
            || self.slots.iter().flatten().any(|s| {
                s.phase != Phase::Ready
                    || !s.has_values
                    || client.needs_rearm(s.schedule.uid)
                    || client.connection_version(s.schedule.uid) != Some(s.version)
            })
        {
            return Err(ErrorCode::State);
        }
        for s in self.slots.iter_mut().flatten() {
            s.phase = Phase::Running;
        }
        Ok(())
    }
    pub fn phase(&self, uid: u64) -> Option<Phase> {
        self.slots
            .iter()
            .flatten()
            .find(|s| s.schedule.uid == uid)
            .map(|s| s.phase)
    }
    pub fn error(&self, uid: u64) -> Option<ClientError> {
        self.slots
            .iter()
            .flatten()
            .find(|s| s.schedule.uid == uid)
            .and_then(|s| s.error)
    }
    pub fn last_status(&self, uid: u64) -> Option<Status> {
        self.slots
            .iter()
            .flatten()
            .find(|s| s.schedule.uid == uid)
            .map(|s| s.last_status)
    }
    fn fail<const P: usize>(s: &mut Slot, client: &mut Client<P>, error: ClientError) {
        s.phase = Phase::Failed;
        s.error = Some(error);
        s.has_values = false;
        s.job = None;
        let _ = client.discard_targets(s.schedule.uid);
    }
    pub fn tick<const P: usize>(&mut self, client: &mut Client<P>, now: u64) {
        let backwards = now < self.last_now;
        self.last_now = self.last_now.max(now);
        for s in self.slots.iter_mut().flatten() {
            if matches!(s.phase, Phase::Ready | Phase::Finished | Phase::Failed) {
                continue;
            }
            let uid = s.schedule.uid;
            if backwards
                || client.connection_version(uid) != Some(s.version)
                || client.needs_rearm(uid)
            {
                Self::fail(s, client, ClientError::ConnectionChanged);
                continue;
            }
            if s.phase != Phase::Running && now >= s.deadline {
                Self::fail(s, client, ClientError::Timeout);
                continue;
            }
            if let Some((ticket, job)) = s.job {
                if let Some(result) = client.take_result(ticket) {
                    s.job = None;
                    match result {
                        Err(ClientError::Superseded)
                            if matches!(job, Job::Targets) && s.phase == Phase::Running =>
                        {
                            s.target_due = now;
                        }
                        Err(e) => {
                            Self::fail(s, client, e);
                            continue;
                        }
                        Ok(frame) => {
                            if matches!(job, Job::Status) {
                                let status = match Status::decode(frame.payload.as_slice()) {
                                    Ok(s) => s,
                                    Err(_) => {
                                        Self::fail(s, client, ClientError::Malformed);
                                        continue;
                                    }
                                };
                                s.last_status = status;
                                if s.phase == Phase::WaitSafe {
                                    if status.ack.state == State::Safe
                                        && status.ack.source == Source::None
                                        && status.ack.fault == 0
                                        && status.safe_confirmed
                                    {
                                        s.phase = Phase::Finished;
                                    } else if status.ack.fault != 0 {
                                        Self::fail(
                                            s,
                                            client,
                                            ClientError::Protocol {
                                                code: ErrorCode::Safety as u16,
                                                detail: 0,
                                                snapshot: status.ack,
                                            },
                                        );
                                        continue;
                                    }
                                } else if status.ack.guard != s.schedule.status.ack.guard
                                    || status.owner != client.session()
                                    || status.ack.state != State::Active
                                    || status.ack.source != Source::Mobocom
                                    || status.ack.fault != 0
                                {
                                    Self::fail(s, client, ClientError::ConnectionChanged);
                                    continue;
                                } else if s.phase == Phase::WaitApplied
                                    && status.ack.applied == s.final_sequence
                                {
                                    s.phase = Phase::Stop;
                                }
                                if s.phase == Phase::Running {
                                    s.status_cursor = if status.next == u16::MAX {
                                        0
                                    } else {
                                        status.next
                                    };
                                    if status.next != u16::MAX {
                                        s.status_due = now;
                                    }
                                }
                            } else if matches!(job, Job::Targets) && s.phase == Phase::FinalTarget {
                                s.final_sequence = ticket.sequence;
                                s.phase = Phase::WaitApplied;
                            } else if matches!(job, Job::Stop) {
                                s.phase = Phase::WaitSafe;
                            }
                        }
                    }
                }
            }
            if s.job.is_some()
                || client.busy(uid)
                || matches!(s.phase, Phase::Finished | Phase::Failed)
            {
                continue;
            }
            let g = s.schedule.status.ack.guard;
            let next = match s.phase {
                Phase::FinalTarget => Some((
                    client.write_targets(uid, g, &s.values[..s.schedule.resource_count]),
                    Job::Targets,
                )),
                Phase::WaitApplied | Phase::WaitSafe => {
                    Some((client.get_status(uid, 0), Job::Status))
                }
                Phase::Stop => Some((
                    client
                        .stop(uid, Some(g), false)
                        .and_then(|t| t.ok_or(ErrorCode::State)),
                    Job::Stop,
                )),
                Phase::Running => {
                    if now >= s.target_due {
                        let period = (1_000_000 / s.schedule.hz as u64).max(1);
                        s.target_due = if s.target_due == 0 {
                            now.saturating_add(period)
                        } else {
                            s.target_due.saturating_add(
                                ((now - s.target_due) / period + 1).saturating_mul(period),
                            )
                        };
                        Some((
                            client.write_targets(uid, g, &s.values[..s.schedule.resource_count]),
                            Job::Targets,
                        ))
                    } else if s.schedule.status_hz > 0 && now >= s.status_due {
                        s.status_due =
                            now.saturating_add((1_000_000 / s.schedule.status_hz as u64).max(1));
                        Some((client.get_status(uid, s.status_cursor), Job::Status))
                    } else if s.schedule.heartbeat_hz > 0 && now >= s.heartbeat_due {
                        s.heartbeat_due =
                            now.saturating_add((1_000_000 / s.schedule.heartbeat_hz as u64).max(1));
                        Some((client.heartbeat(uid, g), Job::Heartbeat))
                    } else {
                        None
                    }
                }
                _ => None,
            };
            if let Some((result, job)) = next {
                match result {
                    Ok(ticket) => s.job = Some((ticket, job)),
                    Err(_) => Self::fail(s, client, ClientError::ConnectionChanged),
                }
            }
        }
    }
    /// Per-node finish: final target -> applied sequence -> STOP -> confirmed SAFE.
    /// applied means driver handoff, never physical arrival. Other nodes keep publishing.
    pub fn finish<const P: usize>(
        &mut self,
        client: &mut Client<P>,
        uid: u64,
        values: &[Target],
        now: u64,
        timeout_us: u64,
    ) -> Result<()> {
        let s = self.slot(uid)?;
        if client.connection_version(uid) != Some(s.version)
            || client.needs_rearm(uid)
            || !matches!(s.phase, Phase::Running | Phase::Ready)
            || timeout_us == 0
        {
            return Err(ErrorCode::State);
        }
        Self::validate(s, values)?;
        client.discard_targets(uid)?;
        s.values[..values.len()].copy_from_slice(values);
        s.phase = Phase::FinalTarget;
        s.deadline = now.checked_add(timeout_us).ok_or(ErrorCode::Range)?;
        s.job = None;
        Ok(())
    }
    pub fn stop<const P: usize>(
        &mut self,
        client: &mut Client<P>,
        uid: u64,
        estop: bool,
        now: u64,
        timeout_us: u64,
    ) -> Result<()> {
        let s = self.slot(uid)?;
        let guard = client
            .status(uid)
            .map(|s| s.ack.guard)
            .unwrap_or(s.schedule.status.ack.guard);
        let ticket = client
            .stop(uid, Some(guard), estop)?
            .ok_or(ErrorCode::State)?;
        s.phase = Phase::Stop;
        s.has_values = false;
        s.job = Some((ticket, Job::Stop));
        s.deadline = now.saturating_add(timeout_us);
        Ok(())
    }
}
