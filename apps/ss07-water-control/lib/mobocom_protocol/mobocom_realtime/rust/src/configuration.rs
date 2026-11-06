//! Configuration codecs only; revision, persistence and real admission checks belong to adapters.
use crate::wire::*;
pub const DUAL_RADIO_SCHEMA: u16 = 1;
pub const MOTION_PROFILE_SCHEMA: u16 = 2;
pub const LIVE_MOTION_PROFILE_SCHEMA: u16 = 3;
pub const FOCUS_CONTROL_SCHEMA: u16 = 4;
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[repr(u8)]
pub enum ConfigurationState {
    Idle = 0,
    Pending = 1,
    Applied = 2,
    Failed = 3,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ConfigurationSnapshot {
    pub schema: u16,
    pub revision: u32,
    pub state: ConfigurationState,
    pub error: u16,
    pub request_session: u64,
    pub request_sequence: u32,
    pub values: Bytes<217>,
}
impl ConfigurationSnapshot {
    pub fn encode(&self) -> Payload {
        let mut b = Payload::default();
        b.u16(self.schema)
            .unwrap()
            .u32(self.revision)
            .unwrap()
            .u8(self.state as u8)
            .unwrap()
            .u16(self.error)
            .unwrap()
            .u64(self.request_session)
            .unwrap()
            .u32(self.request_sequence)
            .unwrap()
            .u16(self.values.len() as u16)
            .unwrap()
            .raw(self.values.as_slice())
            .unwrap();
        b
    }
    pub fn decode(data: &[u8]) -> Result<Self> {
        let mut r = Reader::new(data);
        let schema = r.u16()?;
        let revision = r.u32()?;
        let state = match r.u8()? {
            0 => ConfigurationState::Idle,
            1 => ConfigurationState::Pending,
            2 => ConfigurationState::Applied,
            3 => ConfigurationState::Failed,
            _ => return Err(ErrorCode::Malformed),
        };
        let error = r.u16()?;
        let request_session = r.u64()?;
        let request_sequence = r.u32()?;
        let n = r.u16()? as usize;
        let values = Bytes::from_slice(r.raw(n)?)?;
        r.end()?;
        Ok(Self {
            schema,
            revision,
            state,
            error,
            request_session,
            request_sequence,
            values,
        })
    }
    /// A write timeout is resolved by reading, never by inventing a new write sequence.
    pub fn confirms(
        &self,
        request: &ConfigurationWrite<'_>,
        session: u64,
        sequence: u32,
        expected_values: &[u8],
    ) -> bool {
        self.schema == request.schema
            && request.revision.checked_add(1) == Some(self.revision)
            && self.state == ConfigurationState::Applied
            && self.error == 0
            && self.request_session == session
            && self.request_sequence == sequence
            && self.values.as_slice() == expected_values
    }
}
#[derive(Clone, Copy, Debug)]
pub struct ConfigurationWrite<'a> {
    pub uid: u64,
    pub guard: Guard,
    pub schema: u16,
    pub revision: u32,
    pub values: &'a [u8],
}
impl ConfigurationWrite<'_> {
    pub fn encode(self) -> Result<Payload> {
        if self.values.len() > 206 {
            return Err(ErrorCode::Range);
        }
        let mut b = Payload::default();
        b.u64(self.uid)?
            .raw(self.guard.encode().as_slice())?
            .u16(self.schema)?
            .u32(self.revision)?
            .raw(self.values)?;
        Ok(b)
    }
}
pub fn radio_channels(focus: u8, wheel: u8) -> Result<[u8; 2]> {
    if focus > 15 || wheel > 15 {
        return Err(ErrorCode::Range);
    }
    Ok([focus, wheel])
}
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct MotionLimit {
    pub endpoint: u16,
    pub resource: u16,
    pub vmax: i32,
    pub amax: i32,
}
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct MotionLimitRange {
    pub limit: MotionLimit,
    pub min_vmax: i32,
    pub max_vmax: i32,
    pub min_amax: i32,
    pub max_amax: i32,
}
fn validate_limits(v: &[MotionLimit]) -> Result<()> {
    if v.is_empty() || v.len() > 7 {
        return Err(ErrorCode::Range);
    }
    for (i, x) in v.iter().enumerate() {
        if x.vmax <= 0
            || x.amax <= 0
            || v[..i]
                .iter()
                .any(|o| o.endpoint == x.endpoint && o.resource == x.resource)
        {
            return Err(ErrorCode::Range);
        }
    }
    Ok(())
}
pub fn motion_limits(v: &[MotionLimit]) -> Result<Payload> {
    validate_limits(v)?;
    let mut b = Payload::default();
    b.raw(&[2, 0, v.len() as u8])?;
    for x in v {
        b.u16(x.endpoint)?
            .u16(x.resource)?
            .i32(x.vmax)?
            .i32(x.amax)?;
    }
    Ok(b)
}
pub fn decode_motion_limits(data: &[u8]) -> Result<([MotionLimit; 7], usize)> {
    let mut r = Reader::new(data);
    if r.raw(2)? != [2, 0] {
        return Err(ErrorCode::Malformed);
    }
    let count = r.u8()? as usize;
    if count > 7 {
        return Err(ErrorCode::Range);
    }
    let mut out = [MotionLimit::default(); 7];
    for x in &mut out[..count] {
        *x = MotionLimit {
            endpoint: r.u16()?,
            resource: r.u16()?,
            vmax: r.i32()?,
            amax: r.i32()?,
        };
    }
    r.end()?;
    validate_limits(&out[..count])?;
    Ok((out, count))
}
pub fn encode_motion_profile(values: &[MotionLimitRange]) -> Result<Payload> {
    if values.is_empty() || values.len() > 7 {
        return Err(ErrorCode::Range);
    }
    let mut limits = [MotionLimit::default(); 7];
    let mut b = Payload::default();
    b.raw(&[2, 0, values.len() as u8])?;
    for (i, x) in values.iter().enumerate() {
        limits[i] = x.limit;
        if x.min_vmax <= 0
            || x.min_amax <= 0
            || x.limit.vmax < x.min_vmax
            || x.limit.vmax > x.max_vmax
            || x.limit.amax < x.min_amax
            || x.limit.amax > x.max_amax
        {
            return Err(ErrorCode::Range);
        }
        b.u16(x.limit.endpoint)?
            .u16(x.limit.resource)?
            .i32(x.limit.vmax)?
            .i32(x.limit.amax)?
            .i32(x.min_vmax)?
            .i32(x.max_vmax)?
            .i32(x.min_amax)?
            .i32(x.max_amax)?;
    }
    validate_limits(&limits[..values.len()])?;
    Ok(b)
}
pub fn motion_profile(snapshot: &ConfigurationSnapshot) -> Result<([MotionLimitRange; 7], usize)> {
    if snapshot.schema != 2 && snapshot.schema != 3 {
        return Err(ErrorCode::Unsupported);
    }
    let mut r = Reader::new(snapshot.values.as_slice());
    if r.raw(2)? != [2, 0] {
        return Err(ErrorCode::Malformed);
    }
    let n = r.u8()? as usize;
    if n > 7 {
        return Err(ErrorCode::Range);
    }
    let mut values = [MotionLimitRange::default(); 7];
    for x in &mut values[..n] {
        *x = MotionLimitRange {
            limit: MotionLimit {
                endpoint: r.u16()?,
                resource: r.u16()?,
                vmax: r.i32()?,
                amax: r.i32()?,
            },
            min_vmax: r.i32()?,
            max_vmax: r.i32()?,
            min_amax: r.i32()?,
            max_amax: r.i32()?,
        };
    }
    r.end()?;
    encode_motion_profile(&values[..n])?;
    Ok((values, n))
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[repr(u8)]
pub enum FocusSource {
    Wireless = 0,
    Wired = 1,
    Stopped = 2,
    Fault = 3,
}
pub fn focus_source(source: FocusSource) -> Result<[u8; 1]> {
    if source == FocusSource::Fault {
        return Err(ErrorCode::Range);
    }
    Ok([source as u8])
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct FocusStatus {
    pub source: FocusSource,
    pub output_idle: bool,
    pub radio_ready: bool,
    pub bus_quiet: bool,
    pub wired_commands: bool,
    pub button_remaining_ms: u32,
}
impl FocusStatus {
    pub fn encode(self) -> Result<Bytes<8>> {
        if self.output_idle && self.button_remaining_ms != 0 {
            return Err(ErrorCode::Range);
        }
        let flags = self.output_idle as u8
            | ((self.radio_ready as u8) << 1)
            | ((self.bus_quiet as u8) << 2)
            | ((self.wired_commands as u8) << 3);
        let mut b = Bytes::default();
        b.u8(self.source as u8)?
            .u8(flags)?
            .u16(0)?
            .u32(self.button_remaining_ms)?;
        Ok(b)
    }
    pub fn decode(b: &[u8]) -> Result<Self> {
        let mut r = Reader::new(b);
        let source = match r.u8()? {
            0 => FocusSource::Wireless,
            1 => FocusSource::Wired,
            2 => FocusSource::Stopped,
            3 => FocusSource::Fault,
            _ => return Err(ErrorCode::Malformed),
        };
        let flags = r.u8()?;
        if flags & !15 != 0 || r.u16()? != 0 {
            return Err(ErrorCode::Malformed);
        }
        let button_remaining_ms = r.u32()?;
        r.end()?;
        let s = Self {
            source,
            output_idle: flags & 1 != 0,
            radio_ready: flags & 2 != 0,
            bus_quiet: flags & 4 != 0,
            wired_commands: flags & 8 != 0,
            button_remaining_ms,
        };
        s.encode()?;
        Ok(s)
    }
}
pub fn focus_positions(mask: u8, positions: [u16; 3]) -> Result<Bytes<8>> {
    if !(1..=7).contains(&mask)
        || positions
            .iter()
            .enumerate()
            .any(|(i, v)| mask & (1 << i) == 0 && *v != 0)
    {
        return Err(ErrorCode::Range);
    }
    let mut b = Bytes::default();
    b.u8(0x10)?.u8(mask)?;
    for p in positions {
        b.u16(p)?;
    }
    Ok(b)
}
pub fn focus_button(action: u8) -> Result<[u8; 2]> {
    if action > 1 {
        return Err(ErrorCode::Range);
    }
    Ok([0x11, action])
}
pub fn validate_focus_command(b: &[u8]) -> Result<()> {
    let mut r = Reader::new(b);
    match r.u8()? {
        0x10 => {
            focus_positions(r.u8()?, [r.u16()?, r.u16()?, r.u16()?])?;
        }
        0x11 => {
            focus_button(r.u8()?)?;
        }
        _ => return Err(ErrorCode::Malformed),
    }
    r.end()
}
