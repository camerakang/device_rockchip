#!/usr/bin/env python3
"""Verified TLS/RTSP + native decode, six synthetic sources, recording and revocation.

The RTSP test client depacketizes only test media for an independent decoder.
No production path reads video in Python. No device, installation or system trust change.
"""
import argparse
import base64
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from ss07.accounts import Accounts, password_record
from ss07.api import Server
from ss07.audit import Audit
from ss07.catalog import Catalog
from ss07.config import revision
from ss07.media import CHANNELS, MediaObserver, get_paths, render, rtp_port
from ss07.persistence import atomic_json
from ss07.recorder import RecorderSession, verify_binary
from ss07.snapshots import SnapshotReader
from ss07.state import State
from ss07.volume import DirectoryVolume, open_directory
from ss07.worker import terminate

spec = importlib.util.spec_from_file_location('media_fixture', ROOT / 'tools/test-media.py')
fixture = importlib.util.module_from_spec(spec); spec.loader.exec_module(fixture)


class RTSPError(ValueError): pass


class Reader:
    def __init__(self, port, certificate, channel, user=None, password=None):
        self.url = 'rtsps://127.0.0.1:%d/%s' % (port, channel)
        self.cseq, self.session, self.buffer = 0, None, bytearray()
        self.basic = base64.b64encode((user + ':' + password).encode()).decode() if user else None
        context = ssl.create_default_context(cafile=str(certificate))
        self.socket = context.wrap_socket(socket.create_connection(('127.0.0.1', port), timeout=8), server_hostname='127.0.0.1')
        self.thread = None
        self.stop = threading.Event()
        self.closed_at, self.error, self.packets, self.gaps, self.frames = None, None, 0, 0, 0
        self.last_sequence = None
        self.elementary, self.fragment = bytearray(), bytearray()

    def exact(self, size):
        while len(self.buffer) < size:
            data = self.socket.recv(65536)
            if not data: raise EOFError()
            self.buffer.extend(data)
            if len(self.buffer) > 262144: raise RTSPError('bounded test receive buffer exceeded')
        data = bytes(self.buffer[:size]); del self.buffer[:size]
        return data

    def request(self, method, url=None, headers=None, body=b''):
        self.cseq += 1
        head = {'CSeq': str(self.cseq), 'User-Agent': 'SS07-synthetic-test'}
        if self.basic: head['Authorization'] = 'Basic ' + self.basic
        if self.session: head['Session'] = self.session
        if headers: head.update(headers)
        if body: head['Content-Length'] = str(len(body))
        wire = method + ' ' + (url or self.url) + ' RTSP/1.0\r\n' + ''.join(k + ': ' + v + '\r\n' for k, v in head.items()) + '\r\n'
        self.socket.sendall(wire.encode() + body)
        while b'\r\n\r\n' not in self.buffer:
            data = self.socket.recv(65536)
            if not data: raise EOFError()
            self.buffer.extend(data)
            if len(self.buffer) > 65536: raise RTSPError('bounded test response exceeded')
        split = self.buffer.index(b'\r\n\r\n')
        raw = self.exact(split + 4).decode()
        lines = raw.split('\r\n'); status = int(lines[0].split()[1])
        fields = {line.split(':', 1)[0].lower(): line.split(':', 1)[1].strip() for line in lines[1:] if ':' in line}
        size = int(fields.get('content-length', '0'))
        if not 0 <= size <= 65536: raise RTSPError('bounded test body exceeded')
        return status, fields, self.exact(size)

    def start(self):
        status, headers, sdp = self.request('DESCRIBE', headers={'Accept': 'application/sdp'})
        if status != 200: raise RTSPError('DESCRIBE status %d' % status)
        text = sdp.decode()
        match = re.search(r'sprop-parameter-sets=([^;\r\n]+)', text)
        if match:
            for part in match[1].split(','): self.elementary.extend(b'\0\0\0\1' + base64.b64decode(part))
        controls = re.findall(r'^a=control:(.+)\r?$', text, re.MULTILINE)
        control = next(c.strip() for c in controls if c.strip() != '*')
        url = control if control.startswith('rtsps://') else self.url + '/' + control
        status, headers, _ = self.request('SETUP', url, {'Transport': 'RTP/AVP/TCP;unicast;interleaved=0-1'})
        if status != 200: raise RTSPError('SETUP status %d' % status)
        self.session = headers['session'].split(';', 1)[0]
        status, _, _ = self.request('PLAY')
        if status != 200: raise RTSPError('PLAY status %d' % status)
        self.thread = threading.Thread(target=self.receive, daemon=True); self.thread.start()
        return self

    def receive(self):
        try:
            while not self.stop.is_set():
                head = self.exact(4)
                if head[0] != 36: raise RTSPError('expected interleaved RTP')
                packet = self.exact(struct.unpack('!H', head[2:])[0])
                if head[1] != 0: continue
                if len(packet) < 12 or packet[0] >> 6 != 2: raise RTSPError('invalid RTP')
                seq = struct.unpack('!H', packet[2:4])[0]
                if self.last_sequence is not None and seq != (self.last_sequence + 1) % 65536: self.gaps += 1
                self.last_sequence = seq; self.packets += 1
                offset = 12 + (packet[0] & 15) * 4
                if packet[0] & 16: offset += 4 + struct.unpack('!H', packet[offset+2:offset+4])[0] * 4
                payload = packet[offset:len(packet) - packet[-1] if packet[0] & 32 else len(packet)]
                kind = payload[0] & 31
                nal = b''
                if 1 <= kind <= 23: nal = b'\0\0\0\1' + payload
                elif kind == 24:
                    i = 1
                    while i < len(payload):
                        size = struct.unpack('!H', payload[i:i+2])[0]; i += 2
                        nal += b'\0\0\0\1' + payload[i:i+size]; i += size
                elif kind == 28:
                    if payload[1] & 128: self.fragment = bytearray([(payload[0] & 224) | (payload[1] & 31)])
                    self.fragment.extend(payload[2:])
                    if payload[1] & 64: nal = b'\0\0\0\1' + self.fragment; self.fragment.clear()
                else: raise RTSPError('unsupported test RTP NAL type')
                # Capture only the initial sample; keep draining until revocation.
                if len(self.elementary) + len(nal) <= 8 * 1024 * 1024: self.elementary.extend(nal)
                if packet[1] & 128: self.frames += 1
        except (OSError, EOFError): pass
        except Exception as exc: self.error = type(exc).__name__
        finally: self.closed_at = time.monotonic()

    def close(self):
        self.stop.set()
        try: self.socket.shutdown(socket.SHUT_RDWR)
        except OSError: pass
        self.socket.close()
        if self.thread: self.thread.join(timeout=4)


