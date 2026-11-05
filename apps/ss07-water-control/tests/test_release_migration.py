"""Actual files/SQLite, fault-injected lifecycle. No host services or hardware."""
import copy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
import uuid

from ss07.config import ROOT, load as load_config, revision
from ss07.config_runtime import plan
from ss07.release_contract import DATA, LEGACY_DATA, bootstrap_files, contract, runtime_units, system_files
from ss07.release_install import Installer, InstallError
from ss07.release_migration import HISTORY
from ss07.release_package import unpack
from ss07.release_restore import boot, load, save, transaction, write_file
from ss07.sonar_archive import Catalog
from ss07.sonar_journal import Journal
from test_release_install import Backend
from test_release_package import fixture


def package(path, version, legacy=False):
    def settings(manifest, files):
        deployment = contract(legacy=legacy)
        manifest['deployment'] = deployment
        allowed = system_files(deployment['data']) + bootstrap_files(deployment['data']) + ('etc/ss07/system.json', 'etc/ss07-active/system.json')
        for name in list(files):
            if not name.startswith('opt/ss07/releases/') and name not in allowed:
                files.pop(name); manifest['files'].pop(name)
        for name in ('etc/ss07/system.json', 'etc/ss07-active/system.json'):
            files[name] = (ROOT / 'config/system.example.json').read_bytes()
            manifest['files'][name] = hashlib.sha256(files[name]).hexdigest()
    return fixture(path, version, modify=settings)


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.out = Path(self.temp.name); self.root = self.out / 'root'; self.root.mkdir()
        self.backend = Backend(self.root)
        self.archives = {}
        for version, legacy in [('0.9.0', True), ('0.10.0', False)]:
            path = self.out / (version + '.tar.gz')
            self.archives[version] = (path, package(path, version, legacy))

    def manager(self): return Installer(self.root, self.backend, timeout=.25, settle=0)

    def install(self, version):
        with self.manager() as manager: return manager.install(*self.archives[version])

    def current(self): return os.readlink(str(self.root / 'opt/ss07/current'))

    def existing(self, newer=False):
        self.install('0.9.0')
        self.backend.units = ['ss07-api.service', 'ss07.target']
        if newer: self.install('0.10.0')

    def history(self):
        config = load_config(self.root / 'etc/ss07-active/system.json')
        config['storage']['sonar_recording_enabled'] = False
        for name in ('etc/ss07/system.json', 'etc/ss07-active/system.json'):
            write_file(self.root / name, (json.dumps(config)+'\n').encode(), 0o640)
        previous = copy.deepcopy(config); previous['device_id'] = 'previous-operation'
        value = dict(schema_version=1, operation_id=str(uuid.uuid4()), actor='operator', phase='APPLIED',
            previous_revision=revision(previous), target_revision=revision(config), services=['sonar'],
            results={'sonar': dict(state='VERIFIED', reason='sonar_profile_unavailable')},
            error=None, configuration_restored=False)
        save(self.root / HISTORY[0], value)
        save(self.root / HISTORY[1], previous); save(self.root / HISTORY[2], config)
        return {n: (self.root / n).read_bytes() for n in HISTORY + ('etc/ss07/system.json', 'etc/ss07-active/system.json')}

    def data(self):
        journal = Journal(self.root / 'var/lib/ss07/sonar/samples.sqlite'); journal.initialize()
        catalog = Catalog(self.root / 'var/lib/ss07/sonar/archives.sqlite'); catalog.initialize()
        for path in (journal.path, catalog.path):
            with sqlite3.connect(str(path)) as db:
                db.execute('CREATE TABLE retained_test_row(value TEXT)')
                db.execute("INSERT INTO retained_test_row VALUES ('synthetic_test')")
        sample = self.root / 'data/ss07/recordings/sonar/retained-fixture.jsonl'
        write_file(sample, b'{"evidence_kind":"synthetic_test"}\n')
        return {p: p.read_bytes() for p in (journal.path, catalog.path, sample)}

    def assert_data(self, before):
        for path, raw in before.items(): self.assertEqual(path.read_bytes(), raw)

    def test_recognized_legacy_package_import_uses_its_own_unit_and_path_contract(self):
        result = unpack(*self.archives['0.9.0'], self.out / 'import')
        self.assertEqual(result['deployment'], contract(legacy=True))
        self.assertFalse((self.out / 'import/usr/lib/systemd/system/ss07-sonar.service').exists())

    def test_forward_adds_sonar_only_for_active_target_and_preserves_configuration(self):
        self.existing()
        names = ('etc/ss07/system.json', 'etc/ss07-active/system.json')
        before = {n: (self.root / n).read_bytes() for n in names}
        result = self.install('0.10.0')
        self.assertEqual(result['migration'], 'sonar_config_v1_to_v2')
        self.assertEqual(self.current(), 'releases/0.10.0')
        self.assertIn('ss07-sonar.service', self.backend.units)
        self.assertEqual(load(self.root / 'var/lib/ss07-deploy/installed.json')['data'], DATA)
        for n, raw in before.items(): self.assertEqual((self.root / n).read_bytes(), raw)
        for action, units in self.backend.history:
            if action == 'stop': self.assertNotIn('ss07-sonar.service', units)
        self.assertEqual(len(transaction(self.root)['data_contract']), 5)

    def test_partial_activation_does_not_enable_additional_services(self):
        self.install('0.9.0'); self.backend.units = ['ss07-api.service']
        self.install('0.10.0')
        self.assertEqual(self.backend.units, ['ss07-api.service'])

    def test_downgrade_archives_history_strips_only_false_flag_and_keeps_all_sonar_data(self):
        self.existing(newer=True); original = self.history(); before = self.data()
        info = (self.root / 'etc/ss07/system.json').stat()
        with self.manager() as manager: result = manager.rollback('0.10.0')
        self.assertEqual(result['migration'], 'sonar_config_v2_to_v1')
        self.assertEqual(self.current(), 'releases/0.9.0')
        self.assertNotIn('ss07-sonar.service', self.backend.units)
        self.assertFalse((self.root / 'usr/lib/systemd/system/ss07-sonar.service').exists())
        self.assertEqual(load(self.root / 'var/lib/ss07-deploy/installed.json')['data'], LEGACY_DATA)
        for n in HISTORY: self.assertFalse((self.root / n).exists())
        for n in ('etc/ss07/system.json', 'etc/ss07-active/system.json'):
            expected = json.loads(original[n]); expected['storage'].pop('sonar_recording_enabled')
            self.assertEqual(load_config(self.root / n), expected)
        current = (self.root / 'etc/ss07/system.json').stat()
        self.assertEqual((current.st_uid, current.st_gid, current.st_mode), (info.st_uid, info.st_gid, info.st_mode))
        value = transaction(self.root); directory = self.root / ('var/lib/ss07-deploy/transactions/'+value['operation_id'])
        receipt = load(directory / 'migration.json'); self.assertTrue(receipt['history_archived'])
        from ss07.release_restore import FILES
        for n, raw in original.items(): self.assertEqual((directory / ('file-'+str(FILES.index(n)))).read_bytes(), raw)
        self.assert_data(before)

    def test_failed_downgrade_restores_exact_configuration_history_and_sonar_data(self):
        self.existing(newer=True); original = self.history(); before = self.data()
        self.backend.fail_version = '0.9.0'
        with self.manager() as manager, self.assertRaisesRegex(InstallError, 'release_health_timeout'):
            manager.rollback('0.10.0')
        self.assertEqual(self.current(), 'releases/0.10.0')
        self.assertEqual(transaction(self.root)['phase'], 'ROLLED_BACK')
        for n, raw in original.items(): self.assertEqual((self.root / n).read_bytes(), raw)
        self.assertTrue((self.root / 'usr/lib/systemd/system/ss07-sonar.service').exists())
        self.assert_data(before)

    def test_interrupted_downgrade_boot_recovery_restores_newer_files_without_losing_later_data(self):
        self.existing(newer=True); original = self.history(); before = self.data()
        with self.manager() as manager: manager.rollback('0.10.0')
        value = transaction(self.root); value['phase'] = 'VERIFYING'
        save(self.root / 'var/lib/ss07-deploy/transaction.json', value)
        self.assertTrue(boot(self.root)['files_restored'])
        self.assertEqual(self.current(), 'releases/0.10.0')
        for n, raw in original.items(): self.assertEqual((self.root / n).read_bytes(), raw)
        self.assert_data(before)
        with self.manager() as manager: manager.recover(value['operation_id'])
        self.assertEqual(transaction(self.root)['phase'], 'ROLLED_BACK')

    def test_enabled_archive_in_either_configuration_prevents_downgrade_before_lifecycle_actions(self):
        self.existing(newer=True)
        for n in ('etc/ss07/system.json', 'etc/ss07-active/system.json'):
            config = load_config(self.root / n); config['storage'].update(sonar_recording_enabled=True, filesystem_uuid='test-volume')
            raw = (self.root / n).read_bytes(); write_file(self.root / n, json.dumps(config).encode(), 0o640)
            calls = list(self.backend.history)
            with self.manager() as manager, self.assertRaisesRegex(ValueError, 'must_be_disabled'):
                manager.rollback('0.10.0')
            self.assertEqual(self.backend.history, calls); self.assertEqual(self.current(), 'releases/0.10.0')
            write_file(self.root / n, raw, 0o640)

    def test_archive_can_be_disabled_and_applied_without_guessing_a_vendor_protocol(self):
        previous = load_config(ROOT / 'config/system.example.json')
        previous['storage'].update(sonar_recording_enabled=True, filesystem_uuid='test-volume')
        candidate = copy.deepcopy(previous); candidate['storage']['sonar_recording_enabled'] = False
        self.assertEqual(plan(previous, candidate)['services'], ['sonar', 'api'])
        with self.assertRaisesRegex(ValueError, 'sonar_driver_unavailable'): plan(candidate, previous)

    def test_unknown_sonar_schema_prevents_downgrade_and_is_never_replaced(self):
        self.existing(newer=True); before = self.data()
        path = self.root / 'var/lib/ss07/sonar/archives.sqlite'
        with sqlite3.connect(str(path)) as db: db.execute('PRAGMA user_version=2')
        before[path] = path.read_bytes(); calls = list(self.backend.history)
        with self.manager() as manager, self.assertRaisesRegex(ValueError, 'offline_database_migration_required'):
            manager.rollback('0.10.0')
        self.assertEqual(calls, self.backend.history); self.assert_data(before)

    def test_configuration_writer_drift_before_prepare_does_not_overwrite_user_edit(self):
        self.existing(newer=True); self.history()
        original = self.backend.preflight
        def drift(directory, root):
            original(directory, root)
            write_file(self.root / 'etc/ss07/system.json', b'external edit\n')
        self.backend.preflight = drift
        with self.manager() as manager, self.assertRaisesRegex(ValueError, 'migration_input_changed'):
            manager.rollback('0.10.0')
        self.assertEqual(self.current(), 'releases/0.10.0')
        self.assertEqual((self.root / 'etc/ss07/system.json').read_bytes(), b'external edit\n')
        self.assertEqual(transaction(self.root)['phase'], 'COMMITTED')

    def test_unfinished_configuration_transaction_requires_recovery_before_downgrade(self):
        self.existing(newer=True); self.history()
        value = load(self.root / HISTORY[0]); value['phase'] = 'VERIFYING'; save(self.root / HISTORY[0], value)
        calls = list(self.backend.history)
        with self.manager() as manager, self.assertRaisesRegex(ValueError, 'configuration_recovery_required'):
            manager.rollback('0.10.0')
        self.assertEqual(calls, self.backend.history)

    def test_late_configuration_drift_keeps_recovery_gated_and_preserves_external_change(self):
        self.existing(newer=True); self.history(); before = self.data()
        original = self.backend.command
        def command(action, units=()):
            original(action, units)
            if action == 'stop' and 'ss07-api.service' in units:
                write_file(self.root / 'etc/ss07/system.json', b'external late edit\n')
        self.backend.command = command
        with self.manager() as manager, self.assertRaisesRegex(ValueError, 'migration_input_changed'):
            manager.rollback('0.10.0')
        self.assertEqual(self.current(), 'releases/0.10.0')
        self.assertEqual((self.root / 'etc/ss07/system.json').read_bytes(), b'external late edit\n')
        self.assertEqual(transaction(self.root)['phase'], 'ROLLBACK_FAILED')
        self.assertTrue((self.root / 'var/lib/ss07-deploy/in-progress').exists())
        self.assert_data(before)

    def test_missing_history_snapshot_prevents_downgrade_before_service_actions(self):
        self.existing(newer=True); original = self.history()
        (self.root / HISTORY[1]).unlink(); original.pop(HISTORY[1])
        calls = list(self.backend.history)
        with self.manager() as manager, self.assertRaisesRegex(ValueError, 'configuration_history_incomplete'):
            manager.rollback('0.10.0')
        self.assertEqual(calls, self.backend.history)
        for name, raw in original.items(): self.assertEqual((self.root / name).read_bytes(), raw)
        self.assertFalse((self.root / HISTORY[1]).exists())
        self.assertEqual(self.current(), 'releases/0.10.0')

    def test_corrupt_history_snapshots_are_preserved_and_prevent_downgrade(self):
        self.existing(newer=True)
        for raw in (b'broken JSON\n', b'{"schema_version":1}\n'):
            original = self.history(); write_file(self.root / HISTORY[2], raw)
            original[HISTORY[2]] = raw; calls = list(self.backend.history)
            with self.manager() as manager, self.assertRaises(ValueError): manager.rollback('0.10.0')
            self.assertEqual(calls, self.backend.history)
            for name, content in original.items(): self.assertEqual((self.root / name).read_bytes(), content)

    def test_valid_but_mismatched_history_snapshot_prevents_downgrade(self):
        self.existing(newer=True); self.history()
        value = load_config(self.root / HISTORY[1]); value['device_id'] = 'unrelated-configuration'
        save(self.root / HISTORY[1], value); raw = (self.root / HISTORY[1]).read_bytes()
        calls = list(self.backend.history)
        with self.manager() as manager, self.assertRaisesRegex(ValueError, 'configuration_history_mismatch'):
            manager.rollback('0.10.0')
        self.assertEqual(calls, self.backend.history)
        self.assertEqual((self.root / HISTORY[1]).read_bytes(), raw)

    def test_corrupt_orphan_history_snapshot_is_not_discarded(self):
        self.existing(newer=True); self.history()
        (self.root / HISTORY[0]).unlink(); write_file(self.root / HISTORY[1], b'broken orphan\n')
        calls = list(self.backend.history)
        with self.manager() as manager, self.assertRaises(ValueError): manager.rollback('0.10.0')
        self.assertEqual(calls, self.backend.history)
        self.assertEqual((self.root / HISTORY[1]).read_bytes(), b'broken orphan\n')


if __name__ == '__main__': unittest.main()
