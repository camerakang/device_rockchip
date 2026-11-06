//! SS-07 VIDEO_SONAR (0x9009) Control node built on the unmodified Mobocom Rust SDK.
//!
//! The node only answers identity, discovery, status and the protocol safety
//! commands. It owns no output: video, recording, sonar and timecode run in
//! other services and are never touched from here.
use mobocom_protocol::{
    device_types::DeviceType, wire::{Datagram, Target}, Action, DeviceCore, Driver, Fault, Profile, Resource,
};
use std::{
    fs,
    io::{self, Read, Write},
    net::{Ipv4Addr, SocketAddrV4, UdpSocket},
    path::Path,
};

pub const DEVICE_TYPE: u16 = DeviceType::VideoSonar as u16;
pub const PROFILE_VERSION: u16 = 1;
pub const VERSION: &str = env!("CARGO_PKG_VERSION");

/// Placeholder resource required by the common core; it never carries a target.
pub static RESOURCES: [Resource; 1] = [Resource {
    endpoint: 1,
    resource: 1,
    unit: 0x9009,
    flags: 0,
    minimum: 0,
    maximum: 0,
    group: 0,
    vmax: 0,
    amax: 0,
}];
static ACTIONS: [Action; 0] = [];

/// Contract section 4 values; only firmware varies with the release.
pub fn profile(firmware: u32) -> Profile<'static> {
    Profile {
        device_type: DEVICE_TYPE,
        resources: &RESOURCES,
        actions: &ACTIONS,
        version: PROFILE_VERSION,
        firmware,
        source_mask: 0x04,
        max_target_hz: 100,
        update_gap_us: 10_000,
        target_min_us: 20_000,
        target_max_us: 1_000_000,
        lease_min_us: 50_000,
        lease_max_us: 3_000_000,
        safe_timeout_us: 100_000,
        switch_timeout_us: 100_000,
        auto_reset_mask: 0,
    }
}

/// No physical output: targets are refused, safety is always confirmed.
#[derive(Debug, Default)]
pub struct NodeDriver;
impl Driver for NodeDriver {
    fn validate_targets(&mut self, _: &[Target]) -> bool {
        false
    }
    fn apply_targets(&mut self, _: &[Target], _: u64) -> bool {
        false
    }
    fn start_safe(&mut self, _: Fault, _: u64) -> bool {
        true
    }
    fn safe_confirmed(&self, _: u64) -> bool {
        true
    }
    fn reset_allowed(&self) -> bool {
        true
    }
    fn flush_remote(&mut self) {}
}

pub type Core = DeviceCore<'static, NodeDriver>;

pub fn core(uid: u64, boot: u64, firmware: u32) -> Result<Core, String> {
    DeviceCore::new(profile(firmware), uid, boot, NodeDriver).map_err(|e| format!("{e:?}"))
}

/// One datagram in, at most one reply out, bound to this datagram's source.
pub fn handle(core: &mut Core, raw: &[u8], now: u64) -> Option<Datagram> {
    if core.receive(raw, now) {
        core.take_reply(now)
    } else {
        None
    }
}

/// `major<<24 | minor<<16 | patch`; zero or out-of-range parts are rejected.
pub fn firmware(version: &str) -> Option<u32> {
    let parts: Vec<u32> = version
        .split('.')
        .map(|p| p.parse().ok())
        .collect::<Option<_>>()?;
    let [major, minor, patch] = parts[..] else {
        return None;
    };
    if major > 0xff || minor > 0xff || patch > 0xffff {
        return None;
    }
    let value = major << 24 | minor << 16 | patch;
    (value != 0).then_some(value)
}

pub fn valid_uid(uid: u64) -> bool {
    uid != 0 && uid != u64::MAX
}

/// Stable FNV-1a over a domain prefix and the machine id; persisted after first use.
pub fn derive_uid(machine_id: &str) -> Option<u64> {
    let id = machine_id.trim();
    if id.len() < 16 || !id.bytes().all(|b| b.is_ascii_hexdigit()) {
        return None;
    }
    let mut h: u64 = 0xcbf2_9ce4_8422_2325;
    for b in b"ss07-mobocom-node/".iter().chain(id.as_bytes()) {
        h ^= *b as u64;
        h = h.wrapping_mul(0x0100_0000_01b3);
    }
    valid_uid(h).then_some(h)
}

pub fn random_u64() -> io::Result<u64> {
    let mut file = fs::File::open("/dev/urandom")?;
    loop {
        let mut b = [0u8; 8];
        file.read_exact(&mut b)?;
        let v = u64::from_le_bytes(b);
        if valid_uid(v) {
            return Ok(v);
        }
    }
}

/// Value of `"key":"..."` or `"key":number` in a small flat JSON object.
pub fn json_field<'a>(text: &'a str, key: &str) -> Option<&'a str> {
    let pattern = format!("\"{key}\"");
    let rest = text[text.find(&pattern)? + pattern.len()..].trim_start();
    let rest = rest.strip_prefix(':')?.trim_start();
    if let Some(s) = rest.strip_prefix('"') {
        return Some(&s[..s.find('"')?]);
    }
    let end = rest
        .find(|c: char| c == ',' || c == '}' || c.is_whitespace())
        .unwrap_or(rest.len());
    Some(&rest[..end])
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Identity {
    pub uid: u64,
    pub source: String,
}
impl Identity {
    pub fn parse(text: &str) -> Option<Self> {
        if json_field(text, "schema_version")? != "1" {
            return None;
        }
        let uid: u64 = json_field(text, "uid")?.parse().ok()?;
        let source = json_field(text, "source").unwrap_or("unknown");
        if !valid_uid(uid) || !source.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-') {
            return None;
        }
        Some(Self { uid, source: source.into() })
    }
    /// UID is a decimal string: it must never pass through a JavaScript number.
    pub fn to_json(&self) -> String {
        format!(
            "{{\"schema_version\":1,\"uid\":\"{}\",\"uid_hex\":\"0x{:016x}\",\"source\":\"{}\"}}\n",
            self.uid, self.uid, self.source
        )
    }
}

