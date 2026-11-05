#!/usr/bin/env python3
"""Real native/media processes with SYNTHETIC inputs; never a hardware acceptance test."""
import argparse
import contextlib
import json
import os
import selectors
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from ss07.config import load
from ss07.media import CHANNELS, MediaObserver, get_paths, render, rtp_port
from ss07.worker import terminate


def port(kind=socket.SOCK_STREAM):
    with socket.socket(type=kind) as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def configuration():
    config = load(ROOT / 'config/system.example.json')
    config['api']['port'] = port()
    while True:
        base = port(socket.SOCK_DGRAM)
        if base > 65527:
            continue
        try:
            with contextlib.ExitStack() as stack:
                for i in range(5):
                    sock = stack.enter_context(socket.socket(type=socket.SOCK_DGRAM))
                    sock.bind(('127.0.0.1', base + 2 * i))
            break
        except OSError:
            continue
    config['media'] = dict(rtsp_port=port(), webrtc_port=port(), ice_port=port(socket.SOCK_DGRAM),
                           api_port=port(), rtp_base_port=base)
    return config


def wait_for(check, seconds=15):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            result = check()
            if result:
                return result
        except (OSError, ValueError):
            pass
        time.sleep(0.2)
    raise AssertionError('condition timed out: ' + check.__name__)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--camera-test', required=True, type=Path)
    parser.add_argument('--mediamtx', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path, help='new evidence directory')
    parser.add_argument('--browser-check', type=Path, help='optional Node CDP checker')
    parser.add_argument('--chromium', type=Path)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('output must be a new directory')
    args.output.mkdir(parents=True)
    procs, files = [], []
    config, upstream = configuration(), configuration()
    config['sdi']['rtsp_url'] = 'rtsp://127.0.0.1:%d/front' % upstream['media']['rtsp_port']
    evidence = dict(evidence_kind='synthetic_test', hardware_verified=False,
                    scope='six native H264 -> five RTP inputs + one RTSP pull -> MediaMTX',
                    started_at_utc=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))
    def start(name, argv):
        out = open(args.output / (name + '.stdout'), 'wb')
        err = open(args.output / (name + '.stderr'), 'wb')
        files.extend((out, err))
        proc = subprocess.Popen([str(a) for a in argv], stdout=out, stderr=err, start_new_session=True)
        procs.append(proc)
        return proc
    def source(name, udp_port):
        return start(name, [args.camera_test, '--device', 'synthetic-test', '--width', '640',
                           '--height', '360', '--fps-num', '25', '--fps-den', '1', '--port', str(udp_port)])
    try:
        with tempfile.TemporaryDirectory(prefix='ss07-integration-') as tmp:
            up = render(upstream)
            up['paths'] = {'front': up['paths']['front']}
            for name, data in [('upstream', up), ('main', render(config))]:
                path = Path(tmp) / (name + '.json')
                path.write_text(json.dumps(data))
                start('media-' + name, [args.mediamtx, path])
            wait_for(lambda: get_paths(config))
            cameras = {c: source('camera-' + c, rtp_port(config, c)) for c in CHANNELS[:-1]}
            source('camera-sdi-test', rtp_port(upstream, 'front'))
            observer = MediaObserver()
            def six_ready():
                result = observer.observe(get_paths(config))
                return result if all(s['ready'] for s in result['streams'].values()) else None
            evidence['six_streams'] = wait_for(six_ready)
            # Stop one native source; advancing traffic on other channels must continue.
            stopped_left = cameras['left']
            terminate(stopped_left)
            def isolated_loss():
                current = observer.observe(get_paths(config))['streams']
                return not current['left']['ready'] and all(current[c]['ready'] for c in CHANNELS if c != 'left')
            wait_for(isolated_loss)
            cameras['left'] = source('camera-left-restarted', rtp_port(config, 'left'))
            wait_for(six_ready)
            evidence['single_source_loss_and_recovery'] = 'passed'
            if args.browser_check:
                if not args.chromium:
                    raise ValueError('--chromium required for browser check')
                from ss07.api import Server
                from ss07.state import State
                # API still reports hardware unavailable. The checker explicitly
                # creates players labelled synthetic_test; it cannot publish snapshots.
                with Server(('127.0.0.1', config['api']['port']), State(config)) as server:
                    thread = threading.Thread(target=server.serve_forever, daemon=True)
                    thread.start()
                    try:
                        subprocess.run(['node', str(args.browser_check), str(args.chromium),
                                        'http://127.0.0.1:%d' % config['api']['port'], str(args.output)],
                                       check=True, timeout=60)
                        evidence['browser'] = json.loads((args.output / 'browser.json').read_text())
                    finally:
                        server.shutdown(); thread.join(timeout=5)
            for name in ('front', 'back', 'right', 'down', 'sdi-test', 'left-restarted'):
                lines = (args.output / ('camera-' + name + '.stdout')).read_text().splitlines()
                reports = [json.loads(line) for line in lines]
                assert reports and all(r['evidence_kind'] == 'synthetic_test' for r in reports)
                assert int(reports[-1]['frames']) > 0
            assert all(p.poll() is None for p in procs if p is not stopped_left)
            evidence['result'] = 'passed'
    finally:
        for proc in reversed(procs):
            terminate(proc)
        for stream in files:
            stream.close()
        (args.output / 'result.json').write_text(json.dumps(evidence, indent=2) + '\n')
    print(json.dumps(evidence, indent=2))


if __name__ == '__main__':
    main()
