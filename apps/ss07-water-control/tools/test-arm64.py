#!/usr/bin/env python3
"""ARM64 loader/plugin/RTP smoke test under QEMU. Synthetic evidence, no hardware or deployment."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import socket
import struct
import subprocess
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sysroot', type=Path, required=True)
    parser.add_argument('--native-dir', type=Path, required=True)
    parser.add_argument('--media-dir', type=Path, required=True)
    parser.add_argument('--gateway-dir', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--qemu-go-madvfree', action='store_true',
                        help='test-only GODEBUG=madvdontneed=0 workaround for old QEMU (golang/go#77572)')
    args = parser.parse_args()
    root, dest = args.sysroot.resolve(), args.output.absolute()
    if dest.exists() or dest.is_symlink():
        parser.error('output must not exist')
    dest.mkdir(parents=True)
    plugins = dest / 'plugins'
    plugins.mkdir()
    for name in ('coreelements', 'video4linux2', 'videoparsersbad', 'rtp', 'udp', 'videotestsrc', 'x264'):
        source = root / ('usr/lib/aarch64-linux-gnu/gstreamer-1.0/libgst' + name + '.so')
        if not source.is_file():
            raise ValueError('missing plugin: ' + name)
        (plugins / source.name).symlink_to(source)
    guest = {'LD_LIBRARY_PATH': ':'.join(str(root / name) for name in ('lib/aarch64-linux-gnu', 'usr/lib/aarch64-linux-gnu')),
             'LD_BIND_NOW': '1', 'GST_PLUGIN_SYSTEM_PATH': str(plugins), 'GST_PLUGIN_PATH': '',
             'GST_REGISTRY_FORK': 'no', 'GST_REGISTRY': str(dest / 'gst-registry.bin')}
    runner = ['qemu-aarch64-static', '-L', str(root)]
    for key, value in guest.items():
        runner += ['-E', key + '=' + value]
    env = {key: value for key, value in os.environ.items() if not key.startswith(('GST_', 'LD_', 'QEMU_'))}
    env['LC_ALL'] = 'C'

    def run(binary, argv, name, expected=0, go_runtime=False):
        workaround = ['-E', 'GODEBUG=madvdontneed=0'] if go_runtime and args.qemu_go_madvfree else []
        result = subprocess.run(runner + workaround + [str(binary)] + argv, env=env, capture_output=True, text=True, timeout=30)
        (dest / (name + '.log')).write_text(result.stdout + result.stderr)
        if result.returncode != expected:
            raise AssertionError('{} exit {}: {}'.format(name, result.returncode, result.stderr[:300]))
        return result.stdout + result.stderr

    version = run(args.native_dir / 'ss07-camera', ['--version'], 'camera-version').strip()
    if 'GStreamer 1.18.5' not in version:
        raise AssertionError('unexpected compiled GStreamer version')
    run(root / 'usr/bin/gst-inspect-1.0', ['--version'], 'gstreamer-version')
    for feature in ('v4l2src', 'h264parse', 'queue', 'capsfilter', 'rtph264pay', 'udpsink', 'videotestsrc', 'x264enc'):
        run(root / 'usr/bin/gst-inspect-1.0', [feature], 'plugin-' + feature)
    media_version = run(args.media_dir / 'mediamtx', ['--version'], 'mediamtx-version', go_runtime=True).strip()
    gateway_version = run(args.gateway_dir / 'caddy', ['version'], 'caddy-version', go_runtime=True).strip()
    options = ['--width', '320', '--height', '240', '--fps-num', '5', '--fps-den', '1', '--port', '18000']
    refused = run(args.native_dir / 'ss07-camera', ['--device', 'synthetic-test'] + options, 'production-rejects-test-source', 2)
    if 'stable_device_binding_required' not in refused:
        raise AssertionError('production accepted a synthetic device')
    refused = run(args.native_dir / 'ss07-camera', ['--device', '/dev/v4l/by-path/ss07-absent-qemu-test'] + options, 'missing-device', 2)
    if 'device_open_failed' not in refused:
        raise AssertionError('missing hardware was not reported')
    packets, frames, last_sequence, gaps = 0, 0, None, 0
    au, fragment, elementary = bytearray(), bytearray(), bytearray()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as receiver:
        receiver.bind(('127.0.0.1', 0))
        receiver.settimeout(.2)
        options[-1] = str(receiver.getsockname()[1])
        proc = subprocess.Popen(runner + [str(args.native_dir / 'ss07-camera-test'), '--device', 'synthetic-test'] + options,
                                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        deadline, first = time.monotonic() + 20, None
        try:
            while time.monotonic() < deadline and (first is None or time.monotonic() - first < 4):
                if proc.poll() is not None:
                    raise AssertionError('synthetic source exited before RTP test completed')
                try:
                    packet = receiver.recv(65536)
                except socket.timeout:
                    continue
                if len(packet) < 13 or packet[0] != 0x80 or packet[1] & 0x7f != 96:
                    raise AssertionError('unexpected RTP header')
                sequence = struct.unpack('!H', packet[2:4])[0]
                if last_sequence is not None and sequence != (last_sequence + 1) % 65536:
                    gaps += 1
                last_sequence = sequence
                packets += 1
                if first is None:
                    first = time.monotonic()
                payload = packet[12:]
                kind = payload[0] & 31
                if 1 <= kind <= 23:
                    au += b'\x00\x00\x00\x01' + payload
                elif kind == 28 and len(payload) >= 3:
                    if payload[1] & 0x80:
                        fragment = bytearray(b'\x00\x00\x00\x01' + bytes([(payload[0] & 0xe0) | (payload[1] & 31)]))
                    fragment += payload[2:]
                    if payload[1] & 0x40:
                        au += fragment
                        fragment.clear()
                else:
                    raise AssertionError('unexpected packetization type: ' + str(kind))
                if packet[1] & 0x80:
                    elementary += au
                    au.clear()
                    frames += 1
                if len(elementary) > 8 * 1024 * 1024:
                    raise AssertionError('test stream exceeds bounded capture size')
        finally:
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
            try:
                stdout, stderr = proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                stdout, stderr = proc.communicate()
                raise AssertionError('native process did not stop gracefully')
            (dest / 'synthetic-telemetry.jsonl').write_text(stdout)
            (dest / 'synthetic-stderr.log').write_text(stderr)
        observations = [json.loads(line) for line in stdout.splitlines()]
        if proc.returncode != 0 or frames < 5 or gaps or not observations:
            raise AssertionError('RTP/graceful-stop validation failed')
        if any(item['evidence_kind'] != 'synthetic_test' for item in observations):
            raise AssertionError('test evidence was mislabeled')
    raw = dest / 'synthetic.h264'
    raw.write_bytes(elementary)
    # Use the host's existing test decoder. Production still forwards H.264 without decoding.
    decode = subprocess.run(['gst-launch-1.0', '-v', 'filesrc', 'location=' + str(raw), '!', 'h264parse',
        '!', 'openh264dec', '!', 'video/x-raw,width=320,height=240', '!', 'identity', 'silent=false',
        '!', 'fakesink'], capture_output=True, text=True, timeout=30)
    (dest / 'decode.log').write_text(decode.stdout + decode.stderr)
    decode.check_returncode()
    decoded_frames = len(re.findall(r'GstIdentity:[^\n]*last-message = chain', decode.stdout))
    if decoded_frames < 5 or 'Got EOS' not in decode.stdout:
        raise AssertionError('decoded stream mismatch')
    stream = {'codec_name': 'h264', 'width': 320, 'height': 240, 'decoded_frames': decoded_frames,
              'decoder': 'host GStreamer openh264dec; fixed raw caps and EOS required'}
    result = {'evidence_kind': 'arm64_qemu_synthetic', 'hardware_validated': False, 'deployed': False,
              'qemu_version': subprocess.check_output(['qemu-aarch64-static', '--version'], text=True).splitlines()[0],
              'camera_version': version, 'mediamtx_version': media_version, 'caddy_version': gateway_version,
              'go_emulator_workaround': 'GODEBUG=madvdontneed=0' if args.qemu_go_madvfree else None,
              'plugins_loaded': ['v4l2src', 'h264parse', 'queue', 'capsfilter', 'rtph264pay', 'udpsink'],
              'production_rejects_synthetic': True, 'missing_device_rejected': True,
              'rtp_packets': packets, 'rtp_sequence_gaps': gaps, 'complete_access_units': frames,
              'decoded_stream': stream, 'native_sigterm_exit': proc.returncode,
              'binary_sha256': hashlib.sha256((args.native_dir / 'ss07-camera').read_bytes()).hexdigest()}
    (dest / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
