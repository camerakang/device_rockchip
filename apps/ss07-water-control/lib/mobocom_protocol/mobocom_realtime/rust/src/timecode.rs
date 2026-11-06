//! Independent 0x12 host timecode. Never used by DeviceCore.
use crate::wire::{crc16, Bytes, ErrorCode, Reader, Result};
pub const VERSION: u8 = 0x12;
pub const LOSS_US: u64 = 30_000;
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Packet {
    pub epoch: u64,
    pub sequence: u32,
    pub time_us: u64,
}
impl Packet {
    pub fn encode(self) -> Result<Bytes<26>> {
        if self.epoch == 0 {
            return Err(ErrorCode::Malformed);
        }
        let mut b = Bytes::default();
        b.raw(&[0xd5, 0x7c, VERSION, 0])?
            .u64(self.epoch)?
            .u32(self.sequence)?
            .u64(self.time_us)?;
        let crc = crc16(&b.as_slice()[2..]);
        b.u16(crc)?;
        Ok(b)
    }
    pub fn decode(b: &[u8]) -> Result<Self> {
        if b.len() != 26 {
            return Err(ErrorCode::Malformed);
        }
        let mut r = Reader::new(b);
        if r.raw(4)? != [0xd5, 0x7c, VERSION, 0] {
            return Err(ErrorCode::Malformed);
        }
        let p = Self {
            epoch: r.u64()?,
            sequence: r.u32()?,
            time_us: r.u64()?,
        };
        if p.epoch == 0 || r.u16()? != crc16(&b[2..24]) {
            return Err(ErrorCode::Malformed);
        }
        Ok(p)
    }
}
#[derive(Default)]
pub struct HostTimeline {
    latest: Option<Packet>,
    origin: u64,
    observed: u64,
    playing: bool,
    fallback: bool,
    play_origin: u64,
    play_time: u64,
    play_epoch: u64,
    last_local: u64,
}
impl HostTimeline {
    pub fn fallback(&self) -> bool {
        self.fallback
    }
    pub fn observe(&mut self, p: Packet, origin_us: u64, observed_us: u64) -> bool {
        if p.epoch == 0
            || origin_us > observed_us
            || observed_us - origin_us >= LOSS_US
            || self.latest.is_some() && observed_us < self.observed
            || self.playing && observed_us < self.last_local
        {
            return false;
        }
        if let Some(last) = self.latest {
            if p.epoch == last.epoch {
                let k = p.sequence.wrapping_sub(last.sequence) as u64;
                let Some(delta) = p.time_us.checked_sub(last.time_us) else {
                    return false;
                };
                if k == 0 || k >= 0x80000000 || !(9950 * k..=10050 * k).contains(&delta) {
                    return false;
                }
            }
        }
        if self.playing && (observed_us - self.observed >= LOSS_US || p.epoch != self.play_epoch) {
            self.fallback = true;
        }
        self.latest = Some(p);
        self.origin = origin_us;
        self.observed = observed_us;
        true
    }
    pub fn start(&mut self, local_us: u64) -> Result<u64> {
        let latest = self.latest.ok_or(ErrorCode::State)?;
        if self.playing || local_us < self.observed || local_us - self.observed >= LOSS_US {
            return Err(ErrorCode::State);
        }
        let time = latest
            .time_us
            .checked_add(local_us - self.origin)
            .ok_or(ErrorCode::Range)?;
        self.playing = true;
        self.fallback = false;
        self.play_origin = local_us;
        self.play_time = time;
        self.play_epoch = latest.epoch;
        self.last_local = local_us;
        Ok(time)
    }
    pub fn now(&mut self, local_us: u64) -> Result<u64> {
        if !self.playing || local_us < self.last_local {
            return Err(ErrorCode::State);
        }
        let time = self
            .play_time
            .checked_add(local_us - self.play_origin)
            .ok_or(ErrorCode::Range)?;
        self.last_local = local_us;
        if local_us.saturating_sub(self.observed) >= LOSS_US {
            self.fallback = true;
        }
        Ok(time)
    }
    pub fn stop(&mut self) {
        self.playing = false;
    }
}
