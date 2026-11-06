//! `ss07-mobocom-node`: identity | run | info | discover. See docs/mobocon-network-node.md.
use mobocom_protocol::{
    udp::UdpTransport,
    wire::{Command, Frame, Kind, Payload, MAX_FRAME, MULTICAST, PORT},
};
use ss07_mobocom_node::*;
use std::{
    collections::BTreeMap,
    fs,
    net::{Ipv4Addr, SocketAddrV4, UdpSocket},
    path::{Path, PathBuf},
    process::exit,
    thread,
    time::{Duration, Instant},
};

const STATE: &str = "/var/lib/ss07/mobocom-node.json";
const EXPORT: &str = "/run/ss07-mobocom-node/identity.json";
const STATUS: &str = "/run/ss07-mobocom-node/status.json";
const CONFIG: &str = "/etc/ss07/mobocom-node.json";
const MAX_DATAGRAMS_PER_ROUND: usize = 64;
const TICK_US: u64 = 10_000;
const NETWORK_CHECK: Duration = Duration::from_secs(5);
const STATS_LOG: Duration = Duration::from_secs(60);

fn usage() -> ! {
    eprintln!(
        "usage:\n  ss07-mobocom-node identity [--state {STATE}] [--export {EXPORT}]\n  \
         ss07-mobocom-node run [--identity {EXPORT}] [--config {CONFIG}] [--status {STATUS}]\n  \
         ss07-mobocom-node info [--identity {EXPORT}] [--status {STATUS}] [--json]\n  \
         ss07-mobocom-node discover [--interface-ip IPV4] [--timeout-ms 500]\n  \
         ss07-mobocom-node version"
    );
    exit(2)
}

fn options(args: &[String], allowed: &[&str], flags: &[&str]) -> BTreeMap<String, String> {
    let mut out = BTreeMap::new();
    let mut i = 0;
    while i < args.len() {
        let key = args[i].trim_start_matches("--");
        if flags.contains(&key) {
            out.insert(key.to_string(), String::new());
            i += 1;
        } else if allowed.contains(&key) && i + 1 < args.len() && args[i].starts_with("--") {
            out.insert(key.to_string(), args[i + 1].clone());
            i += 2;
        } else {
            usage()
        }
    }
    out
}

fn path(o: &BTreeMap<String, String>, key: &str, default: &str) -> PathBuf {
    PathBuf::from(o.get(key).map(String::as_str).unwrap_or(default))
}

fn fail(message: impl std::fmt::Display) -> ! {
    eprintln!("ss07-mobocom-node: {message}");
    exit(1)
}

fn firmware_value() -> u32 {
    firmware(VERSION).unwrap_or_else(|| fail("package version cannot be encoded as firmware"))
}

fn read_identity(p: &Path) -> Identity {
    let text = fs::read_to_string(p).unwrap_or_else(|e| fail(format!("read {}: {e}", p.display())));
    Identity::parse(&text).unwrap_or_else(|| fail(format!("invalid identity {}", p.display())))
}

/// Root pre-start step: the only writer of the persistent UID file.
fn identity(args: &[String]) {
    let o = options(args, &["state", "export", "machine-id"], &[]);
    let state = path(&o, "state", STATE);
    let (id, created) = load_or_create_identity(&state, &path(&o, "machine-id", "/etc/machine-id"))
        .unwrap_or_else(|e| fail(format!("identity {}: {e}", state.display())));
    if created {
        eprintln!(
            "ss07-mobocom-node: WARNING created new UID {} (0x{:016x}, source {}); Mobocon treats a changed UID as a new device",
            id.uid, id.uid, id.source
        );
    }
    if let Some(export) = o.get("export") {
        write_atomic(Path::new(export), id.to_json().as_bytes(), 0o644)
            .unwrap_or_else(|e| fail(format!("export {export}: {e}")));
    }
}

#[derive(Default)]
struct Counters {
    received: u64,
    replied: u64,
    ignored: u64,
    send_failed: u64,
    receive_failed: u64,
    saturated_rounds: u64,
    rebinds: u64,
}

fn resolve(config: &Path) -> Result<(String, Ipv4Addr), String> {
    let text = match fs::read_to_string(config) {
        Ok(t) => Some(t),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => None,
        Err(e) => return Err(format!("read {}: {e}", config.display())),
    };
    let name = match configured_interface(text.as_deref())? {
        Some(n) => n,
        None => fs::read_to_string("/proc/net/route")
            .ok()
            .and_then(|t| default_interface(&t))
            .ok_or("no IPv4 default route")?,
    };
    let ip = interface_ipv4(&name).ok_or(format!("{name} has no IPv4 address"))?;
    Ok((name, ip))
}

