"""Interrupted release recovery preserves compatible data and refuses ambiguous state."""
import copy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
import uuid

from ss07.release_contract import DATA
from ss07.release_restore import FILES, RestoreError, boot, check_data, read_regular, save, transaction, verify_release, write_file


def sha(raw): return hashlib.sha256(raw).hexdigest()


class RestoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.identity = str(uuid.uuid4())
        self.directory = self.root / ('var/lib/ss07-deploy/transactions/' + self.identity)
        self.directory.mkdir(parents=True)
        self.old = self.root / 'opt/ss07/releases/0.7.0'; self.old.mkdir(parents=True)
        write_file(self.old / 'program.py', b'old program\n', mode=0o644)
        save(self.old / '.ss07-release.json', dict(schema_version=1, version='0.7.0', data=dict(DATA), files={'program.py': sha(b'old program\n')}))
        self.pointer = self.root / 'opt/ss07/current'; self.pointer.symlink_to('releases/0.8.0')
        self.name = 'usr/lib/systemd/system/ss07-api.service'
        self.file = self.root / self.name; write_file(self.file, b'new unit\n', mode=0o644)
        write_file(self.directory / ('file-' + str(FILES.index(self.name))), b'old unit\n')
        self.value = dict(schema_version=1, operation_id=self.identity, phase='VERIFYING', previous='releases/0.7.0',
                          candidate='releases/0.8.0', files={self.name: dict(previous_sha256=sha(b'old unit\n'), new_sha256=sha(b'new unit\n'),
                          mode=0o644, uid=os.geteuid(), gid=os.getegid())}, running_before=['ss07-api.service'],
                          data_contract={key: DATA[key] for key in ('catalog', 'timecode_journal', 'audit')},
                          baseline={}, error=None, files_restored=False, service_health_verified=False)
        self.state = self.root / 'var/lib/ss07-deploy/transaction.json'; save(self.state, self.value)
        self.gate = self.root / 'var/lib/ss07-deploy/in-progress'; write_file(self.gate, b'upgrade\n')

    def database(self, version=2):
        path = self.root / 'var/lib/ss07/catalog.sqlite'; path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(path)) as db:
            db.execute('PRAGMA application_id=1397960759'); db.execute('PRAGMA user_version=' + str(version))
            db.execute('CREATE TABLE retained (value TEXT)'); db.execute("INSERT INTO retained VALUES ('new recording remains')")
        return path

    def test_boot_restores_code_and_units_but_preserves_new_compatible_database_rows(self):
        path = self.database(); before = path.read_bytes()
        result = boot(self.root)
        self.assertTrue(result['files_restored']); self.assertFalse(result['service_health_verified'])
        self.assertEqual(os.readlink(str(self.pointer)), 'releases/0.7.0')
        self.assertEqual(self.file.read_bytes(), b'old unit\n')
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(self.gate.exists())
        self.assertEqual(transaction(self.root)['phase'], 'RECOVERY_PENDING')
        self.assertTrue(boot(self.root)['recovery_required'])  # Idempotent after another reboot.

    def test_schema_change_keeps_consumers_gated_instead_of_reverting_database(self):
        path = self.database(version=3); before = path.read_bytes()
        with self.assertRaisesRegex(RestoreError, 'offline_database_migration_required'): boot(self.root)
        self.assertEqual(self.file.read_bytes(), b'old unit\n')
        self.assertTrue(self.gate.exists()); self.assertEqual(path.read_bytes(), before)
        self.assertTrue(transaction(self.root)['files_restored'])

    def test_corrupt_backup_fails_before_any_replacement(self):
        write_file(self.directory / ('file-' + str(FILES.index(self.name))), b'bad backup')
        with self.assertRaisesRegex(RestoreError, 'backup_digest'): boot(self.root)
        self.assertEqual(self.file.read_bytes(), b'new unit\n')
        self.assertEqual(os.readlink(str(self.pointer)), 'releases/0.8.0')

    def test_modified_old_program_is_not_reactivated(self):
        write_file(self.old / 'program.py', b'changed by another operation\n', mode=0o644)
        with self.assertRaisesRegex(RestoreError, 'release_digest'): boot(self.root)
        self.assertEqual(self.file.read_bytes(), b'new unit\n')

    def test_external_file_or_pointer_drift_is_not_overwritten(self):
        write_file(self.file, b'local administrator change', mode=0o644)
        with self.assertRaisesRegex(RestoreError, 'file_drift'): boot(self.root)
        self.pointer.unlink(); self.pointer.symlink_to('releases/0.9.0')
        with self.assertRaisesRegex(RestoreError, 'release_drift'): boot(self.root)

    def test_invalid_journal_cannot_select_arbitrary_paths_or_units(self):
        for changes in (dict(schema_version=True), dict(previous='../../etc'), dict(running_before=['ssh.service']),
                        dict(files={'etc/shadow': next(iter(self.value['files'].values()))})):
            with self.subTest(changes=changes):
                save(self.state, dict(self.value, **changes))
                with self.assertRaises(RestoreError): boot(self.root)
        self.assertEqual(self.file.read_bytes(), b'new unit\n')

    def test_symlink_backup_and_parent_are_not_followed(self):
        backup = self.directory / ('file-' + str(FILES.index(self.name)))
        backup.unlink(); backup.symlink_to(self.file)
        with self.assertRaises(OSError): boot(self.root)
        self.assertEqual(self.file.read_bytes(), b'new unit\n')

    def test_no_transaction_and_successful_install_do_not_revert_release(self):
        self.state.unlink(); self.assertFalse(boot(self.root)['recovery_required'])
        save(self.state, dict(self.value, phase='COMMITTED', service_health_verified=True))
        self.assertFalse(boot(self.root)['recovery_required'])
        self.assertEqual(os.readlink(str(self.pointer)), 'releases/0.8.0')


if __name__ == '__main__': unittest.main()
