#!/usr/bin/env python3
"""Real native processes/HTTPS/UI apply and rollback with explicitly synthetic media/storage."""
import argparse
import base64
import hashlib
import importlib.util
import json
import os
import pwd
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from ss07 import __version__
from ss07.accounts import password_record
from ss07.audit import Audit
from ss07.application_worker import run as application_run
from ss07.catalog import Catalog
from ss07.config import load, revision
from ss07.config_runtime import ORDER
from ss07.media import CHANNELS, MediaObserver, get_paths, render, rtp_port
from ss07.persistence import atomic_json
from ss07.snapshots import SnapshotReader
from ss07.timecode_ipc import Client as TimecodeClient
from ss07.volume import DirectoryVolume, open_directory
from ss07.worker import terminate

spec = importlib.util.spec_from_file_location('media_fixture', ROOT / 'tools/test-media.py')
fixture = importlib.util.module_from_spec(spec); spec.loader.exec_module(fixture)


def child(context_path, service):
    context = json.loads(Path(context_path).read_text())
    root, out = Path(context['runtime']), Path(context['output'])
    user = pwd.getpwuid(os.geteuid()).pw_name
    common = ['--config', str(root / 'active.json'), '--state-dir', str(root / 'state')]
    if service.startswith('camera.'):
        argv = ['camera-worker'] + common + ['--channel', service[7:], '--binary', context['camera_test']]
    elif service == 'media':
        argv = ['media-worker'] + common + ['--binary', context['mediamtx'], '--gateway-config', str(root / 'gateway.json')]
    elif service == 'storage':
        argv = ['recording-worker'] + common + ['--binary', context['mediamtx'], '--catalog', str(out / 'catalog.sqlite')]
    elif service == 'timecode':
        argv = ['timecode-worker'] + common + ['--timecode-journal', str(out / 'timecode.sqlite')]
    elif service == 'sonar':
        argv = ['sonar-worker'] + common + ['--sonar-journal', str(out / 'sonar.sqlite'),
                '--sonar-catalog', str(out / 'sonar-archives.sqlite')]
    elif service == 'maintenance':
        argv = ['maintenance-worker', '--config', str(root / 'desired.json'), '--active-config', str(root / 'active.json'),
                '--state-dir', str(root / 'state'), '--catalog', str(out / 'catalog.sqlite'), '--audit', str(out / 'maintenance.sqlite'),
                '--api-user', user, '--application-socket-dir', str(root / 'rpc'), '--application-user', user]
    elif service == 'api':
        argv = ['serve'] + common + ['--desired-config', str(root / 'desired.json'), '--catalog', str(out / 'catalog.sqlite'),
                '--timecode-journal', str(out / 'timecode.sqlite'), '--sonar-journal', str(out / 'sonar.sqlite'),
                '--sonar-catalog', str(out / 'sonar-archives.sqlite'), '--accounts', str(root / 'accounts.json'),
                '--gateway-config', str(root / 'gateway.json'), '--access-audit', str(out / 'access.sqlite'),
                '--maintenance-user', user, '--maintenance-audit', str(out / 'maintenance.sqlite'),
                '--application-audit', str(out / 'application/audit.sqlite'), '--media-cleanup-state', str(root / 'cleanup.json')]
    elif service == 'gateway':
        argv = ['gateway-worker'] + common + ['--gateway-config', str(root / 'gateway.json'), '--binary', context['caddy']]
    else: raise ValueError('unknown test consumer')
    def volume(*args, **kwargs):
        return DirectoryVolume(open_directory(out / 'recordings'), context['volume_uuid'], 'synthetic_test')
    def timecode_client(directory, boot):
        return TimecodeClient(directory, boot, 'synthetic_test')
    # Only this test launcher substitutes a directory for a data disk. Production
    # binaries, CLI and configuration contain no switch disabling MountLease.
    with patch('ss07.volume.MountLease.acquire', side_effect=volume), \
            patch('ss07.recorder.TimecodeClient', side_effect=timecode_client):
        from ss07.__main__ import main
        sys.argv = ['ss07'] + argv
        return main()


