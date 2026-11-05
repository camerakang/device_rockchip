"""Lifecycle faults use isolated roots; no host services, accounts or devices."""
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from ss07.config import load as load_config
from ss07.config_runtime import fingerprint
from ss07.release_contract import DEFAULT_FILES
from ss07.release_install import InstallError, Installer, SystemBackend
from ss07.release_package import unpack
from ss07.release_restore import boot, load, save, transaction, write_file
from test_release_package import fixture

ROOT = Path(__file__).resolve().parents[1]


class Backend:
    def __init__(self, root):
        self.root, self.units, self.history = root, [], []
        self.fail_version = self.fail_action = self.after_start = None

    def command(self, action, units=()):
        self.history.append((action, tuple(units)))
        if action == self.fail_action: raise subprocess.SubprocessError('test lifecycle fault')
        if action == 'start' and units == ['ss07-release-recover.service']:
            boot(self.root)
        elif action == 'stop': self.units = [n for n in self.units if n not in units]
        elif action == 'start':
            self.units = list(units)
            if self.after_start: self.after_start()

    def running(self): return list(self.units)
    def observe(self): return {}
    def prepare_users(self): pass
    def preflight(self, directory, root):
        if self.fail_action == 'preflight': raise InstallError('test_preflight_fault')
    def healthy(self, version, config, started, baseline): return version != self.fail_version


class InstallationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.out = Path(self.temp.name); self.root = self.out / 'root'; self.root.mkdir()
        self.backend = Backend(self.root)
        self.archives = {}
        for version in ('0.7.0', '0.8.0'):
            path = self.out / (version + '.tar.gz')
            def settings(manifest, files):
                for name in DEFAULT_FILES:
                    files[name] = (ROOT / 'config/system.example.json').read_bytes()
                    manifest['files'][name] = hashlib.sha256(files[name]).hexdigest()
            self.archives[version] = (path, fixture(path, version, modify=settings))

    def manager(self): return Installer(self.root, self.backend, timeout=.3, settle=0)

    def install(self, version):
        with self.manager() as manager: return manager.install(*self.archives[version])

    def existing(self):
        self.install('0.7.0')
        self.backend.units = ['ss07-api.service']
        write_file(self.root / 'etc/ss07-provisioned', b'provisioned\n')

    def test_initial_install_requires_separate_provisioning_and_activation(self):
        result = self.install('0.8.0')
        self.assertFalse(result['activated']); self.assertFalse(result['hardware_verified'])
        self.assertEqual(transaction(self.root)['phase'], 'COMMITTED')
        self.assertFalse((self.root / 'etc/ss07-provisioned').exists())
        self.assertEqual(self.backend.units, [])
        self.assertIn('ConditionPathExists=/etc/ss07-provisioned',
                      (self.root / 'etc/systemd/system/ss07-api.service.d/00-ss07-release.conf').read_text())
        self.assertEqual(self.install('0.8.0'), {'unchanged': True, 'version': '0.8.0'})

    def test_upgrade_and_manual_rollback_preserve_config_credentials_and_database_rows(self):
        self.existing()
        paths = {'etc/ss07/system.json': b'candidate kept for later validation',
                 'etc/ss07-credentials/accounts.json': b'credentials remain private',
                 'etc/ss07-gateway/rtsp.json': b'{"schema_version":1,"enabled":true,"port":8322}',
                 'etc/ss07-tls/server.key': b'private TLS key'}
        for name, raw in paths.items(): write_file(self.root / name, raw)
        from ss07.audit import Audit
        rtsp_audit = Audit(self.root / 'var/lib/ss07-media/access.sqlite'); rtsp_audit.initialize()
        rtsp_audit.append('RTSP_ACCESS_START', outcome='before_upgrade')
        dbpath = self.root / 'var/lib/ss07/catalog.sqlite'; dbpath.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(dbpath)) as db:
            db.execute('PRAGMA application_id=1397960759'); db.execute('PRAGMA user_version=2')
            db.execute('CREATE TABLE preserved (id INTEGER)'); db.execute('INSERT INTO preserved VALUES (1)')
        result = self.install('0.8.0')
        self.assertTrue(result['software_health_verified'])
        rtsp_audit.append('RTSP_REVOKE', outcome='after_upgrade')
        with sqlite3.connect(str(dbpath)) as db: db.execute('INSERT INTO preserved VALUES (2)')
        with self.manager() as manager:
            with self.assertRaisesRegex(InstallError, 'installed_version_changed'): manager.rollback('0.7.0')
            result = manager.rollback('0.8.0')
        self.assertEqual(result['version'], '0.7.0')
        for name, raw in paths.items(): self.assertEqual((self.root / name).read_bytes(), raw)
        with sqlite3.connect(str(dbpath)) as db: self.assertEqual(db.execute('SELECT id FROM preserved').fetchall(), [(1,), (2,)])
        backups = list((self.root / 'var/lib/ss07-deploy/transactions').glob('*/databases.json'))
        self.assertEqual(len(backups), 3)
        self.assertTrue(any('var/lib/ss07/catalog.sqlite' in json.loads(p.read_text()) for p in backups))
        self.assertTrue(any('var/lib/ss07-media/access.sqlite' in json.loads(p.read_text()) for p in backups))
        self.assertEqual([e['outcome'] for e in rtsp_audit.history()['items']], ['after_upgrade', 'before_upgrade'])

    def test_health_failure_restores_previous_release_and_running_services(self):
        self.existing(); self.backend.fail_version = '0.8.0'
        with self.assertRaisesRegex(InstallError, 'release_health_timeout'): self.install('0.8.0')
        value = transaction(self.root)
        self.assertEqual(value['phase'], 'ROLLED_BACK'); self.assertTrue(value['service_health_verified'])
        self.assertEqual(os.readlink(str(self.root / 'opt/ss07/current')), 'releases/0.7.0')
        self.assertEqual(self.backend.units, ['ss07-api.service'])
        self.assertEqual(load(self.root / 'var/lib/ss07-deploy/installed.json')['version'], '0.7.0')

    def test_stop_failure_does_not_switch_files_under_running_processes(self):
        self.existing(); self.backend.fail_action = 'stop'
        with self.assertRaises(subprocess.SubprocessError): self.install('0.8.0')
        self.assertEqual(transaction(self.root)['phase'], 'ROLLBACK_FAILED')
        self.assertTrue((self.root / 'var/lib/ss07-deploy/in-progress').exists())
        self.assertEqual(os.readlink(str(self.root / 'opt/ss07/current')), 'releases/0.7.0')
        self.backend.fail_action = None
        with self.manager() as manager: manager.recover(transaction(self.root)['operation_id'])
        self.assertEqual(transaction(self.root)['phase'], 'ROLLED_BACK')

    def test_preflight_failure_changes_no_live_code_or_transaction(self):
        self.existing(); before = transaction(self.root); self.backend.fail_action = 'preflight'
        with self.assertRaisesRegex(InstallError, 'preflight'): self.install('0.8.0')
        self.assertEqual(transaction(self.root), before)
        self.assertEqual(os.readlink(str(self.root / 'opt/ss07/current')), 'releases/0.7.0')
        self.assertEqual(self.backend.units, ['ss07-api.service'])

    def test_unfinished_operation_and_config_application_block_upgrade(self):
        self.existing()
        state = self.root / 'var/lib/ss07-apply/transaction.json'; save(state, {'phase': 'STARTING'})
        with self.assertRaisesRegex(InstallError, 'configuration_recovery_required'): self.install('0.8.0')
        self.assertEqual(os.readlink(str(self.root / 'opt/ss07/current')), 'releases/0.7.0')
        value = transaction(self.root); value['phase'] = 'RECOVERY_PENDING'
        save(self.root / 'var/lib/ss07-deploy/transaction.json', value)
        with self.assertRaisesRegex(InstallError, 'recovery_required'): self.install('0.8.0')
        with self.manager() as manager:
            with self.assertRaisesRegex(InstallError, 'operation_changed'): manager.recover('old-operation')

    def test_post_boot_recovery_finishes_health_check_without_losing_candidate_config(self):
        self.existing(); self.install('0.8.0')
        state = self.root / 'var/lib/ss07-deploy/transaction.json'; value = transaction(self.root)
        value['phase'] = 'VERIFYING'; save(state, value)
        boot(self.root)
        self.assertEqual(transaction(self.root)['phase'], 'RECOVERY_PENDING')
        with self.manager() as manager: manager.recover(value['operation_id'])
        self.assertEqual(transaction(self.root)['phase'], 'ROLLED_BACK')
        self.assertFalse((self.root / 'var/lib/ss07-deploy/in-progress').exists())

    def test_corrupt_cached_package_and_current_code_are_never_reused(self):
        self.existing(); self.install('0.8.0')
        checksum = self.archives['0.7.0'][1]
        path = self.root / 'var/lib/ss07-deploy/imports' / checksum / 'opt/ss07/releases/0.7.0/tools/ss07'
        path.write_text('corrupt cache')
        with self.manager() as manager:
            with self.assertRaisesRegex(InstallError, 'imported_file_changed'): manager.rollback('0.8.0')
        path = self.root / 'opt/ss07/releases/0.8.0/tools/ss07'; path.write_text('corrupt running code')
        with self.assertRaisesRegex(ValueError, 'release_digest'): self.install('0.7.0')
        with self.manager() as manager:
            status = manager.status()
            self.assertEqual(status['installed_integrity_error'], 'previous_release_digest_mismatch')
            self.assertIsNotNone(status['transaction']['operation_id'])

    def test_single_installer_lock_and_checksum_path_validation(self):
        with self.manager() as manager:
            with self.assertRaises(BlockingIOError):
                with self.manager(): pass
            with self.assertRaisesRegex(InstallError, 'invalid_archive_checksum'): manager.install('/missing', '../../etc')
        with self.manager() as manager: self.assertIsNone(manager.status()['installed_version'])

    def test_low_system_disk_space_refuses_installation_before_stopping_services(self):
        self.existing(); before = transaction(self.root); history = list(self.backend.history)
        with patch('ss07.release_install.shutil.disk_usage', return_value=shutil._ntuple_diskusage(1000, 999, 1)):
            with self.assertRaisesRegex(InstallError, 'insufficient_installation_space'): self.install('0.8.0')
        self.assertEqual(transaction(self.root), before); self.assertEqual(self.backend.history, history)

    def test_first_install_failure_recovers_without_an_old_release(self):
        self.backend.fail_action = 'daemon-reload'
        # The first reload precedes PREPARED and must not touch current.
        with self.assertRaises(subprocess.SubprocessError): self.install('0.8.0')
        self.assertFalse((self.root / 'opt/ss07/current').exists())
        self.backend.fail_action = None
        with patch.object(self.backend, 'prepare_users', side_effect=OSError('test directory failure')):
            with self.assertRaises(OSError): self.install('0.8.0')
        self.assertEqual(transaction(self.root)['phase'], 'ROLLED_BACK')
        self.assertFalse(os.path.lexists(str(self.root / 'opt/ss07/current')))
        self.assertFalse((self.root / 'etc/ss07/system.json').exists())

    def test_activation_checks_readiness_before_marker_and_removes_it_on_failure(self):
        self.install('0.8.0')
        with patch('ss07.provisioning.check', side_effect=ValueError('test_missing_certificate')):
            with self.manager() as manager:
                with self.assertRaisesRegex(ValueError, 'missing_certificate'): manager.activate()
        self.assertFalse((self.root / 'etc/ss07-provisioned').exists())
        self.backend.fail_version = '0.8.0'
        with patch('ss07.provisioning.check', return_value={'config_revision': '0' * 64}):
            with self.manager() as manager:
                with self.assertRaisesRegex(InstallError, 'release_health_timeout'): manager.activate()
            self.assertEqual(load(self.root / 'var/lib/ss07-deploy/activation.json')['phase'], 'FAILED')
            self.assertFalse((self.root / 'etc/ss07-provisioned').exists())
            self.backend.fail_version = None
            with self.manager() as manager: result = manager.activate()
        self.assertTrue(result['activated']); self.assertFalse(result['hardware_verified'])
        self.assertIn(('enable', ('ss07.target',)), self.backend.history)

    def test_power_loss_during_activation_leaves_system_unprovisioned(self):
        self.install('0.8.0')
        with patch('ss07.provisioning.check', return_value={'config_revision': '0' * 64}):
            with self.manager() as manager: manager.activate()
        path = self.root / 'var/lib/ss07-deploy/activation.json'; value = load(path)
        value['phase'] = 'STARTING'; save(path, value)
        boot(self.root)
        self.assertFalse((self.root / 'etc/ss07-provisioned').exists())
        self.assertEqual(load(path)['error'], 'activation_interrupted')

    def test_factory_image_registration_allows_later_upgrade_and_rollback(self):
        imported = self.out / 'factory-import'; unpack(*self.archives['0.7.0'], imported)
        for name in ('opt', 'etc', 'usr'): shutil.copytree(imported / name, self.root / name, symlinks=True)
        with self.manager() as manager:
            result = manager.register_image(imported)
            self.assertTrue(result['factory_registered']); self.assertFalse(result['activated'])
            self.assertEqual(manager.status()['installed_version'], '0.7.0')
            with self.assertRaisesRegex(InstallError, 'already_registered'): manager.register_image(imported)
        self.assertEqual(self.backend.history, [])
        self.install('0.8.0')
        with self.manager() as manager: self.assertEqual(manager.rollback('0.8.0')['version'], '0.7.0')


