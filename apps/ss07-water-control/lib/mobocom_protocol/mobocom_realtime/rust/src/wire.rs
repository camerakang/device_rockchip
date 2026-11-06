//! Exact one-datagram codec. No allocation, stream buffering or resynchronization.
pub const VERSION: u8 = 0x22;
pub const MAX_PAYLOAD: usize = 240;
pub const MAX_FRAME: usize = 279;
pub const ALL_NODES: u64 = 0;
pub const MULTICAST: [u8; 4] = [239, 255, 33, 21];
pub const PORT: u16 = 47821;
pub const DISCOVERY_WINDOW_US: u64 = 100_000;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[repr(u8)]
pub enum Kind {
    Request = 1,
    Response = 2,
    Ack = 3,
    Error = 4,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[repr(u16)]
pub enum Command {
    Hello = 1,
    Discover = 2,
    Info = 4,
    Caps = 5,
    SessionOpen = 6,
    SetSource = 0x10,
    Heartbeat = 0x11,
    WriteTargets = 0x20,
    ExecuteAction = 0x21,
    ActionStatus = 0x22,
    Status = 0x30,
    Stop = 0x40,
    Estop = 0x41,
    ResetFault = 0x42,
    ReadConfig = 0x50,
    WriteConfig = 0x51,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[repr(u16)]
pub enum ErrorCode {
    Ok = 0,
    Malformed = 1,
    Unsupported = 2,
    State = 3,
    Boot = 4,
    Generation = 5,
    Owner = 6,
    Sequence = 7,
    Conflict = 8,
    Range = 9,
    Group = 10,
    Busy = 11,
    Space = 12,
    Safety = 13,
    NotFound = 14,
    Address = 15,
    Capability = 16,
    Bandwidth = 17,
    Timeout = 18,
    Driver = 19,
    Epoch = 20,
    Session = 21,
}
pub type Result<T> = core::result::Result<T, ErrorCode>;

/// Capacity is checked before every append; partial writes are never sent by codecs.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Bytes<const N: usize> {
    data: [u8; N],
    len: usize,
}
impl<const N: usize> Default for Bytes<N> {
    fn default() -> Self {
        Self {
            data: [0; N],
            len: 0,
        }
    }
}
impl<const N: usize> Bytes<N> {
    pub fn from_slice(data: &[u8]) -> Result<Self> {
        let mut b = Self::default();
        b.raw(data)?;
        Ok(b)
    }
    pub fn as_slice(&self) -> &[u8] {
        &self.data[..self.len]
    }
    pub fn len(&self) -> usize {
        self.len
    }
    pub fn is_empty(&self) -> bool {
        self.len == 0
    }
    pub fn raw(&mut self, b: &[u8]) -> Result<&mut Self> {
        if b.len() > N - self.len {
            return Err(ErrorCode::Space);
        }
        self.data[self.len..self.len + b.len()].copy_from_slice(b);
        self.len += b.len();
        Ok(self)
    }
    pub fn u8(&mut self, v: u8) -> Result<&mut Self> {
        self.raw(&[v])
    }
    pub fn u16(&mut self, v: u16) -> Result<&mut Self> {
        self.raw(&v.to_le_bytes())
    }
    pub fn u32(&mut self, v: u32) -> Result<&mut Self> {
        self.raw(&v.to_le_bytes())
    }
    pub fn i32(&mut self, v: i32) -> Result<&mut Self> {
        self.raw(&v.to_le_bytes())
    }
    pub fn u64(&mut self, v: u64) -> Result<&mut Self> {
        self.raw(&v.to_le_bytes())
    }
}
pub type Payload = Bytes<MAX_PAYLOAD>;
pub type Datagram = Bytes<MAX_FRAME>;
pub struct Reader<'a> {
    data: &'a [u8],
    offset: usize,
}
impl<'a> Reader<'a> {
    pub fn new(data: &'a [u8]) -> Self {
        Self { data, offset: 0 }
    }
    pub fn raw(&mut self, n: usize) -> Result<&'a [u8]> {
        if n > self.data.len() - self.offset {
            return Err(ErrorCode::Malformed);
        }
        let b = &self.data[self.offset..self.offset + n];
        self.offset += n;
        Ok(b)
    }
    pub fn u8(&mut self) -> Result<u8> {
        Ok(self.raw(1)?[0])
    }
    pub fn u16(&mut self) -> Result<u16> {
        let b = self.raw(2)?;
        Ok(u16::from_le_bytes([b[0], b[1]]))
    }
    pub fn u32(&mut self) -> Result<u32> {
        let b = self.raw(4)?;
        Ok(u32::from_le_bytes([b[0], b[1], b[2], b[3]]))
    }
    pub fn i32(&mut self) -> Result<i32> {
        Ok(self.u32()? as i32)
    }
    pub fn u64(&mut self) -> Result<u64> {
        let lo = self.u32()? as u64;
        Ok(lo | ((self.u32()? as u64) << 32))
    }
    pub fn end(&self) -> Result<()> {
        if self.offset == self.data.len() {
            Ok(())
        } else {
            Err(ErrorCode::Malformed)
        }
    }
    pub fn rest(&mut self) -> &'a [u8] {
        let b = &self.data[self.offset..];
        self.offset = self.data.len();
        b
    }
}
pub fn crc16(data: &[u8]) -> u16 {
    let mut c = 0xffffu16;
    for b in data {
        c ^= (*b as u16) << 8;
        for _ in 0..8 {
            c = (c << 1) ^ if c & 0x8000 != 0 { 0x1021 } else { 0 };
        }
    }
    c
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Frame {
    pub kind: Kind,
    pub src_uid: u64,
    pub dst_uid: u64,
    pub sequence: u32,
    pub command: u16,
    pub session: u64,
    pub payload: Payload,
}
impl Frame {
    pub fn request(
        uid: u64,
        sequence: u32,
        command: Command,
        session: u64,
        payload: Payload,
    ) -> Self {
        Self {
            kind: Kind::Request,
            src_uid: 0,
            dst_uid: uid,
            sequence,
            command: command as u16,
            session,
            payload,
        }
    }
    pub fn encode(&self) -> Result<Datagram> {
        if self.sequence == 0 || self.session == 0 {
            return Err(ErrorCode::Malformed);
        }
        let mut b = Datagram::default();
        b.raw(&[0xd5, 0x7d, VERSION, self.kind as u8, 0])?
            .u64(self.src_uid)?
            .u64(self.dst_uid)?
            .u32(self.sequence)?
            .u16(self.command)?
            .u64(self.session)?
            .u16(self.payload.len() as u16)?
            .raw(self.payload.as_slice())?;
        let crc = crc16(&b.as_slice()[2..]);
        b.u16(crc)?;
        Ok(b)
    }
    pub fn decode(raw: &[u8]) -> Result<Self> {
        if raw.len() < 39 || raw.len() > MAX_FRAME {
            return Err(ErrorCode::Malformed);
        }
        let mut r = Reader::new(raw);
        if r.raw(3)? != [0xd5, 0x7d, VERSION] {
            return Err(ErrorCode::Malformed);
        }
        let kind = match r.u8()? {
            1 => Kind::Request,
            2 => Kind::Response,
            3 => Kind::Ack,
            4 => Kind::Error,
            _ => return Err(ErrorCode::Malformed),
        };
        if r.u8()? != 0 {
            return Err(ErrorCode::Malformed);
        }
        let src_uid = r.u64()?;
        let dst_uid = r.u64()?;
        let sequence = r.u32()?;
        let command = r.u16()?;
        let session = r.u64()?;
        let size = r.u16()? as usize;
        if sequence == 0 || session == 0 || size > MAX_PAYLOAD || raw.len() != 39 + size {
            return Err(ErrorCode::Malformed);
        }
        let payload = Payload::from_slice(r.raw(size)?)?;
        if r.u16()? != crc16(&raw[2..raw.len() - 2]) {
            return Err(ErrorCode::Malformed);
        }
        Ok(Self {
            kind,
            src_uid,
            dst_uid,
            sequence,
            command,
            session,
            payload,
        })
    }
}
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Guard {
    pub boot: u64,
    pub generation: u32,
    pub epoch: u64,
}
impl Guard {
    pub fn encode(self) -> Payload {
        let mut b = Payload::default();
        b.u64(self.boot)
            .unwrap()
            .u32(self.generation)
            .unwrap()
            .u64(self.epoch)
            .unwrap();
        b
    }
    pub fn read(r: &mut Reader<'_>) -> Result<Self> {
        Ok(Self {
            boot: r.u64()?,
            generation: r.u32()?,
            epoch: r.u64()?,
        })
    }
}
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Target {
    pub endpoint: u16,
    pub resource: u16,
    pub value: i32,
}
pub fn targets(guard: Guard, values: &[Target]) -> Result<Payload> {
    if values.is_empty() || values.len() > 16 {
        return Err(ErrorCode::Malformed);
    }
    let mut b = guard.encode();
    b.u8(values.len() as u8)?;
    for t in values {
        b.u16(t.endpoint)?.u16(t.resource)?.i32(t.value)?;
    }
    Ok(b)
}
