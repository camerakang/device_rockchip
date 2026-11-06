//! Nonblocking, fixed-capacity host client. One transaction per UID, independent discovery.
//! Applications drive next_transmission/feed with monotonic microseconds and bound their I/O.
use crate::{configuration::*, model::*, wire::*};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Endpoint {
    pub address: [u8; 4],
    pub port: u16,
}
impl Endpoint {
    pub fn validate(self) -> Result<()> {
        if self.port == 0 || self.address == [0; 4] || self.address[0] >= 224 {
            return Err(ErrorCode::Address);
        }
        Ok(())
    }
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Ticket {
    pub uid: u64,
    pub sequence: u32,
    pub connection_version: u64,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ClientError {
    Protocol {
        code: u16,
        detail: u16,
        snapshot: Ack,
    },
    Timeout,
    Superseded,
    ConnectionChanged,
    Transport,
    Closed,
    Malformed,
}
pub type TransactionResult = core::result::Result<Frame, ClientError>;
#[derive(Clone, Copy, Debug)]
pub struct Transmission {
    pub endpoint: Option<Endpoint>,
    pub frame: Datagram,
    pub sequence: u32,
}
#[derive(Clone, Copy)]
struct Transaction {
    ticket: Ticket,
    frame: Frame,
    raw: Datagram,
    sent_at: Option<u64>,
    attempts: u8,
    max_attempts: u8,
    no_retry: bool,
}
#[derive(Clone, Copy)]
struct TargetBatch {
    guard: Guard,
    values: [Target; 16],
    len: usize,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ConnectionState {
    Ready,
    Probing,
    Unconfirmed,
    Conflict,
}
#[derive(Clone, Copy)]
struct Candidate {
    endpoint: Endpoint,
    discovery: Option<Discovery>,
    probe: Option<Frame>,
    sent_at: Option<u64>,
    identity: Option<Info>,
    done: bool,
}
impl Candidate {
    fn new(endpoint: Endpoint, discovery: Option<Discovery>) -> Self {
        Self {
            endpoint,
            discovery,
            probe: None,
            sent_at: None,
            identity: None,
            done: false,
        }
    }
}
#[derive(Clone, Copy)]
struct Peer {
    uid: u64,
    endpoint: Option<Endpoint>,
    identity: Option<Discovery>,
    version: u64,
    needs_rearm: bool,
    state: ConnectionState,
    pending: Option<Transaction>,
    completed: Option<(Ticket, TransactionResult)>,
    targets: Option<TargetBatch>,
    status: Option<Status>,
    candidates: [Option<Candidate>; 4],
    candidate_overflow: bool,
    seen: bool,
}
impl Peer {
    fn new(uid: u64) -> Self {
        Self {
            uid,
            endpoint: None,
            identity: None,
            version: 0,
            needs_rearm: false,
            state: ConnectionState::Unconfirmed,
            pending: None,
            completed: None,
            targets: None,
            status: None,
            candidates: [None; 4],
            candidate_overflow: false,
            seen: false,
        }
    }
}
#[derive(Clone, Copy, Debug)]
pub struct DiscoveredPeer {
    pub uid: u64,
    pub endpoint: Option<Endpoint>,
    pub identity: Option<Discovery>,
    pub state: ConnectionState,
    pub seen: bool,
    pub needs_rearm: bool,
    pub connection_version: u64,
}
struct Scan {
    frame: Frame,
    raw: Datagram,
    sent_at: Option<u64>,
    probing: bool,
}

pub struct Client<const N: usize = 32> {
    session: u64,
    probe_session: u64,
    sequence: u32,
    timeout_us: u64,
    peers: [Option<Peer>; N],
    scan: Option<Scan>,
    scan_done: bool,
    scan_overflow: bool,
    emergency: Option<Transmission>,
    closed: bool,
    last_now: u64,
    round_robin: usize,
}
impl<const N: usize> Client<N> {
    /// Caller supplies a fresh nonzero unpredictable session ID (OS entropy on a host).
    pub fn new(session: u64, transaction_timeout_ms: u32) -> Result<Self> {
        if session == 0 || !(30..=2000).contains(&transaction_timeout_ms) || N == 0 {
            return Err(ErrorCode::Range);
        }
        let probe_session = if session ^ 0x8000000000000000 == 0 {
            1
        } else {
            session ^ 0x8000000000000000
        };
        Ok(Self {
            session,
            probe_session,
            sequence: 0,
            timeout_us: transaction_timeout_ms as u64 * 1000,
            peers: [None; N],
            scan: None,
            scan_done: false,
            scan_overflow: false,
            emergency: None,
            closed: false,
            last_now: 0,
            round_robin: 0,
        })
    }
    pub fn session(&self) -> u64 {
        self.session
    }
    pub fn needs_session_renewal(&self) -> bool {
        self.sequence >= 0xfffff000
    }
    fn next(&mut self) -> Result<u32> {
        if self.closed {
            return Err(ErrorCode::State);
        }
        self.sequence = self.sequence.checked_add(1).ok_or(ErrorCode::Space)?;
        Ok(self.sequence)
    }
    fn index(&self, uid: u64) -> Result<usize> {
        self.peers
            .iter()
            .position(|p| p.is_some_and(|p| p.uid == uid))
            .ok_or(ErrorCode::Address)
    }
    fn ensure(&mut self, uid: u64) -> Result<usize> {
        if uid == 0 {
            return Err(ErrorCode::Address);
        }
        if let Ok(i) = self.index(uid) {
            return Ok(i);
        }
        let i = self
            .peers
            .iter()
            .position(Option::is_none)
            .ok_or(ErrorCode::Space)?;
        self.peers[i] = Some(Peer::new(uid));
        Ok(i)
    }
    pub fn set_endpoint(&mut self, uid: u64, endpoint: Endpoint) -> Result<()> {
        endpoint.validate()?;
        let i = self.ensure(uid)?;
        let p = self.peers[i].as_mut().unwrap();
        if p.endpoint.is_some_and(|old| old != endpoint) {
            return Err(ErrorCode::Address);
        }
        p.endpoint = Some(endpoint);
        p.state = ConnectionState::Ready;
        Ok(())
    }
    pub fn peer(&self, uid: u64) -> Option<DiscoveredPeer> {
        let p = self.peers[self.index(uid).ok()?]?;
        Some(DiscoveredPeer {
            uid,
            endpoint: p.endpoint,
            identity: p.identity,
            state: p.state,
            seen: p.seen,
            needs_rearm: p.needs_rearm,
            connection_version: p.version,
        })
    }
    pub fn peers(&self) -> impl Iterator<Item = DiscoveredPeer> + '_ {
        self.peers.iter().flatten().map(|p| DiscoveredPeer {
            uid: p.uid,
            endpoint: p.endpoint,
            identity: p.identity,
            state: p.state,
            seen: p.seen,
            needs_rearm: p.needs_rearm,
            connection_version: p.version,
        })
    }
    pub fn status(&self, uid: u64) -> Option<Status> {
        self.peers[self.index(uid).ok()?]?.status
    }
    pub fn busy(&self, uid: u64) -> bool {
        self.index(uid)
            .ok()
            .and_then(|i| self.peers[i])
            .is_some_and(|p| p.pending.is_some())
    }
    pub fn connection_version(&self, uid: u64) -> Option<u64> {
        self.peer(uid).map(|p| p.connection_version)
    }
    pub fn needs_rearm(&self, uid: u64) -> bool {
        self.peer(uid)
            .is_none_or(|p| p.needs_rearm || p.state != ConnectionState::Ready)
    }
    fn invalidate(p: &mut Peer) {
        p.version = p.version.saturating_add(1);
        p.needs_rearm = true;
        p.state = ConnectionState::Probing;
        p.targets = None;
        p.status = None;
        if let Some(t) = p.pending.take() {
            p.completed = Some((t.ticket, Err(ClientError::ConnectionChanged)));
        }
    }
    pub fn cancel_target_retries(&mut self, uid: u64) -> Result<()> {
        let i = self.index(uid)?;
        if let Some(t) = self.peers[i].as_mut().unwrap().pending.as_mut() {
            if t.frame.command == Command::WriteTargets as u16 {
                t.no_retry = true;
            }
        }
        Ok(())
    }
    pub fn discard_targets(&mut self, uid: u64) -> Result<()> {
        let i = self.index(uid)?;
        let p = self.peers[i].as_mut().unwrap();
        p.targets = None;
        if let Some(t) = p.pending.as_mut() {
            if t.frame.command == Command::WriteTargets as u16 {
                if t.sent_at.is_none() {
                    let ticket = t.ticket;
                    p.pending = None;
                    p.completed = Some((ticket, Err(ClientError::Superseded)));
                } else {
                    t.no_retry = true;
                }
            }
        }
        Ok(())
    }
    fn allowed_rearm(command: Command) -> bool {
        matches!(
            command,
            Command::Hello
                | Command::Info
                | Command::Caps
                | Command::Status
                | Command::ActionStatus
                | Command::ReadConfig
                | Command::Stop
                | Command::Estop
                | Command::ResetFault
                | Command::SessionOpen
        )
    }
    /// Low-level typed request. Configuration writes are always single-send in Rust.
    /// Consume the returned ticket before issuing a later request if its result is needed.
    pub fn request(&mut self, uid: u64, command: Command, payload: Payload) -> Result<Ticket> {
        let i = self.index(uid)?;
        let p = self.peers[i].as_ref().unwrap();
        if p.state != ConnectionState::Ready || p.endpoint.is_none() {
            return Err(ErrorCode::Address);
        }
        if p.needs_rearm && !Self::allowed_rearm(command) {
            return Err(ErrorCode::State);
        }
        if p.pending.is_some() {
            return Err(ErrorCode::Busy);
        }
        if matches!(command, Command::Discover) {
            return Err(ErrorCode::Address);
        }
        let sequence = self.next()?;
        let frame = Frame::request(uid, sequence, command, self.session, payload);
        let raw = frame.encode()?;
        let p = self.peers[i].as_mut().unwrap();
        let ticket = Ticket {
            uid,
            sequence,
            connection_version: p.version,
        };
        p.pending = Some(Transaction {
            ticket,
            frame,
            raw,
            sent_at: None,
            attempts: 0,
            max_attempts: if command == Command::WriteConfig {
                1
            } else {
                3
            },
            no_retry: false,
        });
        Ok(ticket)
    }
    pub fn request_record(&self, ticket: Ticket) -> Option<Datagram> {
        let p = self.peers[self.index(ticket.uid).ok()?]?;
        p.pending.filter(|t| t.ticket == ticket).map(|t| t.raw)
    }
    pub fn take_result(&mut self, ticket: Ticket) -> Option<TransactionResult> {
        let i = match self.index(ticket.uid) {
            Ok(i) => i,
            Err(_) => return Some(Err(ClientError::ConnectionChanged)),
        };
        let p = self.peers[i].as_mut().unwrap();
        if p.completed.is_some_and(|(t, _)| t == ticket) {
            return p.completed.take().map(|(_, r)| r);
        }
        if p.version != ticket.connection_version {
            return Some(Err(ClientError::ConnectionChanged));
        }
        if p.pending.is_some_and(|t| t.ticket == ticket) {
            None
        } else {
            Some(Err(ClientError::Superseded))
        }
    }
    pub fn get_info(&mut self, uid: u64) -> Result<Ticket> {
        self.request(uid, Command::Info, Payload::default())
    }
    pub fn get_caps(&mut self, uid: u64, cursor: u16) -> Result<Ticket> {
        let mut b = Payload::default();
        b.u16(cursor)?;
        self.request(uid, Command::Caps, b)
    }
    pub fn get_status(&mut self, uid: u64, cursor: u16) -> Result<Ticket> {
        let mut b = Payload::default();
        b.u16(cursor)?;
        self.request(uid, Command::Status, b)
    }
    pub fn open_session(&mut self, uid: u64, g: Guard) -> Result<Ticket> {
        let mut b = Payload::default();
        b.u64(g.boot)?.u64(g.epoch)?.u32(g.generation)?;
        self.request(uid, Command::SessionOpen, b)
    }
    pub fn set_source(
        &mut self,
        uid: u64,
        g: Guard,
        source: Source,
        target_ttl_us: u32,
        lease_ttl_us: u32,
    ) -> Result<Ticket> {
        let mut b = g.encode();
        b.u8(source as u8)?.u32(target_ttl_us)?.u32(lease_ttl_us)?;
        self.request(uid, Command::SetSource, b)
    }
    pub fn heartbeat(&mut self, uid: u64, g: Guard) -> Result<Ticket> {
        self.request(uid, Command::Heartbeat, g.encode())
    }
    pub fn reset_fault(&mut self, uid: u64, g: Guard) -> Result<Ticket> {
        self.request(uid, Command::ResetFault, g.encode())
    }
    pub fn execute_action(
        &mut self,
        uid: u64,
        g: Guard,
        opcode: u16,
        parameters: &[u8],
    ) -> Result<Ticket> {
        let mut b = g.encode();
        b.u16(opcode)?.raw(parameters)?;
        self.request(uid, Command::ExecuteAction, b)
    }
    pub fn action_status(&mut self, uid: u64, operation: u64) -> Result<Ticket> {
        let mut b = Payload::default();
        b.u64(operation)?;
        self.request(uid, Command::ActionStatus, b)
    }
    pub fn read_configuration(&mut self, uid: u64, schema: u16) -> Result<Ticket> {
        let mut b = Payload::default();
        b.u16(schema)?;
        let ticket = self.request(uid, Command::ReadConfig, b)?;
        if schema == 3 || schema == 4 {
            self.peers[self.index(uid)?]
                .as_mut()
                .unwrap()
                .pending
                .as_mut()
                .unwrap()
                .max_attempts = 1;
        }
        Ok(ticket)
    }
    /// Does not discard continuous targets. Insert only in a measured spare TTL budget.
    /// Save request_record(ticket) before pumping I/O; on timeout only query association.
    pub fn write_configuration(&mut self, request: ConfigurationWrite<'_>) -> Result<Ticket> {
        self.request(request.uid, Command::WriteConfig, request.encode()?)
    }
    pub fn write_motion_profile(
        &mut self,
        uid: u64,
        g: Guard,
        revision: u32,
        limits: &[MotionLimit],
        live: bool,
    ) -> Result<Ticket> {
        let b = motion_limits(limits)?;
        self.write_configuration(ConfigurationWrite {
            uid,
            guard: g,
            schema: if live { 3 } else { 2 },
            revision,
            values: b.as_slice(),
        })
    }
    pub fn write_focus_source(
        &mut self,
        uid: u64,
        g: Guard,
        revision: u32,
        source: FocusSource,
    ) -> Result<Ticket> {
        let b = focus_source(source)?;
        self.write_configuration(ConfigurationWrite {
            uid,
            guard: g,
            schema: 4,
            revision,
            values: &b,
        })
    }
    pub fn write_focus_command(
        &mut self,
        uid: u64,
        g: Guard,
        revision: u32,
        status: FocusStatus,
        command: &[u8],
    ) -> Result<Ticket> {
        if !status.wired_commands {
            return Err(ErrorCode::Capability);
        }
        validate_focus_command(command)?;
        self.write_configuration(ConfigurationWrite {
            uid,
            guard: g,
            schema: 4,
            revision,
            values: command,
        })
    }
    pub fn write_targets(&mut self, uid: u64, g: Guard, values: &[Target]) -> Result<Ticket> {
        self.request(uid, Command::WriteTargets, targets(g, values)?)
    }
    /// Latest unsent values merge by resource, including distinct resources. Sent bytes stay immutable.
    pub fn queue_targets(&mut self, uid: u64, g: Guard, values: &[Target]) -> Result<()> {
        targets(g, values)?;
        let i = self.index(uid)?;
        let p = self.peers[i].as_mut().unwrap();
        if p.needs_rearm || p.state != ConnectionState::Ready {
            return Err(ErrorCode::State);
        }
        let mut batch = p.targets.unwrap_or(TargetBatch {
            guard: g,
            values: [Target::default(); 16],
            len: 0,
        });
        if let Some(t) = p
            .pending
            .filter(|t| t.sent_at.is_none() && t.frame.command == Command::WriteTargets as u16)
        {
            let mut r = Reader::new(t.frame.payload.as_slice());
            batch.guard = Guard::read(&mut r)?;
            let count = r.u8()? as usize;
            if count == 0 || count > 16 {
                return Err(ErrorCode::Malformed);
            }
            batch.len = count;
            for item in &mut batch.values[..count] {
                *item = Target {
                    endpoint: r.u16()?,
                    resource: r.u16()?,
                    value: r.i32()?,
                };
            }
            r.end()?;
        }
        if batch.guard != g {
            return Err(ErrorCode::Generation);
        }
        for (n, v) in values.iter().enumerate() {
            if values[..n]
                .iter()
                .any(|x| x.endpoint == v.endpoint && x.resource == v.resource)
            {
                return Err(ErrorCode::Malformed);
            }
            if let Some(k) = batch.values[..batch.len]
                .iter()
                .position(|x| x.endpoint == v.endpoint && x.resource == v.resource)
            {
                batch.values[k] = *v;
            } else {
                if batch.len == 16 {
                    return Err(ErrorCode::Space);
                }
                batch.values[batch.len] = *v;
                batch.len += 1;
            }
        }
        if let Some(t) = p.pending.as_mut() {
            if t.frame.command == Command::WriteTargets as u16 {
                if t.sent_at.is_none() {
                    t.frame.payload = targets(g, &batch.values[..batch.len])?;
                    t.raw = t.frame.encode()?;
                    t.no_retry = false;
                    p.targets = None;
                    return Ok(());
                }
                t.no_retry = true;
            }
        }
        p.targets = Some(batch);
        Ok(())
    }
    /// STOP preempts the UID transaction. For all nodes it bypasses discovery and every queue.
    /// A unicast guard must be queried explicitly; this API never guesses new motion credentials.
    pub fn stop(&mut self, uid: u64, guard: Option<Guard>, estop: bool) -> Result<Option<Ticket>> {
        let command = if estop { Command::Estop } else { Command::Stop };
        if uid == 0 {
            let sequence = self.next()?;
            for p in self.peers.iter_mut().flatten() {
                p.targets = None;
                if let Some(t) = p.pending.take() {
                    p.completed = Some((t.ticket, Err(ClientError::Superseded)));
                }
            }
            self.scan = None;
            self.scan_done = false;
            let frame =
                Frame::request(0, sequence, command, self.session, Payload::default()).encode()?;
            // Never downgrade an unsent emergency stop to a normal stop.
            let pending_estop = self.emergency.is_some_and(|t| {
                Frame::decode(t.frame.as_slice()).is_ok_and(|f| f.command == Command::Estop as u16)
            });
            if estop || !pending_estop {
                self.emergency = Some(Transmission {
                    endpoint: None,
                    frame,
                    sequence,
                });
            }
            return Ok(None);
        }
        let g = guard.ok_or(ErrorCode::Generation)?;
        let i = self.index(uid)?;
        let p = self.peers[i].as_mut().unwrap();
        p.targets = None;
        if let Some(t) = p.pending.take() {
            p.completed = Some((t.ticket, Err(ClientError::Superseded)));
        }
        let ticket = self.request(uid, command, g.encode())?;
        Ok(Some(ticket))
    }
    /// Starts one 100ms discovery round. Repeat explicitly if deployment requires more rounds.
    pub fn scan(&mut self) -> Result<()> {
        if self.scan.is_some() {
            return Err(ErrorCode::Busy);
        }
        let sequence = self.next()?;
        for p in self.peers.iter_mut().flatten() {
            if p.state == ConnectionState::Ready {
                p.candidates = [None; 4];
                p.candidate_overflow = false;
            } else {
                // Previously ambiguous addresses must be re-probed, even when DISCOVER is lost.
                for c in p.candidates.iter_mut().flatten() {
                    *c = Candidate::new(c.endpoint, None);
                }
            }
            p.seen = false;
        }
        let frame = Frame::request(
            0,
            sequence,
            Command::Discover,
            self.session,
            Payload::default(),
        );
        self.scan = Some(Scan {
            frame,
            raw: frame.encode()?,
            sent_at: None,
            probing: false,
        });
        self.scan_done = false;
        self.scan_overflow = false;
        Ok(())
    }
    pub fn reconnect(&mut self, uid: u64) -> Result<()> {
        if self.scan.is_some() {
            return Err(ErrorCode::Busy);
        }
        let i = self.index(uid)?;
        Self::invalidate(self.peers[i].as_mut().unwrap());
        self.scan()
    }
    pub fn scan_complete(&self) -> bool {
        self.scan_done
    }
    pub fn scan_overflow(&self) -> bool {
        self.scan_overflow
    }
    fn candidate(p: &mut Peer, endpoint: Endpoint, discovery: Option<Discovery>) {
        if let Some(c) = p
            .candidates
            .iter_mut()
            .flatten()
            .find(|c| c.endpoint == endpoint)
        {
            if discovery.is_some() {
                c.discovery = discovery;
            }
            return;
        }
        if let Some(slot) = p.candidates.iter_mut().find(|c| c.is_none()) {
            *slot = Some(Candidate::new(endpoint, discovery));
        } else {
            p.candidate_overflow = true;
        }
    }
    fn needs_probe(p: &Peer) -> bool {
        p.state != ConnectionState::Ready || p.identity.is_none() || p.endpoint.is_none()
    }
    fn finish_scan(&mut self) {
        for p in self.peers.iter_mut().flatten() {
            if !p.seen {
                if p.state == ConnectionState::Probing {
                    p.state = ConnectionState::Unconfirmed;
                }
                continue;
            }
            if !Self::needs_probe(p) {
                // A stable scan must not disrupt motion because an unnecessary INFO was lost.
                if let Some(d) = p
                    .candidates
                    .iter()
                    .flatten()
                    .find(|c| Some(c.endpoint) == p.endpoint)
                    .and_then(|c| c.discovery)
                {
                    p.identity = Some(d);
                }
                continue;
            }
            let count = p
                .candidates
                .iter()
                .flatten()
                .filter(|c| c.identity.is_some())
                .count();
            if count > 1 {
                if !p.needs_rearm {
                    Self::invalidate(p)
                }
                p.state = ConnectionState::Conflict;
                continue;
            }
            let valid = p.candidates.iter().flatten().find_map(|c| {
                let i = c.identity?;
                let d = c.discovery?;
                if i.boot == d.boot
                    && i.device_type == d.device_type
                    && i.firmware == d.firmware
                    && i.epoch == d.epoch
                {
                    Some((c.endpoint, d))
                } else {
                    None
                }
            });
            if p.candidate_overflow || count != 1 || valid.is_none() {
                if !p.needs_rearm {
                    Self::invalidate(p)
                }
                p.state = ConnectionState::Unconfirmed;
                continue;
            }
            let (endpoint, identity) = valid.unwrap();
            p.endpoint = Some(endpoint);
            p.identity = Some(identity);
            p.state = ConnectionState::Ready;
        }
        self.scan = None;
        self.scan_done = true;
    }
    fn maintain(&mut self, now: u64) {
        if now < self.last_now {
            for p in self.peers.iter_mut().flatten() {
                Self::invalidate(p);
            }
            self.scan = None;
            return;
        }
        self.last_now = now;
        for p in self.peers.iter_mut().flatten() {
            if let Some(t) = p.pending {
                if t.sent_at.is_some_and(|sent| now - sent >= self.timeout_us)
                    && (t.attempts >= t.max_attempts || t.no_retry)
                {
                    p.pending = None;
                    p.completed = Some((
                        t.ticket,
                        Err(if t.no_retry {
                            ClientError::Superseded
                        } else {
                            ClientError::Timeout
                        }),
                    ));
                }
            }
        }
        if self.scan.as_ref().is_some_and(|s| {
            !s.probing
                && s.sent_at
                    .is_some_and(|sent| now - sent >= DISCOVERY_WINDOW_US)
        }) {
            self.scan.as_mut().unwrap().probing = true;
            for p in self.peers.iter_mut().flatten() {
                if p.seen && Self::needs_probe(p) {
                    if let Some(old) = p.endpoint {
                        Self::candidate(p, old, None);
                    }
                }
            }
            for i in 0..N {
                for j in 0..4 {
                    if self.peers[i].is_some_and(|p| {
                        p.seen && Self::needs_probe(&p) && p.candidates[j].is_some()
                    }) {
                        match self.next() {
                            Ok(sequence) => {
                                let p = self.peers[i].as_mut().unwrap();
                                p.candidates[j].as_mut().unwrap().probe = Some(Frame::request(
                                    p.uid,
                                    sequence,
                                    Command::Info,
                                    self.probe_session,
                                    Payload::default(),
                                ));
                            }
                            Err(_) => {
                                self.peers[i].as_mut().unwrap().candidate_overflow = true;
                                self.peers[i].as_mut().unwrap().candidates[j]
                                    .as_mut()
                                    .unwrap()
                                    .done = true;
                            }
                        }
                    }
                }
            }
        }
        if self.scan.as_ref().is_some_and(|s| s.probing) {
            for p in self.peers.iter_mut().flatten() {
                for c in p.candidates.iter_mut().flatten() {
                    if c.sent_at.is_some_and(|sent| now - sent >= self.timeout_us) {
                        c.done = true;
                    }
                }
            }
            if self
                .peers
                .iter()
                .flatten()
                .filter(|p| p.seen && Self::needs_probe(p))
                .flat_map(|p| p.candidates.iter().flatten())
                .all(|c| c.done)
            {
                self.finish_scan();
            }
        }
    }
    /// Returns at most one datagram; send immediately, with a bounded adapter queue.
    /// Drain in a bounded loop every tick. There is no wait/sleep inside the SDK.
    pub fn next_transmission(&mut self, now: u64) -> Option<Transmission> {
        if self.closed {
            return None;
        }
        self.maintain(now);
        if now < self.last_now {
            return None;
        }
        if let Some(t) = self.emergency.take() {
            return Some(t);
        }
        // Unicast safety gets first opportunity across peers.
        for priority in [true, false] {
            for step in 0..N {
                let i = (self.round_robin + step) % N;
                let p = match self.peers[i] {
                    Some(p) => p,
                    None => continue,
                };
                let t = match p.pending {
                    Some(t) => t,
                    None => continue,
                };
                let safety = t.frame.command == Command::Stop as u16
                    || t.frame.command == Command::Estop as u16;
                if safety != priority {
                    continue;
                }
                if t.sent_at.is_none()
                    || t.sent_at.is_some_and(|sent| now - sent >= self.timeout_us)
                {
                    let p = self.peers[i].as_mut().unwrap();
                    let pending = p.pending.as_mut().unwrap();
                    pending.sent_at = Some(now);
                    pending.attempts += 1;
                    self.round_robin = (i + 1) % N;
                    return Some(Transmission {
                        endpoint: p.endpoint,
                        frame: t.raw,
                        sequence: t.frame.sequence,
                    });
                }
            }
        }
        // Start latest targets only after explicit transactions, but never block other UIDs.
        for i in 0..N {
            if let Some(p) = self.peers[i] {
                if p.pending.is_none() {
                    if let Some(batch) = p.targets {
                        if let Ok(ticket) =
                            self.write_targets(p.uid, batch.guard, &batch.values[..batch.len])
                        {
                            let p = self.peers[i].as_mut().unwrap();
                            p.targets = None;
                            let t = p.pending.as_mut().unwrap();
                            t.sent_at = Some(now);
                            t.attempts = 1;
                            return Some(Transmission {
                                endpoint: p.endpoint,
                                frame: t.raw,
                                sequence: ticket.sequence,
                            });
                        }
                    }
                }
            }
        }
        if let Some(scan) = self.scan.as_mut() {
            if scan.sent_at.is_none() {
                scan.sent_at = Some(now);
                return Some(Transmission {
                    endpoint: None,
                    frame: scan.raw,
                    sequence: scan.frame.sequence,
                });
            }
        }
        if self.scan.as_ref().is_some_and(|s| s.probing) {
            for p in self.peers.iter_mut().flatten() {
                for c in p.candidates.iter_mut().flatten() {
                    if !c.done && c.sent_at.is_none() {
                        if let Some(f) = c.probe {
                            c.sent_at = Some(now);
                            return Some(Transmission {
                                endpoint: Some(c.endpoint),
                                frame: f.encode().ok()?,
                                sequence: f.sequence,
                            });
                        }
                    }
                }
            }
        }
        None
    }
    pub fn send_failed(&mut self, sequence: u32) {
        for p in self.peers.iter_mut().flatten() {
            if let Some(t) = p.pending.filter(|t| t.frame.sequence == sequence) {
                p.pending = None;
                p.completed = Some((t.ticket, Err(ClientError::Transport)));
            }
        }
    }
    pub fn feed(&mut self, raw: &[u8], from: Endpoint, now: u64) {
        if self.closed || from.validate().is_err() {
            return;
        }
        self.maintain(now);
        if now < self.last_now {
            return;
        }
        let f = match Frame::decode(raw) {
            Ok(f) => f,
            Err(_) => return,
        };
        if f.dst_uid != 0 || f.src_uid == 0 {
            return;
        }
        if self.scan.as_ref().is_some_and(|s| {
            !s.probing
                && s.sent_at.is_some()
                && f.sequence == s.frame.sequence
                && f.session == s.frame.session
                && f.command == Command::Discover as u16
                && f.kind == Kind::Response
        }) {
            if let Ok(d) = Discovery::decode(f.payload.as_slice()) {
                let i = match self.ensure(f.src_uid) {
                    Ok(i) => i,
                    Err(_) => {
                        self.scan_overflow = true;
                        return;
                    }
                };
                let p = self.peers[i].as_mut().unwrap();
                let changed = p.endpoint.is_some_and(|old| old != from)
                    || p.identity.is_some_and(|old| {
                        old.boot != d.boot
                            || old.device_type != d.device_type
                            || old.firmware != d.firmware
                    })
                    || p.status.is_some_and(|old| old.ack.guard.boot != d.boot)
                    || p.candidates.iter().flatten().any(|c| c.endpoint != from);
                if changed && p.state != ConnectionState::Probing {
                    Self::invalidate(p)
                }
                p.seen = true;
                Self::candidate(p, from, Some(d));
            }
            return;
        }
        if self.scan.as_ref().is_some_and(|s| s.probing)
            && f.session == self.probe_session
            && f.kind == Kind::Response
            && f.command == Command::Info as u16
        {
            if let Ok(i) = self.index(f.src_uid) {
                let p = self.peers[i].as_mut().unwrap();
                for c in p.candidates.iter_mut().flatten() {
                    if !c.done
                        && c.sent_at.is_some()
                        && c.endpoint == from
                        && c.probe.is_some_and(|probe| probe.sequence == f.sequence)
                    {
                        if let Ok(info) = Info::decode(f.payload.as_slice()) {
                            if info.uid == f.src_uid {
                                c.identity = Some(info);
                                c.done = true;
                            }
                        }
                    }
                }
            }
            return;
        }
        let i = match self.index(f.src_uid) {
            Ok(i) => i,
            Err(_) => return,
        };
        let p = self.peers[i].as_mut().unwrap();
        let t = match p.pending {
            Some(t) => t,
            None => return,
        };
        if t.sent_at.is_none()
            || p.endpoint != Some(from)
            || f.session != self.session
            || f.sequence != t.frame.sequence
            || f.command != t.frame.command
        {
            return;
        }
        let expected_response = matches!(f.command, 1 | 4 | 5 | 0x22 | 0x30 | 0x50);
        let result = if f.kind == Kind::Error {
            let mut r = Reader::new(f.payload.as_slice());
            match (r.u16(), r.u16(), Ack::decode(r.rest())) {
                (Ok(code), Ok(detail), Ok(snapshot)) => Err(ClientError::Protocol {
                    code,
                    detail,
                    snapshot,
                }),
                _ => return,
            }
        } else if (expected_response && f.kind == Kind::Response)
            || (!expected_response && f.kind == Kind::Ack)
        {
            let valid = if f.kind == Kind::Ack {
                Ack::decode(f.payload.as_slice()).is_ok()
            } else {
                match f.command {
                    1 | 4 => Info::decode(f.payload.as_slice()).is_ok_and(|x| x.uid == f.src_uid),
                    0x30 => Status::decode(f.payload.as_slice()).is_ok(),
                    0x50 => ConfigurationSnapshot::decode(f.payload.as_slice()).is_ok(),
                    _ => true,
                }
            };
            if !valid {
                return;
            }
            if f.command == Command::Status as u16 {
                p.status = Status::decode(f.payload.as_slice()).ok();
            }
            if f.command == Command::SessionOpen as u16 {
                let a = Ack::decode(f.payload.as_slice()).unwrap();
                let mut r = Reader::new(t.frame.payload.as_slice());
                let boot = r.u64().ok();
                let epoch = r.u64().ok().and_then(|v| v.checked_add(1));
                if Some(a.guard.boot) != boot
                    || Some(a.guard.epoch) != epoch
                    || a.guard.generation != 1
                    || a.state != State::Safe
                    || a.source != Source::None
                    || a.fault != 0
                {
                    Err(ClientError::Malformed)
                } else {
                    p.needs_rearm = false;
                    Ok(f)
                }
            } else {
                Ok(f)
            }
        } else {
            return;
        };
        p.pending = None;
        p.completed = Some((t.ticket, result));
    }
    /// Explicit renewal after the caller has stopped publishing and queried SAFE on every peer.
    /// Never automatically STOP, RESET, take ownership or resume motion on a connection change.
    pub fn renew_session(&mut self, new_session: u64) -> Result<()> {
        if new_session == 0
            || new_session == self.session
            || self.scan.is_some()
            || self.emergency.is_some()
        {
            return Err(ErrorCode::State);
        }
        for p in self.peers.iter().flatten() {
            if p.pending.is_some()
                || p.targets.is_some()
                || (!p.needs_rearm
                    && p.status.is_none_or(|s| {
                        s.ack.state != State::Safe
                            || s.ack.source != Source::None
                            || s.ack.fault != 0
                            || !s.safe_confirmed
                    }))
            {
                return Err(ErrorCode::Safety);
            }
        }
        self.session = new_session;
        self.probe_session = if new_session ^ 0x8000000000000000 == 0 {
            1
        } else {
            new_session ^ 0x8000000000000000
        };
        self.sequence = 0;
        for p in self.peers.iter_mut().flatten() {
            p.version = p.version.saturating_add(1);
            p.needs_rearm = true;
            p.completed = None;
        }
        Ok(())
    }
    pub fn close(&mut self) {
        self.closed = true;
        self.scan = None;
        self.emergency = None;
        for p in self.peers.iter_mut().flatten() {
            p.targets = None;
            if let Some(t) = p.pending.take() {
                p.completed = Some((t.ticket, Err(ClientError::Closed)));
            }
        }
    }
}