class HealthTests(unittest.TestCase):
    def test_loopback_health_check_preserves_the_provisioned_public_host(self):
        config = load_config(ROOT / 'config/system.example.json')
        backend = SystemBackend()
        state = dict(instance_id='api-test', seq='1', observed_monotonic_us='20', state='RUNNING',
                     service_revision=fingerprint(config, 'api'), payload={})
        backend.observe = lambda: {'api': state}
        baseline = {'_running_units': ['ss07-api.service']}
        def response(request, **kwargs):
            self.assertEqual(request.full_url, 'http://127.0.0.1:8077/healthz')
            self.assertEqual(request.get_header('Host'), 'ss07.test:8443')
            return io.BytesIO(b'{"data":{"app_version":"0.8.0"}}')
        with patch('ss07.release_install.load_gateway', return_value={'public_origin': 'https://ss07.test:8443'}), \
                patch('ss07.release_install.urlopen', side_effect=response):
            self.assertFalse(backend.healthy('0.8.0', config, 10, baseline))
            state['seq'] = '2'
            self.assertTrue(backend.healthy('0.8.0', config, 10, baseline))

    def test_hardware_absence_requires_same_degradation_and_advancing_heartbeats(self):
        config = load_config(ROOT / 'config/system.example.json')
        config['cameras']['front'] = dict(binding='/dev/v4l/by-path/test', mode=dict(fourcc='H264', width=1920, height=1080, fps_num=25, fps_den=1))
        name = 'camera.front'
        state = dict(instance_id='test-worker', seq='1', observed_monotonic_us='20', state='UNAVAILABLE',
                     service_revision=fingerprint(config, name), payload={'reason': 'camera_missing'})
        baseline = {'_running_units': ['ss07-camera@front.service'], name: copy.deepcopy(state)}
        backend = SystemBackend(); backend.observe = lambda: {name: state}
        self.assertFalse(backend.healthy('0.8.0', config, 10, baseline))
        state['seq'] = '2'
        self.assertTrue(backend.healthy('0.8.0', config, 10, baseline))
        state['payload']['reason'] = 'unsupported_format'
        self.assertFalse(backend.healthy('0.8.0', config, 10, baseline))
        state['payload']['reason'] = 'camera_missing'; state['instance_id'] = 'restarted'
        self.assertFalse(backend.healthy('0.8.0', config, 10, baseline))
        state['seq'] = '3'; state['observed_monotonic_us'] = '9'
        self.assertFalse(backend.healthy('0.8.0', config, 10, baseline))


if __name__ == '__main__': unittest.main()
