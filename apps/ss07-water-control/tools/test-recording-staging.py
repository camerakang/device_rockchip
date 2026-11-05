#!/usr/bin/env python3
"""Run an isolated staged app: disabled/missing-volume recorder and read-only API."""
import argparse
import json
import os
import signal
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.request import urlopen


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rootfs', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--timecode', action='store_true', help='also verify the unconfigured receive-only service')
    args = parser.parse_args()
    if args.output.exists():
        parser.error('output must not exist')
    root = args.rootfs.absolute()
    app = root / 'opt/ss07/current'
    command = str(app / 'tools/ss07')
    report = dict(hardware_verified=False, evidence_kind='software_process_test')
    with tempfile.TemporaryDirectory(prefix='ss07-stage-record-') as temporary:
        tmp = Path(temporary)
        for enabled in (False, True):
            case = tmp / str(enabled); case.mkdir()
            config = json.loads((root / 'etc/ss07/system.json').read_text())
            config['storage'].update(recording_enabled=enabled, filesystem_uuid='unconnected-test-volume' if enabled else None)
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0)); config['api']['port'] = sock.getsockname()[1]
            path = case / 'config.json'; path.write_text(json.dumps(config))
            state, catalog = case / 'state', case / 'catalog.sqlite'
            journal = case / 'timecode.sqlite'
            processes = []
            def start(extra):
                process = subprocess.Popen([command] + extra, cwd=temporary,
                    env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                processes.append(process); return process
            def wait(check):
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    if any(p.poll() is not None for p in processes):
                        raise AssertionError('staged process exited')
                    try:
                        result = check()
                        if result: return result
                    except (OSError, ValueError):
                        pass
                    time.sleep(.05)
                raise AssertionError('staged state timeout')
            try:
                worker = start(['recording-worker', '--config', str(path), '--state-dir', str(state),
                                '--catalog', str(catalog), '--binary', str(app / 'bin/mediamtx')])
                expected = 'ERROR' if enabled else 'UNAVAILABLE'
                def settled():
                    value = json.loads((state / 'storage.json').read_text())
                    return value if value['state'] == expected else None
                snapshot = wait(settled)
                assert snapshot['payload']['recording_enabled'] is False
                assert snapshot['payload']['recording_active'] is False
                if args.timecode:
                    receiver = start(['timecode-worker', '--config', str(path), '--state-dir', str(state),
                                      '--timecode-journal', str(journal)])
                api = start(['serve', '--config', str(path), '--state-dir', str(state), '--catalog', str(catalog)] +
                            (['--timecode-journal', str(journal)] if args.timecode else []))
                def get(route):
                    with urlopen('http://127.0.0.1:%d%s' % (config['api']['port'], route), timeout=2) as response:
                        return json.load(response)['data']
                status = wait(lambda: get('/api/v1/status'))
                assert not status['storage']['recording_enabled'] and not status['storage']['recording_active']
                assert get('/api/v1/recordings')['items'] == []
                assert not get('/healthz')['hardware_ready']
                if args.timecode:
                    def timecode_ready():
                        value = get('/api/v1/timecode')['timecode']
                        return value if value['reason'] == 'timecode_not_configured' else None
                    observed = wait(timecode_ready)
                    assert observed['quality'] == 'UNAVAILABLE' and not observed['source_valid']
                    assert not observed['serial_connected'] and observed['software_tx_bytes'] == '0'
                    wait(lambda: get('/api/v1/timecode/history')['items'])
                    receiver.send_signal(signal.SIGTERM)
                    _, error = receiver.communicate(timeout=5); assert receiver.returncode == 0, error
                    assert json.loads((state / 'timecode.json').read_text())['state'] == 'STOPPED'
                    assert not (state / 'timecode.sock').exists()
                    report['unconfigured_timecode'] = dict(result='passed', serial_opened=False,
                        quality='UNAVAILABLE', history_api='passed', clean_stop=True)
                worker.send_signal(signal.SIGTERM)
                _, error = worker.communicate(timeout=5); assert worker.returncode == 0, error
                assert json.loads((state / 'storage.json').read_text())['state'] == 'STOPPED'
                api.send_signal(signal.SIGTERM)
                _, error = api.communicate(timeout=5); assert api.returncode == 0, error
                report['missing_volume' if enabled else 'disabled'] = dict(result='passed', reason=snapshot['payload']['reason'],
                    recording_started=False, clean_stop=True)
            finally:
                for process in reversed(processes):
                    if process.poll() is None: process.terminate()
                    try: process.communicate(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill(); process.communicate(timeout=2)
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
