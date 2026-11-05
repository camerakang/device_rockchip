"""Lifecycle observations and real recovery files; no peripheral simulation."""
import copy
import sqlite3
import unittest
from unittest.mock import patch

from ss07.config import ROOT, load
from ss07.config_runtime import ORDER, UNITS, acknowledgement, fingerprint, plan
from ss07.release_contract import DATA
from ss07.release_install import STOP_ORDER
from ss07.release_restore import DATABASE_KINDS, FILES, RUN_UNITS, RestoreError, boot, check_data, save, transaction
from ss07.sonar_archive import Catalog
from ss07.sonar_journal import Journal
import test_release_restore as restore_fixture


class SonarLifecycleTests(unittest.TestCase):
    def setUp(self): self.config = load(ROOT / 'config/system.example.json')

    def snapshot(self, state, reason, **payload):
        return dict(state=state, payload=dict(reason=reason, **payload),
                    service_revision=fingerprint(self.config, 'sonar'), observed_monotonic_us='100')

    def test_missing_protocol_requires_explicit_unavailable_acknowledgement(self):
        self.assertIn('sonar', ORDER)
        self.assertIn(UNITS['sonar'], RUN_UNITS)
        self.assertIn(UNITS['sonar'], STOP_ORDER)
        observed = self.snapshot('UNAVAILABLE', 'sonar_not_configured')
        self.assertTrue(acknowledgement(self.config, 'sonar', observed, 100)[0])
        for state, reason in [('RUNNING', None), ('UNAVAILABLE', 'serial_open_failed'), ('ERROR', 'sonar_not_configured')]:
            self.assertFalse(acknowledgement(self.config, 'sonar', self.snapshot(state, reason), 100)[0])
        self.assertFalse(acknowledgement(self.config, 'sonar', observed, 101)[0])
        self.config['sonar']['vendor_protocol'] = 'unknown-test-protocol'
        self.assertTrue(acknowledgement(self.config, 'sonar', self.snapshot('UNAVAILABLE', 'sonar_protocol_unavailable'))[0])
        self.assertFalse(acknowledgement(self.config, 'sonar', self.snapshot('UNAVAILABLE', 'sonar_not_configured'))[0])

    def test_configured_profile_requires_serial_and_enabled_archive_observation(self):
        # Only the lifecycle branch is injected. No protocol, UART or sample is supplied.
        with patch('ss07.sonar_driver.resolve', return_value=(None, {})):
            self.assertFalse(acknowledgement(self.config, 'sonar', self.snapshot('RUNNING', None))[0])
            self.assertTrue(acknowledgement(self.config, 'sonar', self.snapshot('RUNNING', None, serial_connected=True))[0])
            self.config['storage']['sonar_recording_enabled'] = True
            for state in ['BLOCKED', 'ERROR', 'STOPPED']:
                observed = self.snapshot('RUNNING', None, serial_connected=True, archive={'state': state})
                self.assertFalse(acknowledgement(self.config, 'sonar', observed)[0])
            observed = self.snapshot('RUNNING', None, serial_connected=True, archive={'state': 'RECORDING'})
            self.assertTrue(acknowledgement(self.config, 'sonar', observed)[0])

    def test_disabled_archive_does_not_restart_sonar_for_video_storage_changes(self):
        candidate = copy.deepcopy(self.config)
        candidate['storage']['segment_seconds'] = 60
        self.assertNotIn('sonar', plan(self.config, candidate)['services'])
        self.assertEqual(fingerprint(candidate, 'sonar'), fingerprint(self.config, 'sonar'))
        candidate['storage']['sonar_recording_enabled'] = False
        self.assertEqual(fingerprint(candidate, 'sonar'), fingerprint(self.config, 'sonar'))
        self.config['storage']['sonar_recording_enabled'] = True
        candidate['storage']['sonar_recording_enabled'] = True
        self.assertNotEqual(fingerprint(candidate, 'sonar'), fingerprint(self.config, 'sonar'))


class SonarRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = restore_fixture.RestoreTests('test_no_transaction_and_successful_install_do_not_revert_release')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.journal = Journal(self.root / 'var/lib/ss07/sonar/samples.sqlite')
        self.journal.initialize()
        self.catalog = Catalog(self.root / 'var/lib/ss07/sonar/archives.sqlite')
        self.catalog.initialize()

    def contract(self): return {name: DATA[name] for name in DATABASE_KINDS}

    def test_old_backup_positions_and_legacy_transaction_recovery_remain_valid(self):
        self.assertEqual(FILES.index('usr/lib/sysusers.d/ss07.conf'), 10)
        self.assertEqual(FILES.index('etc/ss07/system.json'), 12)
        self.assertEqual(FILES.index('var/lib/ss07-deploy/installed.json'), 14)
        before = {p: p.read_bytes() for p in (self.journal.path, self.catalog.path)}
        self.assertTrue(boot(self.root)['files_restored'])
        for path, raw in before.items(): self.assertEqual(path.read_bytes(), raw)

    def test_current_transaction_checks_sonar_schemas_and_preserves_data(self):
        value = dict(self.fixture.value, data_contract=self.contract())
        save(self.fixture.state, value)
        self.assertEqual(transaction(self.root)['data_contract'], self.contract())
        result = check_data(self.root, self.contract())
        self.assertEqual(result['var/lib/ss07/sonar/samples.sqlite'], 1)
        self.assertEqual(result['var/lib/ss07/sonar/archives.sqlite'], 1)
        before = {p: p.read_bytes() for p in (self.journal.path, self.catalog.path)}
        boot(self.root)
        for path, raw in before.items(): self.assertEqual(path.read_bytes(), raw)

    def test_unknown_sonar_schema_keeps_recovery_gate_and_never_restores_old_database(self):
        save(self.fixture.state, dict(self.fixture.value, data_contract=self.contract()))
        with sqlite3.connect(str(self.catalog.path)) as db: db.execute('PRAGMA user_version=2')
        before = self.catalog.path.read_bytes()
        with self.assertRaisesRegex(RestoreError, 'offline_database_migration_required'): boot(self.root)
        self.assertTrue(self.fixture.gate.exists())
        self.assertEqual(self.catalog.path.read_bytes(), before)
        self.assertTrue(transaction(self.root)['files_restored'])

    def test_partial_or_invalid_contract_is_rejected_before_database_open(self):
        for value in [dict(self.contract(), sonar_journal=True), dict(self.contract(), unknown=1),
                      {k: v for k, v in self.contract().items() if k != 'sonar_journal'}]:
            with self.assertRaisesRegex(RestoreError, 'invalid_recovery_data_contract'): check_data(self.root, value)


if __name__ == '__main__': unittest.main()
