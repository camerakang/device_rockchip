use mobocom_protocol::{client::*, model::*, wire::*};
const EP: Endpoint = Endpoint {
    address: [127, 0, 0, 1],
    port: 47821,
};
#[test]
fn unsent_request_is_replaced_but_sent_request_is_immutable() {
    let mut c = Client::<1>::new(100, 30).unwrap();
    c.set_endpoint(1, EP).unwrap();
    let g = Guard {
        boot: 10,
        epoch: 2,
        generation: 2,
    };
    let a = Target {
        endpoint: 1,
        resource: 1,
        value: 1,
    };
    let b = Target {
        resource: 2,
        value: 2,
        ..a
    };
    let ticket = c.write_targets(1, g, &[a]).unwrap();
    c.queue_targets(1, g, &[b]).unwrap();
    let first = c.next_transmission(0).unwrap();
    assert_eq!(
        Frame::decode(first.frame.as_slice()).unwrap().payload,
        targets(g, &[a, b]).unwrap()
    );
    c.queue_targets(1, g, &[Target { value: 3, ..a }]).unwrap();
    assert_eq!(c.request_record(ticket).unwrap(), first.frame);
    let next = c.next_transmission(30000).unwrap();
    assert_ne!(first.sequence, next.sequence);
    assert_eq!(c.take_result(ticket), Some(Err(ClientError::Superseded)));
    assert_eq!(
        Frame::decode(next.frame.as_slice()).unwrap().payload,
        targets(g, &[Target { value: 3, ..a }]).unwrap()
    );
}
#[test]
fn global_stop_does_not_downgrade_pending_estop() {
    let mut c = Client::<1>::new(100, 30).unwrap();
    c.stop(0, None, true).unwrap();
    c.stop(0, None, false).unwrap();
    assert_eq!(
        Frame::decode(c.next_transmission(0).unwrap().frame.as_slice())
            .unwrap()
            .command,
        Command::Estop as u16
    );
}
fn discover(c: &mut Client<2>, now: u64, endpoints: &[Endpoint]) {
    c.scan().unwrap();
    let t = c.next_transmission(now).unwrap();
    let request = Frame::decode(t.frame.as_slice()).unwrap();
    let frame = Frame {
        kind: Kind::Response,
        src_uid: 1,
        dst_uid: 0,
        sequence: request.sequence,
        command: request.command,
        session: 100,
        payload: Discovery {
            device_type: 0x8001,
            firmware: 1,
            boot: 10,
            epoch: 1,
            generation: 1,
        }
        .encode(),
    };
    for ep in endpoints {
        c.feed(frame.encode().unwrap().as_slice(), *ep, now);
    }
}
#[test]
fn ambiguous_address_is_probed_even_after_lost_discovery() {
    let mut c = Client::<2>::new(100, 30).unwrap();
    let second = Endpoint { port: 47822, ..EP };
    discover(&mut c, 0, &[EP, second]);
    for now in [100000, 200001] {
        for _ in 0..2 {
            let t = c.next_transmission(now).unwrap();
            let f = Frame::decode(t.frame.as_slice()).unwrap();
            let response = Frame {
                kind: Kind::Response,
                src_uid: 1,
                dst_uid: 0,
                sequence: f.sequence,
                command: f.command,
                session: f.session,
                payload: Info {
                    uid: 1,
                    device_type: 0x8001,
                    firmware: 1,
                    boot: 10,
                    profile_version: 1,
                    epoch: 1,
                }
                .encode(),
            };
            c.feed(
                response.encode().unwrap().as_slice(),
                t.endpoint.unwrap(),
                now,
            );
        }
        c.next_transmission(now + 1);
        assert_eq!(c.peer(1).unwrap().state, ConnectionState::Conflict);
        if now == 100000 {
            discover(&mut c, 100001, &[EP]);
        }
    }
}
#[test]
fn stable_background_scan_does_not_require_info_or_rearm() {
    let mut c = Client::<2>::new(100, 30).unwrap();
    discover(&mut c, 0, &[EP]);
    let t = c.next_transmission(100000).unwrap();
    let f = Frame::decode(t.frame.as_slice()).unwrap();
    let response = Frame {
        kind: Kind::Response,
        src_uid: 1,
        dst_uid: 0,
        sequence: f.sequence,
        command: f.command,
        session: f.session,
        payload: Info {
            uid: 1,
            device_type: 0x8001,
            firmware: 1,
            boot: 10,
            profile_version: 1,
            epoch: 1,
        }
        .encode(),
    };
    c.feed(response.encode().unwrap().as_slice(), EP, 100000);
    c.next_transmission(100001);
    assert!(!c.needs_rearm(1));
    let version = c.connection_version(1);
    discover(&mut c, 100002, &[EP]);
    assert!(c.next_transmission(200002).is_none());
    assert!(c.scan_complete());
    assert!(!c.needs_rearm(1));
    assert_eq!(version, c.connection_version(1));
}
#[test]
fn full_capacity_scan_completes_and_reports_overflow() {
    let mut c = Client::<1>::new(100, 30).unwrap();
    c.set_endpoint(1, EP).unwrap();
    c.scan().unwrap();
    let f = Frame::decode(c.next_transmission(0).unwrap().frame.as_slice()).unwrap();
    let response = Frame {
        kind: Kind::Response,
        src_uid: 2,
        dst_uid: 0,
        sequence: f.sequence,
        command: f.command,
        session: f.session,
        payload: Discovery {
            device_type: 0x8001,
            firmware: 1,
            boot: 10,
            epoch: 1,
            generation: 1,
        }
        .encode(),
    };
    c.feed(response.encode().unwrap().as_slice(), EP, 0);
    assert!(c.scan_overflow());
    c.next_transmission(100000);
    assert!(c.scan_complete());
}
