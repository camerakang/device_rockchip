import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

from ss07.catalog import Catalog, CatalogError, RecordStore
from ss07.timecode_model import Mapper
from ss07.timecode_wire import Packet
from test_recordings import RecordingFixture


class AssociationTests(RecordingFixture):
    def mapped_segment(self, changed=False, lost=False):
        mapper = Mapper('test-boot', 'receiver-a', 'synthetic_test')
        epoch, source = str(2**63 + 123), 2**63 + 500
        mapper.observe(Packet(int(epoch), 0, source), 1000, 1000)
        start = mapper.tag(1500, 1500, 'segment_create_notification')
        for seq in range(1, 201):
            when = 1000 + 10000 * seq
            if not lost: mapper.observe(Packet(int(epoch), seq, source + seq * 10000), when, when)
        if changed:
            mapper.observe(Packet(2, 0, 100000), 2001000, 2001000)
        end = mapper.tag(2001500, 2001500, 'segment_complete_notification')
        relative = self.segment(complete=False)
        item = self.store.finalize(relative, self.session, 2000, '1500', '2001500', dict(start=start, end=end))
        return item, epoch, source

    def test_large_u64_epoch_scoped_interval_query_and_sidecar_recovery(self):
        item, epoch, source = self.mapped_segment()
        result = self.catalog.list(epoch=epoch, time_start_us=str(source + 500000), time_end_us=str(source + 600000))
        self.assertEqual(len(result['items']), 1)
        self.assertEqual(result['items'][0]['timecode_association'], ['continuous_mapping_estimate'])
        self.assertEqual(self.catalog.list(epoch='2')['items'], [])
        with self.assertRaisesRegex(CatalogError, 'requires_epoch'):
            self.catalog.list(time_start_us='1')
        self.store.set_lock(item['recording_id'], True, '0')
        self.catalog.path.unlink(); self.catalog.initialize(); self.store.recover()
        recovered = self.catalog.list(epoch=epoch)['items'][0]
        self.assertEqual(recovered['time_tags'], item['time_tags'])
        self.assertTrue(recovered['locked'])

    def test_epoch_change_is_endpoint_only_never_cross_epoch_interpolation(self):
        item, epoch, source = self.mapped_segment(changed=True)
        self.assertEqual(self.catalog.list(epoch=epoch, time_start_us=str(source + 500000))['items'], [])
        self.assertEqual(len(self.catalog.list(epoch='2', time_start_us='100000', time_end_us='101000')['items']), 1)
        self.assertEqual(self.catalog.list()['items'][0]['timecode_association'], ['endpoint_estimate'])
        self.assertEqual(item['time_tags']['start']['source_epoch'], epoch)
        self.assertEqual(item['time_tags']['end']['source_epoch'], '2')

    def test_loss_has_explicit_holdover_end_tag_and_no_covered_interval(self):
        item, epoch, source = self.mapped_segment(lost=True)
        self.assertFalse(item['time_tags']['end']['source_valid'])
        self.assertEqual(item['time_tags']['end']['reason'], 'source_lost_holdover')
        self.assertEqual(self.catalog.list(epoch=epoch, time_start_us=str(source + 500000), time_end_us=str(source + 600000))['items'], [])
        self.assertIn('holdover_endpoint', self.catalog.list(epoch=epoch)['items'][0]['timecode_association'])

    def test_wrong_boot_or_event_cannot_be_associated(self):
        item, _, _ = self.mapped_segment()
        other = json.loads(json.dumps(item))
        other['time_tags']['start']['rk_boot_id'] = 'other-boot'
        with self.assertRaises(CatalogError): self.catalog.import_metadata(other)
        other = json.loads(json.dumps(item))
        other['create_monotonic_us'] = '1501'
        with self.assertRaises(CatalogError): self.catalog.import_metadata(other)
        self.assertEqual(json.loads(self.catalog.get(item['recording_id'])['metadata'])['time_tags'], item['time_tags'])

    def legacy_catalog(self):
        item = self.segment()
        self.store.set_lock(item['recording_id'], True, '0')
        with sqlite3.connect(str(self.catalog.path)) as db:
            db.execute('DROP TABLE time_index'); db.execute('PRAGMA user_version=1')
        return item

    def test_explicit_migration_preserves_legacy_metadata_lock_and_checked_backup(self):
        item = self.legacy_catalog()
        before = self.catalog.get(item['recording_id'])['metadata']
        self.assertEqual(len(self.catalog.list()['items']), 1)
        with self.assertRaisesRegex(CatalogError, 'upgrade_required'): self.catalog.initialize()
        backup = self.base / 'schema1.sqlite'
        self.assertTrue(self.catalog.migrate_v1(backup)['migrated'])
        self.catalog.initialize()
        self.assertEqual(self.catalog.get(item['recording_id'])['metadata'], before)
        self.assertEqual(self.catalog.list(epoch='1')['items'], [])
        with sqlite3.connect(str(backup)) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 1)
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertEqual(db.execute('SELECT locked,metadata FROM recordings').fetchone(), (1, before))

    def test_failed_migration_rolls_back_and_backup_is_never_overwritten(self):
        item = self.legacy_catalog()
        occupied = self.base / 'existing'; occupied.write_text('preserve')
        with self.assertRaises(FileExistsError): self.catalog.migrate_v1(occupied)
        self.assertEqual(occupied.read_text(), 'preserve')
        backup = self.base / 'failure-backup.sqlite'
        with patch('ss07.catalog.TIME_LOOKUP_SQL', 'INVALID SQL'), self.assertRaises(sqlite3.Error):
            self.catalog.migrate_v1(backup)
        with sqlite3.connect(str(self.catalog.path)) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 1)
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='time_index'").fetchone())
        self.assertEqual(self.catalog.get(item['recording_id'])['locked'], 1)
        self.assertTrue(backup.exists())
        self.assertTrue(self.catalog.migrate_v1(self.base / 'retry-backup.sqlite')['migrated'])
