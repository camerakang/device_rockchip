//! Optional std UDP adapter; selected interface is explicit, TTL=1, bounded nonblocking pump.
use crate::{
    client::{Client, Endpoint},
    wire::{MAX_FRAME, MULTICAST, PORT},
};
use std::{
    io,
    net::{Ipv4Addr, SocketAddr, SocketAddrV4, UdpSocket},
};
pub struct UdpTransport {
    socket: UdpSocket,
}
impl UdpTransport {
    /// Host binds the chosen interface address. OS routing must route multicast via that interface.
    /// For multi-homed systems requiring IP_MULTICAST_IF, configure the socket externally and use
    /// from_socket; std does not expose an outgoing multicast-interface setter.
    pub fn bind(interface: Ipv4Addr, local_port: u16) -> io::Result<Self> {
        if interface.is_unspecified() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "select control interface explicitly",
            ));
        }
        Self::from_socket(UdpSocket::bind(SocketAddrV4::new(interface, local_port))?)
    }
    pub fn from_socket(socket: UdpSocket) -> io::Result<Self> {
        socket.set_nonblocking(true)?;
        socket.set_multicast_ttl_v4(1)?;
        Ok(Self { socket })
    }
    /// Device listener joins the fixed group on the selected interface; keep calling DeviceCore::tick.
    pub fn device(interface: Ipv4Addr) -> io::Result<Self> {
        let socket = UdpSocket::bind(SocketAddrV4::new(Ipv4Addr::UNSPECIFIED, PORT))?;
        socket.join_multicast_v4(&Ipv4Addr::from(MULTICAST), &interface)?;
        Self::from_socket(socket)
    }
    pub fn socket(&self) -> &UdpSocket {
        &self.socket
    }
    pub fn send(&self, data: &[u8], endpoint: Option<Endpoint>) -> io::Result<()> {
        let e = endpoint.unwrap_or(Endpoint {
            address: MULTICAST,
            port: PORT,
        });
        let n = self
            .socket
            .send_to(data, SocketAddrV4::new(Ipv4Addr::from(e.address), e.port))?;
        if n != data.len() {
            return Err(io::Error::new(io::ErrorKind::WriteZero, "partial datagram"));
        }
        Ok(())
    }
    /// Buffer has MAX_FRAME+1 bytes: truncated oversized datagrams cannot look like valid frames.
    pub fn receive(
        &self,
        buffer: &mut [u8; MAX_FRAME + 1],
    ) -> io::Result<Option<(usize, Endpoint)>> {
        match self.socket.recv_from(buffer) {
            Ok((n, SocketAddr::V4(from))) => Ok(Some((
                n,
                Endpoint {
                    address: from.ip().octets(),
                    port: from.port(),
                },
            ))),
            Ok(_) => Ok(None),
            Err(e) if e.kind() == io::ErrorKind::WouldBlock => Ok(None),
            Err(e) => Err(e),
        }
    }
    pub fn pump<const N: usize>(
        &self,
        client: &mut Client<N>,
        now: u64,
        max_datagrams: usize,
    ) -> io::Result<()> {
        let mut buffer = [0; MAX_FRAME + 1];
        for _ in 0..max_datagrams {
            let Some((n, from)) = self.receive(&mut buffer)? else {
                break;
            };
            client.feed(&buffer[..n], from, now);
        }
        for _ in 0..max_datagrams {
            let Some(t) = client.next_transmission(now) else {
                break;
            };
            if let Err(e) = self.send(t.frame.as_slice(), t.endpoint) {
                client.send_failed(t.sequence);
                return Err(e);
            }
        }
        Ok(())
    }
}
