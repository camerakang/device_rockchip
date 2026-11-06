//! Software-only line-oriented harness for tools/test_rust_sdk.py, not a hardware adapter.
#[path = "../tests/common/mod.rs"]
mod common;
use common::*;
use mobocom_protocol::{configuration::*, timecode, *};
use std::io::{self, BufRead};
fn main() {
    let args: Vec<String> = std::env::args().collect();
    let kind = args.get(1).and_then(|s| s.parse().ok()).unwrap_or(1);
    let uid = args.get(2).and_then(|s| s.parse().ok()).unwrap_or(1);
    let boot = args.get(3).and_then(|s| s.parse().ok()).unwrap_or(10);
    let mode = args.get(4).and_then(|s| s.parse().ok()).unwrap_or(0);
    let resources = resources(kind);
    let actions = actions(kind);
    let mut p = profile(kind, &resources, &actions);
    p.auto_reset_mask = args.get(5).and_then(|s| s.parse().ok()).unwrap_or(0);
    p.device_type = args
        .get(6)
        .and_then(|s| s.parse().ok())
        .unwrap_or(p.device_type);
    let mut node = DeviceCore::new(p, uid, boot, VirtualDriver::configuration(mode)).unwrap();
    if mode == 1 {
        node.fault_input(0, Fault::Driver);
    }
    for line in io::stdin().lock().lines() {
        let line = line.unwrap();
        let a: Vec<&str> = line.split_whitespace().collect();
        if a.is_empty() {
            continue;
        }
        let out = match a[0] {
            "codec" => Frame::decode(&unhex(a[1]))
                .and_then(|f| f.encode())
                .map(|b| hex(b.as_slice()))
                .unwrap_or_else(|_| "invalid".into()),
            "timecode" => timecode::Packet::decode(&unhex(a[1]))
                .and_then(|f| f.encode())
                .map(|b| hex(b.as_slice()))
                .unwrap_or_else(|_| "invalid".into()),
            "focus" => {
                if validate_focus_command(&unhex(a[1])).is_ok() {
                    a[1].to_string()
                } else {
                    "invalid".into()
                }
            }
            "snapshot" => ConfigurationSnapshot::decode(&unhex(a[1]))
                .map(|s| hex(s.encode().as_slice()))
                .unwrap_or_else(|_| "invalid".into()),
            "request" => node
                .transact(&unhex(a[2]), a[1].parse().unwrap())
                .map(|b| hex(b.as_slice()))
                .unwrap_or_default(),
            "tick" => {
                node.tick(a[1].parse().unwrap());
                String::new()
            }
            "recover" => node
                .recover_timeout_fault(a[1].parse().unwrap())
                .to_string(),
            "fault" => {
                node.fault_input(
                    a[1].parse().unwrap(),
                    Fault::decode(a[2].parse().unwrap()).unwrap(),
                );
                String::new()
            }
            "driver" => {
                let v: u64 = a[2].parse().unwrap();
                let d = node.driver_mut();
                match a[1] {
                    "safe_delay" => d.safe_delay = v,
                    "safe_fail" => d.safe_fail = v != 0,
                    "apply_fail" => d.apply_fail = v != 0,
                    "action_fail" => d.action_fail = v != 0,
                    "action_delay" => d.action_delay = v,
                    "reset_ok" => d.reset_ok = v != 0,
                    "fail_validation" => d.fail_validation = v != 0,
                    _ => panic!("unknown hook"),
                };
                String::new()
            }
            _ => panic!("unknown operation"),
        };
        println!("{out}");
    }
}
