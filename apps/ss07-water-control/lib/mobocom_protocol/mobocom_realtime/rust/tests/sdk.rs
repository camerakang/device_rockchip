mod common;
use common::*;
use mobocom_protocol::{client::*, configuration::*, scheduler::*, timecode::*, wire::*, *};
const EP: Endpoint = Endpoint {
    address: [127, 0, 0, 1],
    port: 47821,
};
fn request(
    node: &mut DeviceCore<'_, VirtualDriver>,
    sequence: u32,
    command: Command,
    p: Payload,
    now: u64,
) -> Frame {
    Frame::decode(
        node.transact(
            Frame::request(node.uid(), sequence, command, 100, p)
                .encode()
                .unwrap()
                .as_slice(),
            now,
        )
        .unwrap()
        .as_slice(),
    )
    .unwrap()
}
fn activate(node: &mut DeviceCore<'_, VirtualDriver>) {
    let g = node.guard();
    let mut p = Payload::default();
    p.u64(g.boot)
        .unwrap()
        .u64(g.epoch)
        .unwrap()
        .u32(g.generation)
        .unwrap();
    assert_eq!(request(node, 1, Command::SessionOpen, p, 0).kind, Kind::Ack);
    let mut p = node.guard().encode();
    p.u8(2).unwrap().u32(100000).unwrap().u32(300000).unwrap();
    assert_eq!(request(node, 2, Command::SetSource, p, 0).kind, Kind::Ack);
    node.tick(0);
}
fn pump<const N: usize>(
    client: &mut Client<N>,
    node: &mut DeviceCore<'_, VirtualDriver>,
    now: u64,
) {
    for _ in 0..32 {
        let Some(t) = client.next_transmission(now) else {
            break;
        };
        if t.endpoint == Some(EP) || t.endpoint.is_none() {
            if let Some(reply) = node.transact(t.frame.as_slice(), now) {
                client.feed(reply.as_slice(), EP, now);
            }
        }
    }
    node.tick(now);
}
#[test]
fn wire_lengths_crc_and_unknown_commands() {
    assert_eq!(crc16(b"123456789"), 0x29b1);
    let mut f = Frame::request(
        u64::MAX,
        1,
        Command::Info,
        u64::MAX,
        Payload::from_slice(&[4; 240]).unwrap(),
    );
    f.command = 0xffff;
    let raw = f.encode().unwrap();
    assert_eq!(raw.len(), 279);
    assert_eq!(Frame::decode(raw.as_slice()).unwrap(), f);
    for i in 0..raw.len() {
        assert!(Frame::decode(&raw.as_slice()[..i]).is_err());
    }
    for i in 0..raw.len() {
        let mut bad = raw.as_slice().to_vec();
        bad[i] ^= 1;
        assert!(Frame::decode(&bad).is_err());
    }
}
#[test]
fn unregistered_type_and_invalid_profiles() {
    let r = resources(1);
    let a = actions(1);
    let mut p = profile(1, &r, &a);
    p.device_type = 0x900f;
    assert!(p.validate().is_err());
    p.device_type = DeviceType::VirtualLens as u16;
    p.source_mask = 7;
    assert!(p.validate().is_err());
}
#[test]
fn acceptance_tick_ttl_duplicate_and_recovery() {
    let r = resources(1);
    let a = actions(1);
    let mut p = profile(1, &r, &a);
    p.auto_reset_mask = 6;
    let mut n = DeviceCore::new(p, 1, 10, VirtualDriver::default()).unwrap();
    activate(&mut n);
    let target = targets(
        n.guard(),
        &[Target {
            endpoint: 1,
            resource: 1,
            value: 42,
        }],
    )
    .unwrap();
    let ack = request(&mut n, 3, Command::WriteTargets, target, 0);
    assert_eq!(n.driver().apply_calls, 0);
    n.tick(0);
    assert_eq!(n.driver().apply_calls, 1);
    for now in (5000..100000).step_by(5000) {
        n.tick(now);
    }
    assert_eq!(
        request(&mut n, 3, Command::WriteTargets, target, 99999),
        ack
    );
    n.tick(100000);
    n.tick(100000);
    assert_eq!(n.fault(), Fault::TargetExpired);
    assert!(n.recover_timeout_fault(100000));
    assert_eq!(n.state(), State::Safe);
    assert_eq!(n.ack().accepted, 0);
    assert_eq!(n.owner(), 0);
}
#[test]
fn groups_are_atomic_and_callbacks_see_candidates() {
    let r = resources(2);
    let a = actions(2);
    let mut n = DeviceCore::new(profile(2, &r, &a), 1, 10, VirtualDriver::default()).unwrap();
    activate(&mut n);
    let bad = targets(
        n.guard(),
        &[Target {
            endpoint: 1,
            resource: 1,
            value: 42,
        }],
    )
    .unwrap();
    let f = request(&mut n, 3, Command::WriteTargets, bad, 1);
    assert_eq!(f.kind, Kind::Error);
    assert_eq!(
        Reader::new(f.payload.as_slice()).u16().unwrap(),
        ErrorCode::Group as u16
    );
    assert_eq!(n.ack().accepted, 0);
}
#[test]
fn reply_lifetime_and_invalid_datagram_clear_old_reply() {
    let r = resources(1);
    let a = actions(1);
    let mut n = DeviceCore::new(profile(1, &r, &a), 1, 10, VirtualDriver::default()).unwrap();
    let raw = Frame::request(1, 1, Command::Info, 100, Payload::default())
        .encode()
        .unwrap();
    assert!(n.receive(raw.as_slice(), 0));
    assert!(n.take_reply(20000).is_some());
    n.receive(raw.as_slice(), 20000);
    assert!(n.take_reply(40001).is_none());
    n.receive(raw.as_slice(), 50000);
    n.receive(&[], 50001);
    assert!(n.take_reply(50001).is_none());
}
#[test]
fn local_inputs_keep_epoch_and_generation() {
    let r = resources(1);
    let a = actions(1);
    let mut n = DeviceCore::new(profile(1, &r, &a), 1, 10, VirtualDriver::default()).unwrap();
    n.select_local_source(Source::Remote, n.guard(), 100000, 300000, 0)
        .unwrap();
    n.tick(0);
    assert_eq!(
        n.local_targets(
            1,
            1,
            1,
            &[Target {
                endpoint: 1,
                resource: 1,
                value: 42
            }],
            1
        ),
        Err(ErrorCode::Generation)
    );
    n.local_targets(
        1,
        2,
        1,
        &[Target {
            endpoint: 1,
            resource: 1,
            value: 42,
        }],
        1,
    )
    .unwrap();
    assert_eq!(n.local_heartbeat(2, 2, 2), Err(ErrorCode::Epoch));
    assert_eq!(
        n.local_targets(
            1,
            2,
            1,
            &[Target {
                endpoint: 1,
                resource: 1,
                value: 42
            }],
            2
        ),
        Err(ErrorCode::Sequence)
    );
}
#[test]
fn safety_deadline_fault_priority_and_backward_clock() {
    let r = resources(1);
    let a = actions(1);
    let mut n = DeviceCore::new(profile(1, &r, &a), 1, 10, VirtualDriver::default()).unwrap();
    activate(&mut n);
    n.driver_mut().safe_delay = 500000;
    n.fault_input(1, Fault::TargetExpired);
    n.fault_input(2, Fault::LimitTrip);
    assert_eq!(n.fault(), Fault::LimitTrip);
    n.fault_input(3, Fault::Estop);
    n.tick(100001);
    assert_eq!(n.state(), State::Fault);
    assert_eq!(n.fault(), Fault::StopTimeout);
    assert!(!n.recover_timeout_fault(100001));
}
#[test]
fn configuration_single_send_records_and_strict_codecs() {
    assert!(radio_channels(16, 0).is_err());
    assert!(focus_positions(1, [0, 1, 0]).is_err());
    assert!(focus_button(2).is_err());
    let mut c = Client::<2>::new(100, 30).unwrap();
    c.set_endpoint(1, EP).unwrap();
    let ticket = c
        .write_focus_source(
            1,
            Guard {
                boot: 10,
                generation: 1,
                epoch: 1,
            },
            1,
            FocusSource::Wired,
        )
        .unwrap();
    let saved = c.request_record(ticket).unwrap();
    assert_eq!(c.next_transmission(0).unwrap().frame, saved);
    assert!(c.next_transmission(30000).is_none());
    assert_eq!(c.take_result(ticket), Some(Err(ClientError::Timeout)));
}
#[test]
fn retry_identity_response_matching_concurrency_and_stop() {
    let mut c = Client::<3>::new(100, 30).unwrap();
    c.set_endpoint(1, EP).unwrap();
    let ep2 = Endpoint { port: 47822, ..EP };
    c.set_endpoint(2, ep2).unwrap();
    let t = c.get_info(1).unwrap();
    c.get_info(2).unwrap();
    let first = c.next_transmission(0).unwrap();
    assert_eq!(first.endpoint, Some(EP));
    assert_eq!(c.next_transmission(0).unwrap().endpoint, Some(ep2));
    assert_eq!(c.next_transmission(30000).unwrap().frame, first.frame);
    assert_eq!(c.next_transmission(30000).unwrap().endpoint, Some(ep2));
    let f = Frame {
        kind: Kind::Response,
        src_uid: 1,
        dst_uid: 0,
        sequence: t.sequence,
        command: Command::Info as u16,
        session: 100,
        payload: Info {
            uid: 1,
            device_type: 0xf000,
            firmware: 1,
            boot: 10,
            profile_version: 1,
            epoch: 1,
        }
        .encode(),
    };
    c.feed(f.encode().unwrap().as_slice(), ep2, 30001);
    assert!(c.take_result(t).is_none());
    c.feed(f.encode().unwrap().as_slice(), EP, 30001);
    assert!(c.take_result(t).unwrap().is_ok());
    c.scan().unwrap();
    c.stop(0, None, true).unwrap();
    let stop = Frame::decode(c.next_transmission(30002).unwrap().frame.as_slice()).unwrap();
    assert_eq!(stop.command, Command::Estop as u16);
    assert_eq!(stop.dst_uid, 0);
    assert!(stop.payload.is_empty());
    assert!(c.next_transmission(60000).is_none());
}
#[test]
fn discovery_does_not_block_targets_and_reconnect_requires_rearm() {
    let r = resources(1);
    let a = actions(1);
    let mut n = DeviceCore::new(profile(1, &r, &a), 1, 10, VirtualDriver::default()).unwrap();
    let mut c = Client::<3>::new(100, 30).unwrap();
    c.scan().unwrap();
    pump(&mut c, &mut n, 0);
    pump(&mut c, &mut n, 100000);
    pump(&mut c, &mut n, 100001);
    assert!(c.scan_complete());
    assert_eq!(c.peer(1).unwrap().state, ConnectionState::Ready);
    c.reconnect(1).unwrap();
    assert!(c.needs_rearm(1));
    pump(&mut c, &mut n, 100002);
    pump(&mut c, &mut n, 200002);
    pump(&mut c, &mut n, 200003);
    assert!(c.scan_complete());
    assert!(c.needs_rearm(1));
    assert!(c.heartbeat(1, n.guard()).is_err());
    let t = c.open_session(1, n.guard()).unwrap();
    pump(&mut c, &mut n, 200004);
    assert!(c.take_result(t).unwrap().is_ok());
    assert!(!c.needs_rearm(1));
}
#[test]
fn duplicate_uid_probe_conflict_is_not_address_change() {
    let mut c = Client::<2>::new(100, 30).unwrap();
    c.scan().unwrap();
    let discover = Frame::decode(c.next_transmission(0).unwrap().frame.as_slice()).unwrap();
    let d = Discovery {
        device_type: DeviceType::VirtualLens as u16,
        firmware: 1,
        boot: 10,
        epoch: 1,
        generation: 1,
    };
    let reply = Frame {
        kind: Kind::Response,
        src_uid: 1,
        dst_uid: 0,
        sequence: discover.sequence,
        command: discover.command,
        session: 100,
        payload: d.encode(),
    }
    .encode()
    .unwrap();
    let ep2 = Endpoint { port: 47822, ..EP };
    c.feed(reply.as_slice(), EP, 0);
    c.feed(reply.as_slice(), ep2, 0);
    for _ in 0..2 {
        let t = c.next_transmission(100000).unwrap();
        let f = Frame::decode(t.frame.as_slice()).unwrap();
        let reply = Frame {
            kind: Kind::Response,
            src_uid: 1,
            dst_uid: 0,
            sequence: f.sequence,
            command: f.command,
            session: f.session,
            payload: Info {
                uid: 1,
                device_type: d.device_type,
                firmware: 1,
                boot: 10,
                profile_version: 1,
                epoch: 1,
            }
            .encode(),
        }
        .encode()
        .unwrap();
        c.feed(reply.as_slice(), t.endpoint.unwrap(), 100000);
    }
    c.next_transmission(100001);
    assert_eq!(c.peer(1).unwrap().state, ConnectionState::Conflict);
    assert!(c.get_info(1).is_err());
}
#[test]
fn latest_targets_merge_distinct_unsent_resources() {
    let mut c = Client::<1>::new(100, 30).unwrap();
    c.set_endpoint(1, EP).unwrap();
    let g = Guard {
        boot: 10,
        generation: 2,
        epoch: 2,
    };
    c.queue_targets(
        1,
        g,
        &[Target {
            endpoint: 1,
            resource: 1,
            value: 1,
        }],
    )
    .unwrap();
    c.queue_targets(
        1,
        g,
        &[Target {
            endpoint: 1,
            resource: 2,
            value: 2,
        }],
    )
    .unwrap();
    c.queue_targets(
        1,
        g,
        &[Target {
            endpoint: 1,
            resource: 1,
            value: 3,
        }],
    )
    .unwrap();
    let f = Frame::decode(c.next_transmission(0).unwrap().frame.as_slice()).unwrap();
    assert_eq!(
        f.payload,
        targets(
            g,
            &[
                Target {
                    endpoint: 1,
                    resource: 1,
                    value: 3
                },
                Target {
                    endpoint: 1,
                    resource: 2,
                    value: 2
                }
            ]
        )
        .unwrap()
    );
}
#[test]
fn scheduler_finish_waits_for_applied_then_safe() {
    let r = resources(1);
    let a = actions(1);
    let mut n = DeviceCore::new(profile(1, &r, &a), 1, 10, VirtualDriver::default()).unwrap();
    activate(&mut n);
    let mut c = Client::<2>::new(100, 30).unwrap();
    c.set_endpoint(1, EP).unwrap(); // Advance host sequence past explicit activation requests.
    for _ in 0..3 {
        let t = c.get_info(1).unwrap();
        pump(&mut c, &mut n, 0);
        let _ = c.take_result(t);
    }
    let mut caps = Capabilities::default();
    for i in 0..5 {
        caps.push(n.profile().caps(i).unwrap().as_slice()).unwrap();
    }
    let status = n.status(0, 0).unwrap();
    let mut keys = [ResourceKey::default(); 16];
    for (i, key) in keys.iter_mut().take(3).enumerate() {
        *key = ResourceKey {
            endpoint: 1,
            resource: (i + 1) as u16,
        };
    }
    let schedule = Schedule {
        uid: 1,
        caps,
        status,
        hz: 100,
        status_hz: 10,
        heartbeat_hz: 10,
        target_ttl_us: 100000,
        lease_ttl_us: 300000,
        resources: keys,
        resource_count: 3,
    };
    let mut scheduler = RealtimeScheduler::<2>::default();
    scheduler.configure(&c, &[schedule], 1000).unwrap();
    let values = [1, 2, 3].map(|resource| Target {
        endpoint: 1,
        resource,
        value: 100,
    });
    scheduler.set_targets(&mut c, 1, &values).unwrap();
    scheduler.start(&c).unwrap();
    for now in 0..10 {
        scheduler.tick(&mut c, now * 1000);
        pump(&mut c, &mut n, now * 1000);
    }
    assert_eq!(scheduler.phase(1), Some(Phase::Running));
    scheduler.finish(&mut c, 1, &values, 10000, 100000).unwrap();
    for now in 10..30 {
        scheduler.tick(&mut c, now * 1000);
        pump(&mut c, &mut n, now * 1000);
    }
    assert_eq!(scheduler.phase(1), Some(Phase::Finished));
    assert_eq!(n.state(), State::Safe);
}
#[test]
fn timeline_fallback_is_latched_until_next_start() {
    let mut t = HostTimeline::default();
    let p = Packet {
        epoch: 1,
        sequence: 1,
        time_us: 10000,
    };
    assert!(t.observe(p, 0, 0));
    assert_eq!(t.start(10).unwrap(), 10010);
    assert_eq!(t.now(30000).unwrap(), 40000);
    assert!(t.fallback());
    assert!(t.observe(
        Packet {
            epoch: 2,
            sequence: 0,
            time_us: 9
        },
        30001,
        30001
    ));
    assert_eq!(t.now(30002).unwrap(), 40002);
    assert!(t.fallback());
    assert!(t.now(1).is_err());
    t.stop();
    assert_eq!(t.start(30003).unwrap(), 11);
    assert!(!t.fallback());
}
#[test]
fn udp_loopback_and_oversize_datagrams() {
    use mobocom_protocol::udp::UdpTransport;
    use std::net::{Ipv4Addr, UdpSocket};
    let adapter = UdpTransport::bind(Ipv4Addr::LOCALHOST, 0).unwrap();
    let sender = UdpSocket::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
    sender
        .send_to(&[0; 600], adapter.socket().local_addr().unwrap())
        .unwrap();
    let mut buffer = [0; MAX_FRAME + 1];
    let (n, _) = loop {
        if let Some(value) = adapter.receive(&mut buffer).unwrap() {
            break value;
        }
    };
    assert_eq!(n, MAX_FRAME + 1);
    assert!(Frame::decode(&buffer[..n]).is_err());
}