fn run(args: &[String]) {
    let o = options(args, &["identity", "config", "status"], &[]);
    let id = read_identity(&path(&o, "identity", EXPORT));
    let (config, status_path) = (path(&o, "config", CONFIG), path(&o, "status", STATUS));
    let boot = random_u64().unwrap_or_else(|e| fail(format!("boot id: {e}")));
    let fw = firmware_value();
    let mut core = core(id.uid, boot, fw).unwrap_or_else(|e| fail(format!("profile rejected by SDK: {e}")));
    eprintln!(
        "ss07-mobocom-node: start version {VERSION} type 0x{DEVICE_TYPE:04x} uid {} (0x{:016x}) boot {boot} (0x{boot:016x}) firmware 0x{fw:08x}",
        id.uid, id.uid
    );
    let start = Instant::now();
    let now = || start.elapsed().as_micros() as u64;
    let mut transport: Option<UdpTransport> = None;
    let mut bound: Option<(String, Ipv4Addr)> = None;
    let mut last_problem = String::new();
    let mut counters = Counters::default();
    let mut logged = (0u64, 0u64, 0u64);
    let (mut next_check, mut next_stats) = (Instant::now(), Instant::now() + STATS_LOG);
    let mut last_tick = 0u64;
    let mut buffer = [0u8; MAX_FRAME + 1];
    loop {
        if Instant::now() >= next_check {
            next_check = Instant::now() + NETWORK_CHECK;
            match resolve(&config) {
                Ok(found) if transport.is_none() || bound.as_ref() != Some(&found) => {
                    transport = None; // release 47821 before rebinding
                    match UdpTransport::device(found.1) {
                        Ok(t) => {
                            counters.rebinds += 1;
                            eprintln!(
                                "ss07-mobocom-node: listening 0.0.0.0:{PORT}, joined {} on {} ({})",
                                Ipv4Addr::from(MULTICAST),
                                found.0,
                                found.1
                            );
                            transport = Some(t);
                            bound = Some(found);
                            last_problem.clear();
                        }
                        Err(e) => {
                            let p = format!("bind/join on {} ({}): {e}", found.0, found.1);
                            if p != last_problem {
                                eprintln!("ss07-mobocom-node: {p}; retrying every 5 s");
                                last_problem = p;
                            }
                        }
                    }
                }
                Ok(_) => {}
                Err(p) => {
                    if p != last_problem {
                        eprintln!("ss07-mobocom-node: control interface unavailable: {p}; retrying every 5 s");
                        last_problem = p;
                    }
                    transport = None;
                    bound = None;
                }
            }
            write_status(&status_path, &id, boot, fw, bound.as_ref(), &core, &counters);
        }
        if let Some(t) = &transport {
            let mut handled = 0;
            while handled < MAX_DATAGRAMS_PER_ROUND {
                match t.receive(&mut buffer) {
                    Ok(Some((n, from))) => {
                        handled += 1;
                        counters.received += 1;
                        let at = now();
                        match handle(&mut core, &buffer[..n], at) {
                            Some(reply) => match t.send(reply.as_slice(), Some(from)) {
                                Ok(()) => counters.replied += 1,
                                Err(_) => counters.send_failed += 1, // no retry, never blocks
                            },
                            None => counters.ignored += 1,
                        }
                    }
                    Ok(None) => break,
                    Err(_) => {
                        counters.receive_failed += 1;
                        break;
                    }
                }
            }
            if handled == MAX_DATAGRAMS_PER_ROUND {
                counters.saturated_rounds += 1;
            }
        }
        let at = now();
        if at - last_tick >= TICK_US {
            core.tick(at);
            last_tick = at;
        }
        if Instant::now() >= next_stats {
            next_stats = Instant::now() + STATS_LOG;
            let current = (counters.received, counters.send_failed, counters.receive_failed);
            if current != logged {
                logged = current;
                eprintln!(
                    "ss07-mobocom-node: received={} replied={} ignored={} send_failed={} receive_failed={} saturated_rounds={} state={:?} fault={:?}",
                    counters.received, counters.replied, counters.ignored, counters.send_failed,
                    counters.receive_failed, counters.saturated_rounds, core.state(), core.fault()
                );
            }
        }
        thread::sleep(Duration::from_millis(1));
    }
}

fn write_status(
    p: &Path,
    id: &Identity,
    boot: u64,
    fw: u32,
    bound: Option<&(String, Ipv4Addr)>,
    core: &Core,
    c: &Counters,
) {
    let (iface, ip) = bound
        .map(|(n, a)| (format!("\"{n}\""), format!("\"{a}\"")))
        .unwrap_or(("null".into(), "null".into()));
    let text = format!(
        "{{\"schema_version\":1,\"uid\":\"{uid}\",\"uid_hex\":\"0x{uid:016x}\",\"boot\":\"{boot}\",\"device_type\":\"0x{DEVICE_TYPE:04x}\",\
\"firmware\":\"0x{fw:08x}\",\"version\":\"{VERSION}\",\"interface\":{iface},\"ipv4\":{ip},\"port\":{PORT},\
\"multicast\":\"{group}\",\"state\":\"{state:?}\",\"fault\":\"{fault:?}\",\"received\":{},\"replied\":{},\"ignored\":{},\
\"send_failed\":{},\"receive_failed\":{},\"rebinds\":{}}}\n",
        c.received, c.replied, c.ignored, c.send_failed, c.receive_failed, c.rebinds,
        uid = id.uid, group = Ipv4Addr::from(MULTICAST), state = core.state(), fault = core.fault()
    );
    let _ = write_atomic(p, text.as_bytes(), 0o644);
}

