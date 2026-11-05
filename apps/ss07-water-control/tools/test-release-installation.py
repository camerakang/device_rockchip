#!/usr/bin/env python3
"""Cross-version real-process installation tests; synthetic media and isolated storage only."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import pwd
import secrets
import ssl
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter
from http.cookiejar import CookieJar
from unittest.mock import patch
from urllib.request import Request, build_opener, HTTPSHandler, HTTPCookieProcessor

ROOT = Path(__file__).resolve().parents[1]
CHILD = len(sys.argv) == 4 and sys.argv[1] == '--child'
if CHILD:
    CONTEXT = json.loads(Path(sys.argv[2]).read_text())
    RELEASE = (Path(CONTEXT['root']) / 'opt/ss07/current').resolve()
else: RELEASE = ROOT
sys.path.insert(0, str(RELEASE / 'src'))
sys.dont_write_bytecode = True
from ss07 import __version__
from ss07.accounts import password_record
from ss07.catalog import Catalog
from ss07.config import load
from ss07.config_runtime import ORDER, UNITS, acknowledgement, fingerprint
from ss07.media import CHANNELS, MediaObserver, get_paths, render, rtp_port
from ss07.persistence import atomic_json
from ss07.snapshots import SnapshotReader
from ss07.timecode_ipc import Client as TimecodeClient
from ss07.volume import DirectoryVolume, open_directory
from ss07.worker import terminate


def child(service):
    context = CONTEXT; root = Path(context['root']); state = root / 'run/ss07'
    active, desired = root / 'etc/ss07-active/system.json', root / 'etc/ss07/system.json'
    data, gateway = root / 'var/lib/ss07', root / 'etc/ss07-gateway/network.json'
    catalog, journal = data / 'catalog.sqlite', data / 'timecode/journal.sqlite'
    user = pwd.getpwuid(os.geteuid()).pw_name
    common = ['--config', str(active), '--state-dir', str(state)]
    if service == 'api' and os.environ.get('SS07_TEST_FAIL_API_VERSION') == __version__: return 42
    if service.startswith('camera.'):
        argv = ['camera-worker'] + common + ['--channel', service[7:], '--binary', context['camera_test']]
    elif service == 'media': argv = ['media-worker'] + common + ['--binary', context['mediamtx'], '--gateway-config', str(gateway)]
    elif service == 'storage': argv = ['recording-worker'] + common + ['--binary', context['mediamtx'], '--catalog', str(catalog)]
    elif service == 'timecode': argv = ['timecode-worker'] + common + ['--timecode-journal', str(journal)]
    elif service == 'sonar':
        argv = ['sonar-worker'] + common + ['--sonar-journal', str(data / 'sonar/samples.sqlite'),
                '--sonar-catalog', str(data / 'sonar/archives.sqlite')]
    elif service == 'maintenance':
        argv = ['maintenance-worker', '--config', str(desired), '--active-config', str(active), '--state-dir', str(state),
                '--catalog', str(catalog), '--audit', str(data / 'maintenance.sqlite'), '--api-user', user,
                '--application-socket-dir', str(root / 'run/ss07-apply'), '--application-user', user]
    elif service == 'configuration':
        argv = ['application-worker', '--desired-config', str(desired), '--active-config', str(active),
                '--journal-dir', str(root / 'var/lib/ss07-apply'), '--state-dir', str(state),
                '--socket-dir', str(root / 'run/ss07-apply'), '--gateway-config', str(gateway), '--maintenance-user', user]
    elif service == 'api':
        argv = ['serve'] + common + ['--desired-config', str(desired), '--catalog', str(catalog), '--timecode-journal', str(journal),
                '--accounts', str(root / 'etc/ss07-credentials/accounts.json'), '--gateway-config', str(gateway),
                '--access-audit', str(root / 'var/lib/ss07-api/access.sqlite'), '--maintenance-user', user,
                '--maintenance-audit', str(data / 'maintenance.sqlite'), '--application-audit', str(root / 'var/lib/ss07-apply/audit.sqlite'),
                '--media-cleanup-state', str(root / 'run/ss07-api/media.json')]
    elif service == 'gateway': argv = ['gateway-worker'] + common + ['--gateway-config', str(gateway), '--binary', context['caddy']]
    else: raise ValueError('unknown test service')
    if tuple(map(int, __version__.split('.'))) >= (0, 9, 0):
        if service in ('media', 'configuration'): argv += ['--rtsp-config', str(root / 'etc/ss07-gateway/rtsp.json')]
        if service == 'media': argv += ['--accounts', str(root / 'etc/ss07-credentials/accounts.json')]
        if service in ('media', 'api'): argv += ['--rtsp-audit', str(root / 'var/lib/ss07-media/access.sqlite')]
    if tuple(map(int, __version__.split('.'))) >= (0, 10, 0) and service == 'api':
        argv += ['--sonar-journal', str(data / 'sonar/samples.sqlite'),
                 '--sonar-catalog', str(data / 'sonar/archives.sqlite')]
    def volume(*args, **kwargs):
        return DirectoryVolume(open_directory(data / 'recordings'), context['volume_uuid'], 'synthetic_test')
    def timecode_client(directory, boot): return TimecodeClient(directory, boot, 'synthetic_test')
    # These adapters exist only in this test launcher. Production has no
    # directory-as-disk switch and this child cannot call the host systemctl.
    with patch('ss07.volume.MountLease.acquire', side_effect=volume), \
            patch('ss07.recorder.TimecodeClient', side_effect=timecode_client), \
            patch('ss07.application.SystemdBackend.action', side_effect=RuntimeError('no host lifecycle in a test child')):
        from ss07.__main__ import main
        sys.argv = ['ss07'] + argv
        return main()


def main():
    if CHILD: return child(sys.argv[3])
    from ss07.release_install import InstallError, Installer, SystemBackend, STOP_ORDER
    from ss07.release_package import digest
    from ss07.release_contract import runtime_units
    from ss07.release_restore import RUN_UNITS, boot, load as load_private, save, transaction, write_file
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('old-package', 'new-package', 'camera-test', 'mediamtx', 'caddy', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    for name in ('old-sha256', 'new-sha256'): parser.add_argument('--' + name, required=True)
    parser.add_argument('--exercise-sonar-migration', action='store_true', help='verify additive sonar data/config retention across legacy downgrade')
    args = parser.parse_args()
    out = args.output.absolute()
    if out.exists(): parser.error('output must be new')
    out.mkdir(parents=True)
    # AF_UNIX sockaddr paths are short; evidence directories can be arbitrarily
    # deep. Move the stopped runtime into the output only after the test.
    root = Path(tempfile.mkdtemp(prefix='ss07-upgrade-'))
    spec = importlib.util.spec_from_file_location('media_fixture', ROOT / 'tools/test-media.py')
    fixture = importlib.util.module_from_spec(spec); spec.loader.exec_module(fixture)
    services = dict(UNITS, configuration='ss07-application.service')

    class ProcessBackend(SystemBackend):
        def __init__(self):
            self.processes, self.history, self.logs, self.units = {}, [], [], set()
            self.fail_version, self.bootstrap_active, self.confirmations = None, False, None
            self.reader = SnapshotReader(root / 'run/ss07', Path('/proc/sys/kernel/random/boot_id').read_text().strip())
            self.gateway_path = root / 'etc/ss07-gateway/network.json'

        def command(self, action, units=()):
            self.history.append(dict(action=action, units=list(units), at_us=str(time.monotonic_ns() // 1000)))
            if action in ('daemon-reload', 'reset-failed', 'enable'): return
            if action not in ('start', 'stop'): raise ValueError('invalid test action')
            if action == 'start' and units == ['ss07-release-recover.service']:
                if not self.bootstrap_active: boot(root); self.bootstrap_active = True
                return
            for unit in units:
                service = next((name for name, value in services.items() if value == unit), None)
                if action == 'stop':
                    self.units.discard(unit)
                    proc = self.processes.pop(service, None)
                    if proc: terminate(proc)
                elif unit not in self.units:
                    self.units.add(unit)
                    if service:
                        log = (out / ('worker-' + service + '-' + str(len(self.history)) + '.log')).open('wb'); self.logs.append(log)
                        env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', SS07_TEST_FAIL_API_VERSION=self.fail_version or '',
                                   XDG_DATA_HOME=str(root / 'run/caddy-data'), XDG_CONFIG_HOME=str(root / 'run/caddy-config'))
                        self.processes[service] = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--child',
                            str(out / 'context.json'), service], stdout=log, stderr=subprocess.STDOUT, env=env, start_new_session=True)

        def running(self): return [name for name in RUN_UNITS if name in self.units]
        def prepare_users(self): pass  # No host account mutation; UID permissions are not validated here.
        def preflight(self, directory, root):
            result = subprocess.check_output([sys.executable, '-B', '-m', 'ss07', '--version'], text=True, timeout=10,
                env=dict(os.environ, PYTHONPATH=str(directory / 'src'), PYTHONDONTWRITEBYTECODE='1')).strip()
            if result != directory.name: raise ValueError('test release import mismatch')
            for name in ('etc/ss07/system.json', 'etc/ss07-active/system.json'):
                path = root / name
                if path.is_file():
                    subprocess.run([sys.executable, '-B', '-m', 'ss07', 'validate', '--config', str(path)],
                        env=dict(os.environ, PYTHONPATH=str(directory / 'src'), PYTHONDONTWRITEBYTECODE='1'),
                        check=True, timeout=10, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        def healthy(self, version, config, started, baseline):
            error = None
            try: good = super().healthy(version, config, started, baseline)
            except (OSError, ValueError, KeyError) as exc: good, error = False, type(exc).__name__
            if time.monotonic() >= getattr(self, 'next_diagnostic', 0):
                self.next_diagnostic = time.monotonic() + 1
                statuses = self.observe()
                diagnostic = {name: dict(state=value.get('state'), payload=value.get('payload'),
                    observed=value.get('observed_monotonic_us'), seq=value.get('seq'),
                    acknowledged=acknowledgement(config, name, value, started) if name in ORDER else None)
                    for name, value in statuses.items()}
                with (out / 'health.jsonl').open('a') as stream:
                    stream.write(json.dumps(dict(version=version, good=good, error=error, required_after=str(started), workers=diagnostic)) + '\n')
            return good
        def close(self):
            self.command('stop', STOP_ORDER)
            for log in self.logs: log.close()

    backend = ProcessBackend(); external, logs = [], []
    report = dict(evidence_kind='synthetic_test', result='failed', app_version=__version__, hardware_verified=False, deployed=False,
                  lifecycle_backend='real local subprocesses; systemd/UID permissions and physical power loss are not exercised',
                  packages={'old': args.old_sha256, 'new': args.new_sha256}, phases={})
    def start(name, argv):
        log = (out / (name + '.log')).open('wb'); logs.append(log)
        proc = subprocess.Popen(list(map(str, argv)), stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        external.append(proc); return proc
    def manager(): return Installer(root, backend, timeout=15, settle=2)
    try:
        with manager() as install: report['phases']['initial_install'] = install.install(args.old_package, args.old_sha256)
        old_version = report['phases']['initial_install']['version']
        config, upstream = fixture.configuration(), fixture.configuration()
        config['sdi']['rtsp_url'] = 'rtsp://127.0.0.1:%d/front' % upstream['media']['rtsp_port']
        config['storage'].update(recording_enabled=True, filesystem_uuid='installation-synthetic', segment_seconds=2, reserve_bytes='536870912')
        for name in ('etc/ss07/system.json', 'etc/ss07-active/system.json'): write_file(root / name, json.dumps(config).encode(), 0o644)
        data = root / 'var/lib/ss07'; (data / 'recordings').mkdir(parents=True)
        for name in ('run/ss07', 'run/ss07-api', 'run/ss07-apply', 'etc/ss07-tls', 'var/lib/ss07-api'):
            (root / name).mkdir(parents=True, exist_ok=True)
        cert, key = root / 'etc/ss07-tls/server.crt', root / 'etc/ss07-tls/server.key'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1', '-subj', '/CN=127.0.0.1',
                        '-addext', 'subjectAltName=IP:127.0.0.1', '-out', str(cert), '-keyout', str(key)], check=True, capture_output=True)
        gateway = dict(schema_version=1, public_origin='https://127.0.0.1:%d' % fixture.port(), listen_ip='127.0.0.1', ice_ip='127.0.0.1',
                       certificate=str(cert), private_key=str(key))
        write_file(root / 'etc/ss07-gateway/network.json', json.dumps(gateway).encode(), 0o644)
        rtsp = dict(schema_version=1, enabled=True, port=fixture.port())
        write_file(root / 'etc/ss07-gateway/rtsp.json', json.dumps(rtsp).encode(), 0o644)
        password = secrets.token_urlsafe(24)
        write_file(root / 'etc/ss07-credentials/accounts.json', json.dumps(dict(schema_version=1, users={'admin': dict(
                   role='maintainer', enabled=True, password=password_record(password))})).encode(), 0o640)
        atomic_json(out / 'context.json', dict(root=str(root), volume_uuid=config['storage']['filesystem_uuid'],
                    camera_test=str(args.camera_test.resolve()), mediamtx=str(args.mediamtx.resolve()), caddy=str(args.caddy.resolve())))
        write_file(root / 'etc/ss07-provisioned', b'synthetic_test_only\n', 0o644)
        baseline_files = {name: digest(root / name) for name in ('etc/ss07/system.json', 'etc/ss07-active/system.json',
                          'etc/ss07-credentials/accounts.json', 'etc/ss07-gateway/network.json', 'etc/ss07-gateway/rtsp.json', 'etc/ss07-tls/server.key')}
        up = render(upstream); up['paths'] = {'front': up['paths']['front']}
        atomic_json(out / 'upstream.json', up); start('upstream', [args.mediamtx, out / 'upstream.json'])
        backend.command('start', runtime_units(load_private(root / 'var/lib/ss07-deploy/installed.json')['data']))
        for channel in CHANNELS:
            port = rtp_port(upstream, 'front') if channel == 'sdi' else rtp_port(config, channel)
            start('source-' + channel, [args.camera_test, '--device', 'synthetic-test', '--width', '640', '--height', '360',
                                       '--fps-num', '25', '--fps-den', '1', '--port', port])
        catalog = Catalog(data / 'catalog.sqlite')
        def counts():
            with catalog.connection() as db:
                return Counter({row[0]: row[1] for row in db.execute("SELECT channel_id,COUNT(*) FROM recordings WHERE state='COMPLETE' GROUP BY channel_id")})
        observer = MediaObserver()
        retained = {}; retained_sonar_events = {}
        def verify(version, before=None):
            fixture.wait_for(lambda: all(v['ready'] for v in observer.observe(get_paths(config))['streams'].values()))
            fixture.wait_for(lambda: all(counts()[c] >= (before[c] + 2 if before else 2) for c in CHANNELS), seconds=30)
            client = build_opener(HTTPSHandler(context=ssl.create_default_context(cafile=str(cert))), HTTPCookieProcessor(CookieJar()))
            def request(path, value=None):
                return json.load(client.open(Request(gateway['public_origin'] + path,
                    data=None if value is None else json.dumps(value).encode(),
                    headers={'Content-Type': 'application/json', 'Origin': gateway['public_origin']}), timeout=5))
            assert request('/healthz')['data']['app_version'] == version
            request('/api/v1/login', dict(username='admin', password=password))
            assert request('/api/v1/session')['data']['role'] == 'maintainer'
            status = request('/api/v1/status')['data']
            native_enabled = tuple(map(int, version.split('.'))) >= (0, 9, 0)
            if native_enabled: assert status['native_rtsp']['state'] == 'READY'
            else:
                import socket
                try:
                    connection = socket.create_connection(('127.0.0.1', rtsp['port']), timeout=.5)
                    connection.close(); raise AssertionError('old version retained a native RTSPS listener')
                except OSError: pass
            assert all(proc.poll() is None for proc in backend.processes.values()), 'a test worker exited'
            assert all(digest(root / name) == expected for name, expected in baseline_files.items())
            if args.exercise_sonar_migration and (data / 'sonar/samples.sqlite').is_file():
                import sqlite3
                with sqlite3.connect('file:'+str(data / 'sonar/samples.sqlite')+'?mode=ro',uri=True) as db:
                    events = dict(db.execute('SELECT id,data FROM events'))
                assert all(events.get(k)==v for k,v in retained_sonar_events.items()), 'sonar history changed or lost'
                retained_sonar_events.update(events)
            with catalog.connection() as db:
                rows = {row[0]: tuple(row[1:]) for row in db.execute(
                    "SELECT recording_id,relative_path,channel_id,start_utc,end_utc,state,locked,lock_revision,metadata FROM recordings WHERE state='COMPLETE'")}
            preserved_count = len(retained)
            for identity, (metadata, expected_digest) in retained.items():
                assert rows.get(identity) == metadata, 'existing recording metadata changed during installation'
                assert digest(data / 'recordings' / metadata[0]) == expected_digest, 'existing recording content changed'
            for identity, metadata in rows.items():
                retained[identity] = (metadata, digest(data / 'recordings' / metadata[0]))
            return dict(version=version, six_recorders_continue=True, completed_segments=dict(counts()), https_relogin=True,
                        configuration_and_credentials_preserved=True, existing_recordings_preserved=preserved_count,
                        native_rtsp_enabled=native_enabled, retained_sonar_events=len(retained_sonar_events),
                        recording_metadata_and_content_checked=True, live_worker_count=len(backend.processes))
        report['phases']['old_running'] = verify(old_version)
        before = counts()
        with manager() as install: report['phases']['upgrade_transaction'] = install.install(args.new_package, args.new_sha256)
        report['phases']['upgraded'] = verify(__version__, before)
        if args.exercise_sonar_migration:
            assert report['phases']['upgrade_transaction']['migration']=='sonar_config_v1_to_v2'
            from ss07.sonar_archive import Catalog as SonarCatalog, Store, Writer
            from ss07.sonar_core import event
            from ss07.timecode_model import unavailable
            from ss07.persistence import ConfigStore
            from ss07.config import revision
            import uuid
            archive_catalog = SonarCatalog(data / 'sonar/archives.sqlite'); archive_catalog.initialize()
            sample=event('installation-test-boot',str(uuid.uuid4()),0,'COLLECTOR_STARTED',1000,'synthetic_test')
            sample['time_tag']=unavailable(sample['rk_boot_id'],1000,sample['timestamp_basis'],'test_no_timecode','synthetic_test')
            with DirectoryVolume(open_directory(data / 'recordings'),config['storage']['filesystem_uuid'],'synthetic_test') as volume:
                writer=Writer(archive_catalog,volume);writer.append([sample]);metadata=writer.finish()
                Store(archive_catalog,volume).set_lock(metadata['archive_id'],True,'0')
                metadata=archive_catalog.get(metadata['archive_id'])
                fd,_=Store(archive_catalog,volume).open(metadata['archive_id']);os.close(fd)
            for path in (archive_catalog.path,data/'recordings'/metadata['relative_path'],data/'recordings'/(metadata['relative_path']+'.json')):
                baseline_files[str(path.relative_to(root))]=digest(path)
            # A saved pending false field is legal in 0.10.0 and unknown to 0.9.0.
            # It is transformed only by the reviewed downgrade transaction.
            desired=root/'etc/ss07/system.json';candidate=load(desired);candidate['storage']['sonar_recording_enabled']=False
            ConfigStore(desired).update(candidate,revision(load(desired)))
            candidate['storage'].pop('sonar_recording_enabled')
            baseline_files['etc/ss07/system.json']=hashlib.sha256((json.dumps(candidate,ensure_ascii=False,indent=2)+'\n').encode()).hexdigest()
            report['sonar_fixture']=dict(evidence_kind='synthetic_test',archive_id=metadata['archive_id'],
                archive_sha256=metadata['sha256'],locked=metadata['locked'],measurement_events=0,
                pending_false_field_prepared=True)
        before = counts()
        with manager() as install: report['phases']['manual_rollback_transaction'] = install.rollback(__version__)
        if args.exercise_sonar_migration:
            assert report['phases']['manual_rollback_transaction']['migration']=='sonar_config_v2_to_v1'
            assert 'sonar_recording_enabled' not in load(root/'etc/ss07/system.json')['storage']
            assert not (root/'usr/lib/systemd/system/ss07-sonar.service').exists()
        report['phases']['manual_rollback'] = verify(old_version, before)
        before = counts(); backend.fail_version = __version__
        with manager() as install:
            try: install.install(args.new_package, args.new_sha256)
            except InstallError as exc: assert str(exc) == 'release_health_timeout'
            else: raise AssertionError('failed API unexpectedly accepted')
        assert transaction(root)['phase'] == 'ROLLED_BACK'
        report['phases']['failed_upgrade_automatic_recovery'] = verify(old_version, before)
        backend.fail_version = None
        with manager() as install: install.install(args.new_package, args.new_sha256)
        before = counts(); backend.command('stop', STOP_ORDER)
        value = transaction(root); value['phase'] = 'VERIFYING'; save(root / 'var/lib/ss07-deploy/transaction.json', value)
        boot(root)
        assert transaction(root)['phase'] == 'RECOVERY_PENDING'
        with manager() as install: install.recover(value['operation_id'])
        report['phases']['interrupted_journal_boot_recovery'] = verify(old_version, before)
        report['result'] = 'passed'
    finally:
        report['lifecycle_actions'] = list(backend.history)
        backend.close()
        for proc in reversed(external): terminate(proc)
        for log in logs: log.close()
        shutil.move(str(root), str(out / 'isolated-root'))
        (out / 'result.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({key: value for key, value in report.items() if key != 'lifecycle_actions'}, indent=2))
    return 0


if __name__ == '__main__': raise SystemExit(main())
