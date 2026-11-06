//! Golden vectors from the protocol library plus the task-book safety/abnormal matrix.
use mobocom_protocol::{
    wire::{crc16, Command, Frame, Kind, Payload, MAX_FRAME},
    Fault, State, Status,
};
use ss07_mobocom_node::*;
use std::fs;

const VECTORS: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/../lib/mobocom_protocol/docs/ss07/ss07_node_vectors.json");
const UID: u64 = 0x9009_0000_0000_0001;
const BOOT: u64 = 0x1122_3344_5566_7788;
const SESSION: u64 = 0x0102_0304_0506_0708;

fn vector_core() -> Core {
    let text = fs::read_to_string(VECTORS).unwrap();
    let uid: u64 = json_field(&text, "uid").unwrap().parse().unwrap();
    let boot: u64 = json_field(&text, "boot").unwrap().parse().unwrap();
    let fw: u32 = json_field(&text, "firmware").unwrap().parse().unwrap();
    assert_eq!(json_field(&text, "device_type").unwrap(), DEVICE_TYPE.to_string());
    core(uid, boot, fw).unwrap()
}

#[test]
fn golden_vectors_byte_exact() {
    let text = fs::read_to_string(VECTORS).unwrap();
    let mut node = vector_core();
    let mut now = 1_000_000;
    let mut count = 0;
    for chunk in text.split("\"request\"").skip(1) {
        let request = unhex(json_field(&format!("\"request\"{chunk}"), "request").unwrap()).unwrap();
        let expected = json_field(chunk, "response").unwrap();
        let reply = handle(&mut node, &request, now).expect("reply");
        assert_eq!(hex(reply.as_slice()), expected, "vector {count}");
        now += 1000;
        count += 1;
    }
    assert_eq!(count, 5);
}

fn request(dst: u64, sequence: u32, command: Command, payload: &[u8]) -> Vec<u8> {
    Frame { kind: Kind::Request, src_uid: 0, dst_uid: dst, sequence, command: command as u16, session: SESSION, payload: Payload::from_slice(payload).unwrap() }
        .encode()
        .unwrap()
        .as_slice()
        .to_vec()
}

/// (state, fault) decoded with the SDK's own STATUS codec.
fn status(node: &mut Core, sequence: u32, now: u64) -> (u8, u16) {
    let reply = handle(node, &request(UID, sequence, Command::Status, &[0, 0]), now).unwrap();
    let f = Frame::decode(reply.as_slice()).unwrap();
    assert_eq!(f.kind, Kind::Response);
    let s = Status::decode(f.payload.as_slice()).unwrap();
    assert!(s.safe_confirmed);
    (s.ack.state as u8, s.ack.fault)
}

#[test]
fn multicast_estop_then_unicast_reset() {
    let mut node = core(UID, BOOT, 0x000a_0009).unwrap();
    assert_eq!(status(&mut node, 1, 1_000).0, State::Safe as u8);
    // Global STOP: no reply, still SAFE.
    assert!(handle(&mut node, &request(0, 2, Command::Stop, &[]), 2_000).is_none());
    assert_eq!(status(&mut node, 3, 3_000).0, State::Safe as u8);
    // Global ESTOP latches FAULT/7 without a reply.
    assert!(handle(&mut node, &request(0, 4, Command::Estop, &[]), 4_000).is_none());
    node.tick(14_000);
    let (state, fault) = status(&mut node, 5, 15_000);
    assert_eq!((state, fault), (State::Fault as u8, Fault::Estop as u16));
    let generation = node.guard().generation;
    let guard = node.guard().encode();
    let reply = handle(&mut node, &request(UID, 6, Command::ResetFault, guard.as_slice()), 16_000).unwrap();
    assert_eq!(Frame::decode(reply.as_slice()).unwrap().kind, Kind::Ack);
    let (state, fault) = status(&mut node, 7, 17_000);
    assert_eq!((state, fault), (State::Safe as u8, 0));
    assert_eq!(node.guard().generation, generation + 1);
}

#[test]
fn writes_are_refused_without_effect() {
    let mut node = core(UID, BOOT, 0x000a_0009).unwrap();
    let mut payload = node.guard().encode();
    payload.u16(1).unwrap().u32(1).unwrap().i32(0).unwrap();
    let reply = handle(&mut node, &request(UID, 1, Command::WriteTargets, payload.as_slice()), 1_000).unwrap();
    assert_eq!(Frame::decode(reply.as_slice()).unwrap().kind, Kind::Error);
    assert_eq!(status(&mut node, 2, 2_000).0, State::Safe as u8);
}

