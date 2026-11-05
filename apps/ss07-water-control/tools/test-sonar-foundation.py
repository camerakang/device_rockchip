#!/usr/bin/env python3
"""Run the real unconfigured sonar/API CLIs locally, with no peripheral driver."""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
# Test harness only: audit Python device-open attempts across worker startup,
# execution and shutdown. This is not an electrical RS485 zero-TX measurement.
AUDITED = '''import json, os, runpy, sys
audit_path = sys.argv[1]
sys.argv = ['ss07'] + sys.argv[2:]
attempts = []
def audit(event, args):
    if event == 'open' and isinstance(args[0], (str, bytes)):
        path = os.fsdecode(args[0])
        if path.startswith(('/dev/tty', '/dev/serial/', '/dev/video')):
            attempts.append(path)
sys.addaudithook(audit)
try:
    runpy.run_module('ss07', run_name='__main__')
finally:
    with open(audit_path, 'w') as stream:
        json.dump({'python_device_open_attempts': attempts}, stream)
'''


def fetch(base, path):
    with urlopen(base+path, timeout=2) as response: return json.load(response)['data']


def stop(proc):
    if proc.poll() is None:
        proc.terminate()
        try: proc.wait(timeout=5)
        except subprocess.TimeoutExpired: proc.kill(); proc.wait(timeout=3)


def run_case(directory, protocol):
    directory.mkdir()
    config = json.loads((ROOT/'config/system.example.json').read_text())
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0)); config['api']['port'] = sock.getsockname()[1]
    config['sonar']['vendor_protocol'] = protocol
    path = directory/'config.json'; path.write_text(json.dumps(config))
    state, journal = directory/'state', directory/'history.sqlite'
    common = ['--config', str(path), '--state-dir', str(state), '--sonar-journal', str(journal)]
    env = dict(os.environ, PYTHONPATH=str(ROOT/'src'), PYTHONDONTWRITEBYTECODE='1')
    audit = directory/'device-open-audit.json'
    processes = []
    with (directory/'worker.log').open('w') as worker_log, (directory/'api.log').open('w') as api_log:
        try:
            worker = subprocess.Popen([sys.executable, '-c', AUDITED, str(audit), 'sonar-worker']+common,
                                      env=env, stdout=worker_log, stderr=subprocess.STDOUT)
            processes.append(worker)
            api = subprocess.Popen([sys.executable, '-m', 'ss07', 'serve']+common,
                                   env=env, stdout=api_log, stderr=subprocess.STDOUT)
            processes.append(api)
            base = 'http://127.0.0.1:'+str(config['api']['port'])
            deadline = time.monotonic()+10
            while True:
                if any(p.poll() is not None for p in processes): raise RuntimeError('service_exited_early')
                try:
                    status = fetch(base, '/api/v1/status')
                    history = fetch(base, '/api/v1/sonars/history')
                    if status['workers']['sonar']['state'] == 'UNAVAILABLE' and history['items']: break
                except (URLError, OSError): pass
                if time.monotonic() >= deadline: raise RuntimeError('service_start_timeout')
                time.sleep(.05)
            expected = 'sonar_not_configured' if protocol is None else 'sonar_protocol_unavailable'
            assert status['sonar_service']['reason'] == expected
            assert len(status['sonars']) == 5
            assert all(s['distance_mm'] is None and not s['valid'] and s['seq'] is None for s in status['sonars'])
            assert all(s['kind'] == 'COLLECTOR_STARTED' for s in history['items'])
            assert history['complete_history'] is False and history['data_disk_archive'] is False
            (directory/'status.json').write_text(json.dumps(status, indent=2)+'\n')
            (directory/'history-running.json').write_text(json.dumps(history, indent=2)+'\n')
            stop(worker)
            assert worker.returncode == 0
            stopped = json.loads((state/'sonar.json').read_text())
            assert stopped['state'] == 'STOPPED'
            history = fetch(base, '/api/v1/sonars/history')
            assert [s['kind'] for s in history['items']] == ['COLLECTOR_STOPPED', 'COLLECTOR_STARTED']
            (directory/'history-stopped.json').write_text(json.dumps(history, indent=2)+'\n')
            stop(api); assert api.returncode == 0
            observed = json.loads(audit.read_text())
            assert observed['python_device_open_attempts'] == []
            return dict(reason=expected, invalid_directions=5, worker_returncode=worker.returncode,
                        api_returncode=api.returncode, python_device_open_attempts=0,
                        completed_lifecycle=True, measurement_events=0)
        finally:
            for proc in reversed(processes): stop(proc)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    output = args.output.absolute()
    if output.exists() or output.is_symlink(): parser.error('output must not exist')
    output.mkdir(parents=True)
    report = dict(evidence_kind='local_service_observation_no_peripherals',
                  unconfigured=run_case(output/'unconfigured', None),
                  unknown_protocol=run_case(output/'unknown-protocol', 'unregistered-test-name'),
                  hardware_validated=False, electrical_zero_tx_validated=False, data_disk_archive=False,
                  deployed=False, release_package_built=False)
    (output/'result.json').write_text(json.dumps(report, indent=2)+'\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__': main()
