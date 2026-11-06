//! Read-only discovery. No ownership acquisition, motion or automatic recovery.
use mobocom_protocol::{client::Client, udp::UdpTransport};
use std::{
    net::Ipv4Addr,
    thread,
    time::{Duration, Instant},
};
fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<String> = std::env::args().collect();
    if args.len() != 3 {
        eprintln!("usage: discover <control-interface-ipv4> <fresh-nonzero-session-u64>");
        std::process::exit(2)
    }
    let interface: Ipv4Addr = args[1].parse()?;
    let session: u64 = args[2].parse()?;
    let udp = UdpTransport::bind(interface, 0)?;
    let mut client = Client::<32>::new(session, 30).map_err(|e| format!("{e:?}"))?;
    client.scan().map_err(|e| format!("{e:?}"))?;
    let start = Instant::now();
    while !client.scan_complete() && start.elapsed() < Duration::from_secs(2) {
        udp.pump(&mut client, start.elapsed().as_micros() as u64, 64)?;
        thread::sleep(Duration::from_millis(1));
    }
    for peer in client.peers() {
        println!("{peer:?}");
    }
    if client.scan_overflow() {
        eprintln!("discovery capacity exceeded: result incomplete");
    }
    if !client.scan_complete() {
        return Err("discovery did not complete".into());
    }
    Ok(())
}