#[test]
fn abnormal_datagrams_get_no_reply() {
    let mut node = core(UID, BOOT, 0x000a_0009).unwrap();
    let good = request(UID, 1, Command::Info, &[]);
    let mut bad_crc = good.clone();
    *bad_crc.last_mut().unwrap() ^= 0xff;
    let mut trailing = good.clone();
    trailing.push(0);
    let concatenated = [good.clone(), good.clone()].concat();
    let other_dst = request(UID + 1, 1, Command::Info, &[]);
    let oversized = vec![0xd5; MAX_FRAME + 1];
    let multicast_info = request(0, 1, Command::Info, &[]);
    let multicast_status = request(0, 1, Command::Status, &[0, 0]);
    // Declares a 2-byte payload that is absent, with an otherwise valid CRC.
    let mut header_only = good[..37].to_vec();
    header_only[35] = 2;
    let crc = crc16(&header_only[2..]);
    header_only.extend_from_slice(&crc.to_le_bytes());
    for (i, raw) in [bad_crc, trailing, concatenated, other_dst, oversized, multicast_info, multicast_status, header_only, vec![], vec![0; 3]]
        .iter()
        .enumerate()
    {
        assert!(handle(&mut node, raw, 1_000 + i as u64).is_none(), "case {i}");
    }
    assert!(handle(&mut node, &good, 50_000).is_some());
    assert_eq!(status(&mut node, 2, 51_000).0, State::Safe as u8);
}

#[test]
fn identity_firmware_and_routes() {
    assert_eq!(firmware("0.10.9"), Some(0x000a_0009));
    assert_eq!(firmware(VERSION), firmware(VERSION).filter(|v| *v != 0));
    assert_eq!(firmware("0.0.0"), None);
    assert_eq!(firmware("1.2"), None);
    let uid = derive_uid("0123456789abcdef0123456789abcdef").unwrap();
    assert_eq!(derive_uid("0123456789abcdef0123456789abcdef\n"), Some(uid));
    assert_eq!(derive_uid("short"), None);
    // Existing board file written by 0.10.8 keeps its UID.
    let id = Identity::parse("{\"schema_version\":1,\"uid\":\"8455818509601193611\",\"uid_hex\":\"0x75591a2235f3be8b\",\"source\":\"machine-id\"}").unwrap();
    assert_eq!(id.uid, 0x7559_1a22_35f3_be8b);
    assert_eq!(Identity::parse(&id.to_json()), Some(id));
    assert_eq!(Identity::parse("{\"schema_version\":1,\"uid\":\"0\"}"), None);
    let table = "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\n\
                 eth0\t00000000\t0103A8C0\t0003\t0\t0\t100\t00000000\n\
                 eth0\t0000FEA9\t00000000\t0001\t0\t0\t1000\t0000FFFF\n\
                 eth1\t000AA8C0\t00000000\t0001\t0\t0\t100\t00FFFFFF\n";
    assert_eq!(default_interface(table).as_deref(), Some("eth0"));
    assert_eq!(probe_address(table, "eth0"), Some("192.168.3.1".parse().unwrap()));
    assert_eq!(probe_address(table, "eth1"), Some("192.168.10.1".parse().unwrap()));
    assert_eq!(probe_address(table, "wlan0"), None);
    assert_eq!(configured_interface(None), Ok(None));
    assert_eq!(configured_interface(Some("{\"schema_version\":1,\"interface\":\"eth0\"}")), Ok(Some("eth0".into())));
    assert!(configured_interface(Some("{\"schema_version\":1,\"interface\":\"eth0; rm\"}")).is_err());
    assert!(configured_interface(Some("{\"schema_version\":2}")).is_err());
}

#[test]
fn identity_file_is_created_once_and_never_replaced() {
    let dir = std::env::temp_dir().join(format!("ss07-node-test-{}", std::process::id()));
    fs::create_dir_all(&dir).unwrap();
    let (state, machine) = (dir.join("mobocom-node.json"), dir.join("machine-id"));
    let _ = fs::remove_file(&state);
    fs::write(&machine, "0123456789abcdef0123456789abcdef\n").unwrap();
    let (first, created) = load_or_create_identity(&state, &machine).unwrap();
    assert!(created && first.source == "machine-id");
    fs::write(&machine, "fedcba9876543210fedcba9876543210\n").unwrap();
    let (second, created) = load_or_create_identity(&state, &machine).unwrap();
    assert!(!created);
    assert_eq!(first, second);
    use std::os::unix::fs::PermissionsExt;
    assert_eq!(fs::metadata(&state).unwrap().permissions().mode() & 0o777, 0o644);
    fs::write(&state, "garbage").unwrap();
    assert!(load_or_create_identity(&state, &machine).is_err());
    fs::remove_dir_all(&dir).unwrap();
}
