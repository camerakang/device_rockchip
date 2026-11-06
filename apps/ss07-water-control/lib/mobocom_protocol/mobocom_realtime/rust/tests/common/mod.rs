#![allow(dead_code)]
use mobocom_protocol::{configuration::*, wire::*, *};
pub fn resources(kind: u8) -> Vec<Resource> {
    match kind {
        2 => (1..=2)
            .map(|i| Resource {
                endpoint: 1,
                resource: i,
                unit: 0x8002,
                flags: 3,
                minimum: -180000,
                maximum: 180000,
                group: 1,
                vmax: 90000,
                amax: 180000,
            })
            .collect(),
        3 => (1..=3)
            .flat_map(|ep| {
                (1..=3).map(move |r| Resource {
                    endpoint: ep,
                    resource: r,
                    unit: 0x8003,
                    flags: 1,
                    minimum: 0,
                    maximum: 10000,
                    ..Resource::default()
                })
            })
            .collect(),
        _ => (1..=3)
            .map(|i| Resource {
                endpoint: 1,
                resource: i,
                unit: 0x8001,
                flags: 1,
                minimum: 0,
                maximum: 10000,
                ..Resource::default()
            })
            .collect(),
    }
}
pub fn profile<'a>(kind: u8, resources: &'a [Resource], actions: &'a [Action]) -> Profile<'a> {
    Profile {
        device_type: match kind {
            2 => DeviceType::VirtualGimbal,
            3 => DeviceType::VirtualFans,
            _ => DeviceType::VirtualLens,
        } as u16,
        resources,
        actions,
        version: 1,
        firmware: 1,
        source_mask: 6,
        max_target_hz: 100,
        update_gap_us: 10000,
        target_min_us: 20000,
        target_max_us: 1000000,
        lease_min_us: 50000,
        lease_max_us: 3000000,
        safe_timeout_us: 100000,
        switch_timeout_us: 100000,
        auto_reset_mask: 0,
    }
}
pub fn actions(kind: u8) -> [Action; 1] {
    [Action {
        opcode: 1,
        resource_mask: match kind {
            2 => 3,
            3 => 0,
            _ => 7,
        },
        parameter_bytes: 0,
        timeout_us: 500000,
    }]
}
pub struct VirtualDriver {
    pub safe_delay: u64,
    pub safe_started: u64,
    pub safe_fail: bool,
    pub apply_fail: bool,
    pub action_fail: bool,
    pub action_delay: u64,
    pub action_started: u64,
    pub reset_ok: bool,
    pub fail_validation: bool,
    pub apply_calls: u32,
    pub flush_calls: u32,
    pub safe_calls: u32,
    pub writes: u32,
    pub configuration: Option<ConfigurationSnapshot>,
}
impl Default for VirtualDriver {
    fn default() -> Self {
        Self {
            safe_delay: 0,
            safe_started: 0,
            safe_fail: false,
            apply_fail: false,
            action_fail: false,
            action_delay: 20000,
            action_started: 0,
            reset_ok: true,
            fail_validation: false,
            apply_calls: 0,
            flush_calls: 0,
            safe_calls: 0,
            writes: 0,
            configuration: None,
        }
    }
}
impl VirtualDriver {
    pub fn configuration(mode: u16) -> Self {
        let values = match mode {
            1 => Bytes::from_slice(&[9, 2, 1, 2]).unwrap(),
            2 | 3 => {
                let ranges = [1, 2].map(|resource| MotionLimitRange {
                    limit: MotionLimit {
                        endpoint: 1,
                        resource,
                        vmax: 50000,
                        amax: 100000,
                    },
                    min_vmax: 1000,
                    max_vmax: 50000,
                    min_amax: 1000,
                    max_amax: 100000,
                });
                Bytes::from_slice(encode_motion_profile(&ranges).unwrap().as_slice()).unwrap()
            }
            4 => Bytes::from_slice(&[0, 7, 0, 0, 0, 0, 0, 0]).unwrap(),
            _ => return Self::default(),
        };
        Self {
            configuration: Some(ConfigurationSnapshot {
                schema: if mode == 3 { 2 } else { mode },
                revision: if mode == 4 { 1 } else { 0 },
                state: ConfigurationState::Idle,
                error: 0,
                request_session: 0,
                request_sequence: 0,
                values,
            }),
            ..Self::default()
        }
    }
}
impl Driver for VirtualDriver {
    fn validate_targets(&mut self, _: &[Target]) -> bool {
        !self.fail_validation
    }
    fn apply_targets(&mut self, _: &[Target], _: u64) -> bool {
        self.apply_calls += 1;
        !self.apply_fail
    }
    fn start_safe(&mut self, _: Fault, now: u64) -> bool {
        self.safe_started = now;
        self.safe_calls += 1;
        !self.safe_fail
    }
    fn safe_confirmed(&self, now: u64) -> bool {
        !self.safe_fail && now.saturating_sub(self.safe_started) >= self.safe_delay
    }
    fn reset_allowed(&self) -> bool {
        self.reset_ok
    }
    fn flush_remote(&mut self) {
        self.flush_calls += 1;
    }
    fn validate_action(&mut self, _: Action, _: &[u8]) -> bool {
        true
    }
    fn start_action(&mut self, _: Action, _: &[u8], _: u64, now: u64) -> bool {
        self.action_started = now;
        !self.action_fail
    }
    fn poll_action(&mut self, _: u64, now: u64) -> Outcome {
        if now - self.action_started >= self.action_delay {
            Outcome::Success
        } else {
            Outcome::Running
        }
    }
    fn configuration_busy(&self) -> bool {
        self.configuration
            .is_some_and(|s| s.state == ConfigurationState::Pending)
    }
    fn read_configuration(&self, schema: u16) -> Result<ConfigurationSnapshot> {
        let mut s = self.configuration.ok_or(ErrorCode::Unsupported)?;
        if schema != s.schema && !(schema == 3 && s.schema == 2) {
            return Err(ErrorCode::Unsupported);
        }
        s.schema = schema;
        Ok(s)
    }
    fn write_configuration(
        &mut self,
        w: ConfigurationWrite<'_>,
        session: u64,
        sequence: u32,
        _: u64,
    ) -> Result<()> {
        let mut s = self.read_configuration(w.schema)?;
        if w.schema == 1 {
            if w.values.len() != 2 {
                return Err(ErrorCode::Malformed);
            }
            if w.values.iter().any(|x| *x > 15) {
                return Err(ErrorCode::Range);
            }
        }
        if w.schema == 4 {
            if w.values.len() != 1 {
                return Err(ErrorCode::Malformed);
            }
            if w.values[0] > 2 {
                return Err(ErrorCode::Range);
            }
        }
        let motion = if w.schema == 2 || w.schema == 3 {
            let (values, n) = decode_motion_limits(w.values).map_err(|_| ErrorCode::Malformed)?;
            if n != 2
                || values[..n]
                    .iter()
                    .any(|v| v.endpoint != 1 || !(1..=2).contains(&v.resource))
            {
                return Err(ErrorCode::Group);
            }
            Some(values)
        } else {
            None
        };
        if w.revision != s.revision {
            return Err(ErrorCode::Conflict);
        }
        s.revision = s.revision.checked_add(1).ok_or(ErrorCode::Space)?;
        if let Some(values) = motion {
            let (mut ranges, n) = motion_profile(&s)?;
            for v in &values[..n] {
                let r = &mut ranges[v.resource as usize - 1];
                if v.vmax < r.min_vmax
                    || v.vmax > r.max_vmax
                    || v.amax < r.min_amax
                    || v.amax > r.max_amax
                {
                    return Err(ErrorCode::Range);
                }
                r.limit = *v;
            }
            s.values = Bytes::from_slice(encode_motion_profile(&ranges[..n])?.as_slice())?;
            s.schema = 2;
        }
        if w.schema == 4 {
            s.values = Bytes::from_slice(&[w.values[0], 7, 0, 0, 0, 0, 0, 0])?;
        }
        s.state = if w.schema == 1 {
            ConfigurationState::Pending
        } else {
            ConfigurationState::Applied
        };
        s.request_session = session;
        s.request_sequence = sequence;
        self.configuration = Some(s);
        self.writes += 1;
        Ok(())
    }
}
pub fn hex(b: &[u8]) -> String {
    b.iter().map(|x| format!("{x:02x}")).collect()
}
pub fn unhex(s: &str) -> Vec<u8> {
    if s == "-" {
        return Vec::new();
    }
    (0..s.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&s[i..i + 2], 16).unwrap())
        .collect()
}