/// Write via a temporary file and rename, so readers never see a partial identity.
pub fn write_atomic(path: &Path, data: &[u8], mode: u32) -> io::Result<()> {
    use std::os::unix::fs::PermissionsExt;
    let tmp = path.with_extension("tmp");
    {
        let mut f = fs::File::create(&tmp)?;
        f.set_permissions(fs::Permissions::from_mode(mode))?;
        f.write_all(data)?;
        f.sync_all()?;
    }
    fs::rename(&tmp, path)?;
    if let Some(parent) = path.parent() {
        fs::File::open(parent)?.sync_all()?;
    }
    Ok(())
}

/// Read the persisted identity, creating it once. Returns (identity, created).
pub fn load_or_create_identity(path: &Path, machine_id: &Path) -> io::Result<(Identity, bool)> {
    match fs::read_to_string(path) {
        Ok(text) => {
            return Identity::parse(&text)
                .map(|i| (i, false))
                .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidData, "invalid identity file; refusing to replace the UID"))
        }
        Err(e) if e.kind() == io::ErrorKind::NotFound => {}
        Err(e) => return Err(e),
    }
    let identity = match fs::read_to_string(machine_id).ok().and_then(|m| derive_uid(&m)) {
        Some(uid) => Identity { uid, source: "machine-id".into() },
        None => Identity { uid: random_u64()?, source: "random".into() },
    };
    write_atomic(path, identity.to_json().as_bytes(), 0o644)?;
    Ok((identity, true))
}

/// Interface carrying the IPv4 default route, from /proc/net/route.
pub fn default_interface(route_table: &str) -> Option<String> {
    route_table.lines().skip(1).find_map(|line| {
        let f: Vec<&str> = line.split_whitespace().collect();
        (f.len() >= 8 && f[1] == "00000000" && f[7] == "00000000").then(|| f[0].to_string())
    })
}

fn route_hex(value: &str) -> Option<Ipv4Addr> {
    Some(Ipv4Addr::from(u32::from_str_radix(value, 16).ok()?.to_le_bytes()))
}

/// A remote address routed through `interface`, used to ask the kernel for its source address.
pub fn probe_address(route_table: &str, interface: &str) -> Option<Ipv4Addr> {
    let mut network = None;
    for line in route_table.lines().skip(1) {
        let f: Vec<&str> = line.split_whitespace().collect();
        if f.len() < 8 || f[0] != interface {
            continue;
        }
        let (dest, gateway, mask) = (route_hex(f[1])?, route_hex(f[2])?, route_hex(f[7])?);
        if dest.is_unspecified() && !gateway.is_unspecified() {
            return Some(gateway);
        }
        if network.is_none() && !dest.is_link_local() && !dest.is_unspecified() && u32::from(mask) != u32::MAX {
            network = Some(Ipv4Addr::from(u32::from(dest) | 1));
        }
    }
    network
}

/// Current IPv4 of `interface` (connect-only UDP probe; nothing is sent).
pub fn interface_ipv4(interface: &str) -> Option<Ipv4Addr> {
    let table = fs::read_to_string("/proc/net/route").ok()?;
    let target = probe_address(&table, interface)?;
    let socket = UdpSocket::bind(SocketAddrV4::new(Ipv4Addr::UNSPECIFIED, 0)).ok()?;
    socket.connect(SocketAddrV4::new(target, 9)).ok()?;
    match socket.local_addr().ok()? {
        std::net::SocketAddr::V4(a) if !a.ip().is_unspecified() => Some(*a.ip()),
        _ => None,
    }
}

/// `/etc/ss07/mobocom-node.json`: `{"schema_version":1,"interface":"eth0"}`; missing means default route.
pub fn configured_interface(text: Option<&str>) -> Result<Option<String>, String> {
    let Some(text) = text else { return Ok(None) };
    if json_field(text, "schema_version") != Some("1") {
        return Err("unsupported mobocom-node.json schema".into());
    }
    match json_field(text, "interface") {
        None | Some("null") => Ok(None),
        Some(name)
            if !name.is_empty()
                && name.len() < 16
                && name.bytes().all(|b| b.is_ascii_alphanumeric() || b"._-".contains(&b)) =>
        {
            Ok(Some(name.to_string()))
        }
        Some(_) => Err("invalid interface name".into()),
    }
}

pub fn hex(b: &[u8]) -> String {
    b.iter().map(|x| format!("{x:02x}")).collect()
}
pub fn unhex(s: &str) -> Option<Vec<u8>> {
    s.len().is_multiple_of(2)
        .then(|| (0..s.len()).step_by(2).map(|i| u8::from_str_radix(&s[i..i + 2], 16).ok()).collect())
        .flatten()
}