class ProcessBackend:
    """Test adapter for lifecycle only; observations come from real worker processes."""
    def __init__(self, context, runtime, output):
        self.context, self.runtime, self.output = context, runtime, output
        self.processes, self.logs, self.history = {}, [], []
        self.reader = SnapshotReader(runtime / 'state', Path('/proc/sys/kernel/random/boot_id').read_text().strip())

    def action(self, action, service):
        if service not in ORDER or action not in ('stop', 'start'): raise ValueError('invalid test lifecycle action')
        self.history.append({'action': action, 'service': service, 'monotonic_us': str(time.monotonic_ns() // 1000)})
        if action == 'stop':
            proc = self.processes.pop(service, None)
            if proc: terminate(proc)
            return
        if service in self.processes: raise ValueError('test service was not stopped')
        log = (self.output / ('consumer-' + service + '-' + str(len(self.history)) + '.log')).open('wb')
        self.logs.append(log)
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', PYTHONPATH=str(ROOT / 'src'),
                   XDG_DATA_HOME=str(self.runtime / 'caddy-data'), XDG_CONFIG_HOME=str(self.runtime / 'caddy-config'))
        self.processes[service] = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--child', str(self.context), service],
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True, env=env)

    def observe(self, service): return self.reader.read(service)

    def identity(self, service):
        proc = self.processes[service]
        if proc.poll() is not None: raise AssertionError('consumer exited: ' + service)
        children = Path('/proc/%d/task/%d/children' % (proc.pid, proc.pid)).read_text().split()
        return {'pid': proc.pid, 'children': children}

    def close(self):
        for name in reversed(ORDER): self.action('stop', name)
        for log in self.logs: log.close()


def main():
    if len(sys.argv) == 4 and sys.argv[1] == '--child': return child(sys.argv[2], sys.argv[3])
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('camera-test', 'mediamtx', 'caddy', 'chromium', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists(): parser.error('output must be new')
    out = args.output.absolute(); out.mkdir(parents=True)
    (out / 'recordings').mkdir()
    catalog = Catalog(out / 'catalog.sqlite'); catalog.initialize()
    evidence = {'evidence_kind': 'synthetic_test', 'app_version': __version__, 'hardware_validated': False,
                'observed_at_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                'deployed': False, 'result': 'failed', 'lifecycle_backend': 'local real subprocesses; systemd backend not exercised'}
    external, logs, backend, coordinator = [], [], None, None
    stop = threading.Event()
    def start(name, argv, env=None):
        log = (out / (name + '.log')).open('wb'); logs.append(log)
        proc = subprocess.Popen(list(map(str, argv)), stdout=log, stderr=subprocess.STDOUT, start_new_session=True, env=env)
        external.append(proc); return proc
    def counts():
        with catalog.connection() as db:
            return Counter({row[0]: row[1] for row in db.execute(
                "SELECT channel_id,COUNT(*) FROM recordings WHERE state='COMPLETE' GROUP BY channel_id")})
    temporary = tempfile.TemporaryDirectory(prefix='ss07-apply-integration-')
    try:
        with socket.socket() as occupied:
            runtime = Path(temporary.name); (runtime / 'rpc').mkdir(); (runtime / 'state').mkdir()
            occupied.bind(('127.0.0.1', 0)); occupied.listen(8)
            config, upstream = fixture.configuration(), fixture.configuration()
            config['sdi']['rtsp_url'] = 'rtsp://127.0.0.1:%d/front' % upstream['media']['rtsp_port']
            config['storage'].update(recording_enabled=True, filesystem_uuid='application-integration', segment_seconds=2, reserve_bytes='536870912')
            for name in ('desired.json', 'active.json'): atomic_json(runtime / name, config, mode=0o640)
            subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', str(runtime / 'key.pem'),
                '-out', str(runtime / 'cert.pem'), '-days', '1', '-subj', '/CN=127.0.0.1', '-addext', 'subjectAltName=IP:127.0.0.1'],
                check=True, capture_output=True)
            pub = subprocess.check_output(['openssl', 'x509', '-in', str(runtime / 'cert.pem'), '-pubkey', '-noout'])
            der = subprocess.run(['openssl', 'pkey', '-pubin', '-outform', 'DER'], input=pub, capture_output=True, check=True).stdout
            gateway = dict(schema_version=1, public_origin='https://127.0.0.1:%d' % fixture.port(), listen_ip='127.0.0.1',
                           ice_ip='127.0.0.1', certificate=str(runtime / 'cert.pem'), private_key=str(runtime / 'key.pem'))
            atomic_json(runtime / 'gateway.json', gateway)
            password = secrets.token_urlsafe(24)
            atomic_json(runtime / 'accounts.json', dict(schema_version=1, users={'admin': dict(role='maintainer', enabled=True,
                        password=password_record(password))}), mode=0o640)
            context = dict(runtime=str(runtime), output=str(out), volume_uuid=config['storage']['filesystem_uuid'],
                           camera_test=str(args.camera_test.resolve()), mediamtx=str(args.mediamtx.resolve()), caddy=str(args.caddy.resolve()))
            atomic_json(runtime / 'context.json', context)
            backend = ProcessBackend(runtime / 'context.json', runtime, out)
            with patch('ss07.application_worker.SystemdBackend', return_value=backend):
                coordinator = threading.Thread(target=application_run, args=(runtime / 'desired.json', runtime / 'active.json',
                    out / 'application', runtime / 'state', runtime / 'rpc', gateway, os.geteuid(), stop), daemon=True)
                coordinator.start()
                fixture.wait_for(lambda: (runtime / 'rpc/application.sock').exists())
                up = render(upstream); up['paths'] = {'front': up['paths']['front']}
                atomic_json(runtime / 'upstream.json', up)
                start('upstream', [args.mediamtx, runtime / 'upstream.json'])
                for service in ORDER: backend.action('start', service)
                fixture.wait_for(lambda: get_paths(config))
                for channel in CHANNELS:
                    port = rtp_port(upstream, 'front') if channel == 'sdi' else rtp_port(config, channel)
                    start('source-' + channel, [args.camera_test, '--device', 'synthetic-test', '--width', '640', '--height', '360',
                        '--fps-num', '25', '--fps-den', '1', '--port', port])
                observer = MediaObserver()
                fixture.wait_for(lambda: all(v['ready'] for v in observer.observe(get_paths(config))['streams'].values()))
                fixture.wait_for(lambda: all(counts()[c] >= 2 for c in CHANNELS), seconds=25)
                baseline = {name: backend.identity(name) for name in ('media', 'storage', 'timecode')}
                initial_counts = counts()
                exchange = runtime / 'exchange'; exchange.mkdir()
                atomic_json(exchange / 'settings.json', {'occupied_port': occupied.getsockname()[1]})
                browser = start('browser', ['node', ROOT / 'tools/test-browser-configuration.cjs', args.chromium,
                    gateway['public_origin'], out, exchange], env=dict(os.environ, SS07_TEST_PASSWORD=password,
                    SS07_TEST_SPKI=base64.b64encode(hashlib.sha256(der).digest()).decode()))
                handled = set(); deadline = time.monotonic() + 150
                while browser.poll() is None and time.monotonic() < deadline:
                    for phase, expected in (('apply', 'APPLIED'), ('rollback', 'ROLLED_BACK')):
                        request = exchange / (phase + '.request')
                        if phase in handled or not request.exists(): continue
                        target = json.loads(request.read_text())['revision']
                        value = json.loads((out / 'application/transaction.json').read_text()) if (out / 'application/transaction.json').exists() else {}
                        if value.get('target_revision') != target or value.get('phase') not in ('APPLIED', 'ROLLED_BACK', 'ROLLBACK_FAILED'): continue
                        assert value['phase'] == expected, value
                        events = Audit(out / 'maintenance.sqlite').history()['items']
                        assert any(item.get('operation_id') == value['operation_id'] and item.get('outcome') == 'config.apply'
                                   for item in events), 'application audit identity lost across RPC'
                        for service, identity in baseline.items(): assert backend.identity(service) == identity, service
                        before = counts(); fixture.wait_for(lambda: all(counts()[c] > before[c] for c in CHANNELS), seconds=8)
                        evidence[phase] = {'transaction': value, 'native_media_and_recorder_identity_unchanged': True,
                                           'six_recorders_continue': True, 'counts': dict(counts())}
                        atomic_json(exchange / (phase + '.response'), {'verified': True}); handled.add(phase)
                    time.sleep(.1)
                if browser.poll() is None: raise AssertionError('configuration browser test timed out')
                assert browser.returncode == 0, (out / 'browser.log').read_text()
                assert handled == {'apply', 'rollback'}
                assert all(counts()[c] > initial_counts[c] for c in CHANNELS)
                evidence['browser'] = json.loads((out / 'browser-configuration.json').read_text())
                evidence['result'] = 'passed'
    finally:
        stop.set()
        if coordinator: coordinator.join(3)
        if backend:
            evidence['lifecycle_actions'] = list(backend.history)
            evidence['consumer_status'] = {name: backend.observe(name) for name in ORDER}
            backend.close()
        for proc in reversed(external): terminate(proc)
        for log in logs: log.close()
        temporary.cleanup()
        (out / 'result.json').write_text(json.dumps(evidence, indent=2) + '\n')
    print(json.dumps({k: v for k, v in evidence.items() if k not in ('lifecycle_actions', 'browser', 'consumer_status')}, indent=2))
    return 0


if __name__ == '__main__': sys.exit(main())
