#!/usr/bin/env python3
"""Network check of a running ss07-mobocom-node using the vendored protocol codec.

Sends only DISCOVER/INFO/CAPS/STATUS, global STOP/ESTOP (dst_uid=0) and a unicast
RESET_FAULT, plus malformed frames. No session, source, target or configuration
command is sent. Prints one JSON report; exit status 1 on any failed check.
"""
import __future__
import argparse
import json
import os
from pathlib import Path
import socket
import struct
import sys
import time
import types

ROOT = Path(__file__).resolve().parents[1]
WIRE = ROOT / 'lib/mobocom_protocol/mobocom_realtime/wire.py'
PORT, GROUP, VIDEO_SONAR = 47821, '239.255.33.21', 0x9009


def load_wire():
    """Load the unmodified codec; postponed annotations keep it importable on Python 3.8."""
    module = types.ModuleType('mobocom_wire')
    code = compile(WIRE.read_text(), str(WIRE), 'exec', flags=__future__.annotations.compiler_flag, dont_inherit=True)
    sys.modules[module.__name__] = module
    exec(code, module.__dict__)
    return module


wire = load_wire()


class Probe:
    def __init__(self, target, interface_ip):
        self.target, self.session, self.sequence = target, int.from_bytes(os.urandom(8), 'little') | 1, 0
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((interface_ip or '0.0.0.0', 0))
        self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
        if interface_ip:
            self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(interface_ip))

    def frame(self, dst, command, payload=b''):
        self.sequence += 1
        return wire.encode(wire.Frame(wire.Kind.REQUEST, 0, dst, self.sequence, command, self.session, payload))

    def exchange(self, raw, address=None, wait=0.2, many=False):
        """Send raw bytes; return [(frame, source, rtt_ms)] of replies to this session/sequence."""
        seq = struct.unpack_from('<I', raw, 21)[0] if len(raw) >= 25 else None
        while True:  # drop stale replies
            self.sock.setblocking(False)
            try: self.sock.recvfrom(512)
            except BlockingIOError: break
        self.sock.setblocking(True)
        start = time.monotonic()
        self.sock.sendto(raw, address or (self.target, PORT))
        replies = []
        while time.monotonic() - start < wait:
            self.sock.settimeout(max(0.001, wait - (time.monotonic() - start)))
            try: data, source = self.sock.recvfrom(512)
            except socket.timeout: break
            try: f = wire.decode(data)
            except ValueError: continue
            if f.session == self.session and f.sequence == seq:
                replies.append((f, source, round((time.monotonic() - start) * 1000, 2)))
                if not many: break
        return replies

    def request(self, uid, command, payload=b''):
        replies = self.exchange(self.frame(uid, command, payload))
        return replies[0][0] if replies else None


def status_fields(payload):
    boot, generation, _version, state, source, fault = struct.unpack_from('<QIIBBH', payload)
    epoch = struct.unpack_from('<Q', payload, 56)[0]
    return dict(boot=boot, generation=generation, state=state, source=source, fault=fault, epoch=epoch,
                safe_confirmed=payload[52])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', required=True, help='node IPv4')
    parser.add_argument('--interface-ip', help='local control interface; enables the multicast DISCOVER check')
    parser.add_argument('--skip-estop', action='store_true', help='do not send ESTOP/RESET_FAULT')
    args = parser.parse_args()
    probe, checks, report = Probe(args.target, args.interface_ip), {}, {}

    def check(name, ok, **detail):
        checks[name] = dict(ok=bool(ok), **detail)

    if args.interface_ip:
        found = [r for r in probe.exchange(probe.frame(0, wire.Command.DISCOVER), (GROUP, PORT), wait=0.5, many=True)
                 if r[1][0] == args.target]
        check('multicast_discover', found and len(found[0][0].payload) == 26 and found[0][2] < 100,
              rtt_ms=found[0][2] if found else None)
    found = probe.exchange(probe.frame(0, wire.Command.DISCOVER))
    if not found:
        check('discover', False); print(json.dumps(dict(checks=checks), indent=2)); return 1
    reply, source, rtt = found[0]
    device_type, firmware, boot = struct.unpack_from('<HIQ', reply.payload)
    uid = reply.src_uid
    report.update(uid=str(uid), uid_hex='0x%016x' % uid, firmware='0x%08x' % firmware, boot=str(boot), source='%s:%d' % source)
    check('discover', len(reply.payload) == 26 and device_type == VIDEO_SONAR and source[1] == PORT, rtt_ms=rtt)
    info = probe.request(uid, wire.Command.INFO)
    check('info', info is not None and info.kind == wire.Kind.RESPONSE and len(info.payload) == 33
          and struct.pack('<H', VIDEO_SONAR) in info.payload)
    for page in (0, 1):
        caps = probe.request(uid, wire.Command.CAPS, struct.pack('<H', page))
        check('caps_page%d' % page, caps is not None and caps.kind == wire.Kind.RESPONSE)
    st = probe.request(uid, wire.Command.STATUS, b'\0\0')
    s = status_fields(st.payload) if st else None
    check('status_safe', s and s['state'] == 0 and s['fault'] == 0 and s['safe_confirmed'] == 1, status=s)

    stop = probe.exchange(probe.frame(0, wire.Command.STOP))
    s = status_fields(probe.request(uid, wire.Command.STATUS, b'\0\0').payload)
    check('global_stop_no_reply_still_safe', not stop and s['state'] == 0)
    if not args.skip_estop:
        estop = probe.exchange(probe.frame(0, wire.Command.ESTOP))
        time.sleep(0.05)
        s = status_fields(probe.request(uid, wire.Command.STATUS, b'\0\0').payload)
        check('global_estop_fault_7', not estop and s['state'] == 4 and s['fault'] == 7, status=s)
        ack = probe.request(uid, wire.Command.RESET_FAULT, wire.guard(s['boot'], s['generation'], s['epoch']))
        after = status_fields(probe.request(uid, wire.Command.STATUS, b'\0\0').payload)
        check('reset_fault_safe', ack is not None and ack.kind == wire.Kind.ACK and after['state'] == 0
              and after['fault'] == 0 and after['generation'] == s['generation'] + 1, status=after)

    good = probe.frame(uid, wire.Command.INFO)
    bad = dict(crc=good[:-1] + bytes([good[-1] ^ 0xff]), trailing=good + b'\0', concatenated=good + good,
               wrong_dst=probe.frame(uid ^ 1, wire.Command.INFO), oversized=b'\xd5' * 300,
               multicast_info=probe.frame(0, wire.Command.INFO),
               multicast_status=probe.frame(0, wire.Command.STATUS, b'\0\0'), garbage=b'\0' * 3)
    silent = {name: not probe.exchange(raw, wait=0.15) for name, raw in bad.items()}
    check('malformed_no_reply', all(silent.values()), cases=silent)
    alive = probe.request(uid, wire.Command.INFO)
    again = probe.exchange(probe.frame(0, wire.Command.DISCOVER))
    check('still_alive_same_boot', alive is not None and again and struct.unpack_from('<HIQ', again[0][0].payload)[2] == boot)
    report['checks'] = checks
    print(json.dumps(report, indent=2))
    return 0 if all(c['ok'] for c in checks.values()) else 1


if __name__ == '__main__':
    sys.exit(main())