fn info(args: &[String]) {
    let o = options(args, &["identity", "status"], &["json"]);
    let status = fs::read_to_string(path(&o, "status", STATUS)).ok();
    if o.contains_key("json") {
        match status {
            Some(s) => print!("{s}"),
            None => fail("node not running (no status file)"),
        }
        return;
    }
    let id = read_identity(&path(&o, "identity", EXPORT));
    println!("UID:        {} (0x{:016x}, source {})", id.uid, id.uid, id.source);
    println!("Type:       VIDEO_SONAR 0x{DEVICE_TYPE:04x}, profile {PROFILE_VERSION}");
    println!("Firmware:   0x{:08x} ({VERSION})", firmware_value());
    println!("Control:    UDP {PORT}, multicast {}", Ipv4Addr::from(MULTICAST));
    match status.as_deref() {
        Some(s) => {
            let f = |k| json_field(s, k).unwrap_or("?").to_string();
            println!("Interface:  {} {}", f("interface"), f("ipv4"));
            println!("Boot:       {}", f("boot"));
            println!("State:      {} fault {}", f("state"), f("fault"));
            println!("Datagrams:  received {} replied {} ignored {}", f("received"), f("replied"), f("ignored"));
        }
        None => println!("Interface:  (service not running)"),
    }
}

/// Read-only multicast DISCOVER; prints every reply with its round-trip time.
fn discover(args: &[String]) {
    let o = options(args, &["interface-ip", "timeout-ms"], &[]);
    let interface: Ipv4Addr = match o.get("interface-ip") {
        Some(v) => v.parse().unwrap_or_else(|_| usage()),
        None => resolve(Path::new(CONFIG)).unwrap_or_else(|e| fail(e)).1,
    };
    let timeout = Duration::from_millis(o.get("timeout-ms").map_or(500, |v| v.parse().unwrap_or_else(|_| usage())));
    let session = random_u64().unwrap_or_else(|e| fail(e));
    let frame = Frame { kind: Kind::Request, src_uid: 0, dst_uid: 0, sequence: 1, command: Command::Discover as u16, session, payload: Payload::default() }
        .encode()
        .unwrap_or_else(|e| fail(format!("{e:?}")));
    let socket = UdpSocket::bind(SocketAddrV4::new(interface, 0)).unwrap_or_else(|e| fail(e));
    socket.set_multicast_ttl_v4(1).unwrap_or_else(|e| fail(e));
    socket.set_read_timeout(Some(Duration::from_millis(20))).unwrap_or_else(|e| fail(e));
    let sent = Instant::now();
    socket
        .send_to(frame.as_slice(), SocketAddrV4::new(Ipv4Addr::from(MULTICAST), PORT))
        .unwrap_or_else(|e| fail(e));
    let mut buffer = [0u8; MAX_FRAME + 1];
    let mut found = 0;
    while sent.elapsed() < timeout {
        let Ok((n, from)) = socket.recv_from(&mut buffer) else { continue };
        let Ok(reply) = Frame::decode(&buffer[..n]) else { continue };
        let p = reply.payload.as_slice();
        if reply.kind != Kind::Response || reply.session != session || reply.command != Command::Discover as u16 || p.len() != 26 {
            continue;
        }
        found += 1;
        let u16le = |i: usize| u16::from_le_bytes([p[i], p[i + 1]]);
        let u32le = |i: usize| u32::from_le_bytes(p[i..i + 4].try_into().unwrap());
        let u64le = |i: usize| u64::from_le_bytes(p[i..i + 8].try_into().unwrap());
        println!(
            "from={from} bytes={n} payload={} rtt_ms={:.2} uid={} uid_hex=0x{:016x} type=0x{:04x} firmware=0x{:08x} boot={} epoch={} generation={}",
            p.len(), sent.elapsed().as_secs_f64() * 1000.0, reply.src_uid, reply.src_uid, u16le(0), u32le(2), u64le(6), u64le(14), u32le(22)
        );
    }
    if found == 0 {
        fail(format!("no DISCOVER reply within {} ms on {interface}", timeout.as_millis()));
    }
}

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let rest = args.get(1..).unwrap_or(&[]);
    match args.first().map(String::as_str) {
        Some("identity") => identity(rest),
        Some("run") => run(rest),
        Some("info") => info(rest),
        Some("discover") => discover(rest),
        Some("version") => println!("{VERSION} firmware=0x{:08x} type=0x{DEVICE_TYPE:04x}", firmware_value()),
        _ => usage(),
    }
}