def gstreamer_reader(port, certificate, password, output, client_runtime=None):
    # Keep the installed native client libraries separate from the synthetic
    # camera's test sysroot. A foreign-library crash cannot strand the fixture.
    env = {key: value for key, value in os.environ.items() if not key.startswith(('GST_', 'LD_'))}
    if client_runtime:
        env.update(GST_PLUGIN_PATH=str(client_runtime.resolve() / 'plugins'),
                   LD_LIBRARY_PATH=str(client_runtime.resolve() / 'lib'), GST_REGISTRY=str(output / 'client-registry.bin'))
    raw = output / 'native-client.h264'
    value = dict(url='rtsps://127.0.0.1:%d/front' % port, username='reader', password=password,
                 certificate=str(certificate), output=str(raw))
    result = subprocess.run([sys.executable, str(ROOT / 'tools/test-rtsp-client.py')], input=json.dumps(value),
                            capture_output=True, text=True, env=env, timeout=20)
    (output / 'native-client.log').write_text(result.stdout + result.stderr)
    if result.returncode != 0: raise RTSPError('native client failed; see native-client.log')
    report = json.loads(result.stdout)
    decoded = subprocess.run(['gst-launch-1.0', '-v', 'filesrc', 'location=' + str(raw), '!', 'h264parse',
        '!', 'openh264dec', '!', 'video/x-raw,width=640,height=360', '!', 'identity', 'silent=false', '!', 'fakesink'],
        capture_output=True, text=True, timeout=20)
    (output / 'native-client-decode.log').write_text(decoded.stdout + decoded.stderr)
    frames = len(re.findall(r'GstIdentity:[^\n]*last-message = chain', decoded.stdout))
    assert decoded.returncode == 0 and frames >= 20 and 'Got EOS' in decoded.stdout
    report['decoded_frames'] = frames
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('camera-test', 'mediamtx', 'chromium', 'output'): parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--client-runtime', type=Path, help='optional isolated plugins/lib for the native test player')
    args = parser.parse_args()
    if args.output.exists(): parser.error('output must be new')
    args.output.mkdir(parents=True)
    verify_binary(args.mediamtx)
    evidence = dict(result='failed', evidence_kind='synthetic_test', hardware_verified=False, deployed=False,
                    scope='actual media supervisor/native RTSPS/verified TLS/six H264 decode/recorders/browser coexistence')
    procs, files, clients, errors = [], [], [], []
    recorder_stop = threading.Event(); record_thread = None
    config, upstream = fixture.configuration(), fixture.configuration()
    config['storage']['segment_seconds'] = 2
    config['sdi']['rtsp_url'] = 'rtsp://127.0.0.1:%d/front' % upstream['media']['rtsp_port']
    root = args.output / 'recordings'; root.mkdir()
    volume = DirectoryVolume(open_directory(root), 'native-rtsp-fixture', 'synthetic_test')
    catalog = Catalog(args.output / 'catalog.sqlite'); catalog.initialize()

    def start(name, argv, env=None):
        log = (args.output / (name + '.log')).open('wb'); files.append(log)
        proc = subprocess.Popen([str(a) for a in argv], stdout=log, stderr=subprocess.STDOUT, start_new_session=True, env=env)
        procs.append(proc); return proc

    def counts(): return Counter(row['channel_id'] for row in catalog.list(limit=200)['items'] if row['complete'])
    def lease(*args, **kw): return DirectoryVolume(open_directory(root), 'native-rtsp-fixture', 'synthetic_test')
    try:
        with tempfile.TemporaryDirectory(prefix='ss07-rtsps-') as tmp:
            runtime = Path(tmp)
            subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', str(runtime / 'key.pem'),
                '-out', str(runtime / 'cert.pem'), '-days', '1', '-subj', '/CN=127.0.0.1', '-addext', 'subjectAltName=IP:127.0.0.1'],
                check=True, capture_output=True)
            gateway = dict(schema_version=1, public_origin='https://127.0.0.1:%d' % fixture.port(), listen_ip='127.0.0.1',
                ice_ip='127.0.0.1', certificate=str(runtime / 'cert.pem'), private_key=str(runtime / 'key.pem'))
            profile = dict(schema_version=1, enabled=True, port=fixture.port())
            password = secrets.token_urlsafe(24); record = password_record(password)
            accounts = runtime / 'accounts.json'
            data = dict(schema_version=1, users={name: dict(role=role, enabled=True, password=record)
                for name, role in [('admin', 'maintainer'), ('viewer', 'monitor'), ('reader', 'reader')]})
            atomic_json(accounts, data, mode=0o640)
            for name, value in [('system', config), ('gateway', gateway), ('rtsp', profile)]:
                atomic_json(runtime / (name + '.json'), value, mode=0o640)
            up = render(upstream); up['paths'] = {'front': up['paths']['front']}
            (runtime / 'upstream.json').write_text(json.dumps(up))
            start('upstream', [args.mediamtx, runtime / 'upstream.json'])
            env = dict(os.environ, PYTHONPATH=str(ROOT / 'src'), PYTHONDONTWRITEBYTECODE='1')
            media = start('media-worker', [sys.executable, '-m', 'ss07', 'media-worker', '--config', runtime / 'system.json',
                '--gateway-config', runtime / 'gateway.json', '--rtsp-config', runtime / 'rtsp.json', '--accounts', accounts,
                '--rtsp-audit', args.output / 'rtsp-access.sqlite', '--state-dir', runtime / 'state', '--binary', args.mediamtx], env)
            snapshot = SnapshotReader(runtime / 'state', Path('/proc/sys/kernel/random/boot_id').read_text().strip())
            def ready():
                native = snapshot.read('media').get('payload', {}).get('native_rtsp', {})
                return native.get('state') == 'READY' and native.get('account_revision') == revision(data)
            fixture.wait_for(ready)
            for channel in CHANNELS:
                port = rtp_port(upstream, 'front') if channel == 'sdi' else rtp_port(config, channel)
                start('source-' + channel, [args.camera_test, '--device', 'synthetic-test', '--width', '640', '--height', '360',
                    '--fps-num', '25', '--fps-den', '1', '--port', port])
            observer = MediaObserver()
            fixture.wait_for(lambda: all(v['ready'] for v in observer.observe(get_paths(config))['streams'].values()))

            def client(channel='front', user='viewer', secret=password):
                value = Reader(profile['port'], runtime / 'cert.pem', channel, user, secret)
                clients.append(value); return value

            denied = {}
            evidence['denied'] = denied
            for name, params in [('anonymous', dict(user=None)), ('bad_password', dict(secret='wrong-test-password')),
                                 ('unknown_path', dict(channel='outside')), ('query_token', dict(channel='front?token=alternate'))]:
                value = client(**params)
                print('Checking refusal:', name, flush=True)
                code, _, _ = value.request('DESCRIBE', headers={'Accept': 'application/sdp'})
                assert code != 200, name
                denied[name] = code; value.close()
            value = client()
            sdp = render(config)['paths']['front']['rtpSDP'] + 'a=control:trackID=0\r\n'
            code, _, _ = value.request('ANNOUNCE', headers={'Content-Type': 'application/sdp'}, body=sdp.encode())
            assert code != 200; denied['publish'] = code; value.close()
            evidence['denied'] = denied
            try:
                with ssl.create_default_context().wrap_socket(socket.create_connection(('127.0.0.1', profile['port']), timeout=2),
                                                               server_hostname='127.0.0.1'): pass
            except ssl.SSLCertVerificationError: evidence['untrusted_certificate_rejected'] = True
            else: raise AssertionError('untrusted TLS unexpectedly accepted')
            with socket.create_connection(('127.0.0.1', profile['port']), timeout=2) as plain:
                plain.sendall(b'OPTIONS rtsp://127.0.0.1/front RTSP/1.0\r\nCSeq: 1\r\n\r\n')
                try: reply = plain.recv(4096)
                except ConnectionResetError: reply = b''
                assert not reply.startswith(b'RTSP/1.0 200')
            evidence['plaintext_rejected'] = True

            with patch('ss07.volume.MountLease.acquire', side_effect=lease), \
                    RecorderSession(config, catalog, volume, args.mediamtx, 'native-rtsp-fixture') as recorder:
                def record_tick():
                    try:
                        while not recorder_stop.is_set(): recorder.tick()
                    except Exception as exc: errors.append(type(exc).__name__)
                record_thread = threading.Thread(target=record_tick, daemon=True); record_thread.start()
                fixture.wait_for(lambda: all(counts()[c] >= 2 for c in CHANNELS), seconds=20)
                with ThreadPoolExecutor(max_workers=6) as pool:
                    viewers = list(pool.map(lambda channel: client(channel).start(), CHANNELS))
                for role in ('reader', 'admin'):
                    value = client(user=role).start()
                    fixture.wait_for(lambda: value.frames >= 5); value.close()
                fixture.wait_for(lambda: all(v.frames >= 25 for v in viewers))
                evidence['roles_read'] = ['monitor', 'reader', 'maintainer']
                evidence['native_gstreamer_client'] = gstreamer_reader(profile['port'], runtime / 'cert.pem', password, args.output, args.client_runtime)
                # Extra front readers cannot exceed the fixed native quota.
                extra = [client().start() for _ in range(3)]
                try: client().start()
                except RTSPError: evidence['per_channel_limit'] = 'four; fifth denied'
                else: raise AssertionError('reader quota not enforced')
                for value in extra: value.close()
                # A loopback-only development API lets the existing browser
                # harness test six real decoders without inventing camera status.
                for cycle in (1, 2):
                    before = [v.frames for v in viewers]
                    with Server(('127.0.0.1', config['api']['port']), State(config)) as server:
                        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
                        try:
                            if cycle == 1:
                                browser_dir = args.output / 'browser'; browser_dir.mkdir()
                                browser = start('browser', ['node', ROOT / 'tools/test-browser-media.cjs', args.chromium,
                                    'http://127.0.0.1:%d' % config['api']['port'], browser_dir])
                                browser.wait(timeout=60)
                                assert browser.returncode == 0, 'browser coexistence failed; see browser.log'
                                evidence['browser'] = json.loads((browser_dir / 'browser.json').read_text())
                            fixture.wait_for(lambda: all(v.frames > n + 10 for v, n in zip(viewers, before)))
                        finally: server.shutdown(); thread.join(3)
                    fixture.wait_for(lambda: all(v.frames > n + 20 for v, n in zip(viewers, before)))
                evidence['api_restart_preserves_native_reads'] = True
                baseline = counts()
                def children():
                    ids = Path('/proc/%d/task/%d/children' % (media.pid, media.pid)).read_text().split()
                    return {int(pid): Path('/proc/' + pid + '/cmdline').read_bytes() for pid in ids}
                internal_pid = next(pid for pid, cmd in children().items() if b'/mediamtx.json' in cmd)
                changed_at = time.monotonic()
                data['users']['viewer']['enabled'] = False; atomic_json(accounts, data, mode=0o640)
                fixture.wait_for(lambda: all(v.closed_at is not None for v in viewers), seconds=6)
                elapsed = max(v.closed_at for v in viewers) - changed_at
                assert elapsed < 5 and all(v.error is None and v.gaps == 0 for v in viewers)
                evidence['account_revocation_seconds'] = round(elapsed, 3)
                fixture.wait_for(ready)
                disabled = client()
                assert disabled.request('DESCRIBE', headers={'Accept': 'application/sdp'})[0] != 200
                disabled.close()
                assert internal_pid in children() and media.poll() is None
                fixture.wait_for(lambda: all(counts()[c] > baseline[c] for c in CHANNELS), seconds=10)
                evidence['recording_continues_after_revocation'] = dict(counts())

                # Losing the account file must also close a previously accepted reader.
                alive = client(user='admin').start(); fixture.wait_for(lambda: alive.frames >= 5)
                accounts.unlink(); lost_at = time.monotonic()
                fixture.wait_for(lambda: alive.closed_at is not None, seconds=6)
                assert alive.closed_at - lost_at < 5
                fixture.wait_for(lambda: not ready())
                atomic_json(accounts, data, mode=0o640); fixture.wait_for(ready, seconds=35)
                assert internal_pid in children()
                evidence['missing_accounts_revokes_and_recovers'] = True
                decoded = {}
                for channel, value in zip(CHANNELS, viewers):
                    raw = args.output / (channel + '.h264'); raw.write_bytes(value.elementary)
                    result = subprocess.run(['gst-launch-1.0', '-v', 'filesrc', 'location=' + str(raw), '!', 'h264parse',
                        '!', 'openh264dec', '!', 'video/x-raw,width=640,height=360', '!', 'identity', 'silent=false', '!', 'fakesink'],
                        capture_output=True, text=True, timeout=30)
                    (args.output / (channel + '-decode.log')).write_text(result.stdout + result.stderr)
                    frames = len(re.findall(r'GstIdentity:[^\n]*last-message = chain', result.stdout))
                    assert result.returncode == 0 and frames >= 20 and 'Got EOS' in result.stdout
                    decoded[channel] = dict(decoded_frames=frames, rtp_packets=value.packets, sequence_gaps=value.gaps)
                evidence['six_rtsps_decoders'] = decoded
                events = Audit(args.output / 'rtsp-access.sqlite').history(limit=100)['items']
                assert any(row['kind'] == 'RTSP_REVOKE' for row in events)
                assert any(row['kind'] == 'RTSP_LOGIN_FAILED' for row in events)
                assert password not in json.dumps(events)
                evidence['audit_kinds'] = sorted({e['kind'] for e in events})
                evidence['completed_counts'] = dict(counts())
                assert not errors, errors
                recorder_stop.set(); record_thread.join(4)
                evidence['result'] = 'passed'
    finally:
        for value in clients: value.close()
        recorder_stop.set()
        if record_thread: record_thread.join(4)
        for proc in reversed(procs): terminate(proc)
        for stream in files: stream.close()
        volume.close()
        (args.output / 'result.json').write_text(json.dumps(evidence, indent=2) + '\n')
    print(json.dumps(evidence, indent=2))


if __name__ == '__main__': main()
