"""Full archive storage tests use explicit synthetic volumes, never /data."""
import copy
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from ss07.catalog import RecordConflict
from ss07.sonar_archive import Catalog, Store, Writer, inspect_file
from ss07.volume import DirectoryVolume, VolumeError, open_directory
from test_sonar import measurement, tagged


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.disk=self.root/'disk';self.disk.mkdir()
        self.volume=DirectoryVolume(open_directory(self.disk),'test-volume','synthetic_test');self.addCleanup(self.volume.close)
        self.catalog=Catalog(self.root/'index.sqlite');self.catalog.initialize()
        self.store=Store(self.catalog,self.volume)
        self.writer=Writer(self.catalog,self.volume);self.addCleanup(self.writer.abort)

    def sample(self,n=1): return tagged(measurement(n,received=n*1000))

    def finish(self):
        self.writer.append([self.sample()]);return self.writer.finish()

    def test_roundtrip_preserves_every_tagged_event_and_checksum(self):
        samples=[self.sample(i) for i in range(1,20)]
        self.writer.append(samples);meta=self.writer.finish()
        self.assertEqual(meta['event_count'],19);self.assertEqual(meta['evidence_kind'],'synthetic_test')
        found=[];fd,data=self.store.open(meta['archive_id'],lambda i,s:found.append(s))
        os.close(fd)
        self.assertEqual(found,samples);self.assertEqual(data,meta)
        self.assertFalse(meta['contains_gap']);self.assertEqual(meta['known_lost_events'],'0')
        self.assertNotIn('relative_path',self.catalog.list()['items'][0])

    def test_rotation_by_count_and_mapping_keeps_all_events(self):
        with patch('ss07.sonar_archive.MAX_PART_EVENTS',3):
            self.writer.append([self.sample(i) for i in range(1,9)]);self.writer.finish()
            parts=self.catalog.list()['items'];self.assertEqual(sorted(p['event_count'] for p in parts),[2,3,3])
        recovered=[]
        for part in parts:
            fd,_=self.store.open(part['archive_id'],lambda i,s:recovered.append(s['seq']));os.close(fd)
        self.assertEqual(sorted(recovered,key=int),[str(i) for i in range(1,9)])

    def test_delayed_u64_epoch_records_are_indexed_without_rounding(self):
        from ss07.timecode_model import Mapper
        from ss07.timecode_wire import Packet
        mapper=Mapper('boot-test','receiver-test','synthetic_test')
        epoch,external=2**63+7,2**63+9000
        mapper.observe(Packet(epoch,1,external),1000,1000)
        sample=measurement(1,received=1100)
        sample['time_tag']=mapper.tag(1100,1200,'sonar_receive','boot-test')
        self.writer.append([sample]);self.writer.append([self.sample(2)]);self.writer.finish()
        self.assertEqual(len(self.catalog.list()['items']),2)
        part=self.catalog.list(source_epoch=str(epoch),start_us=str(external+100),end_us=str(external+100))['items'][0]
        self.assertEqual(part['time_start_us'],str(external+100))
        self.assertEqual(self.catalog.list(source_epoch=str(epoch+1))['items'],[])
        with self.assertRaises(ValueError):self.catalog.list(start_us='1')

    def test_aborted_or_truncated_file_is_locked_incomplete_not_completed(self):
        self.writer.append([self.sample()]);relative=self.writer.relative;self.writer.abort()
        with (self.disk/relative).open('ab') as stream:stream.write(b'{"interrupted":')
        result=self.store.recover();self.assertEqual(result,dict(recovered=1,failures=0))
        part=self.catalog.list()['items'][0]
        self.assertEqual(part['state'],'INCOMPLETE');self.assertTrue(part['locked']);self.assertTrue(part['contains_gap'])
        self.assertEqual(part['event_count'],1)
        with self.assertRaises(VolumeError):self.store.open(part['archive_id'])

    def test_index_rebuild_preserves_lock_and_original_tags(self):
        meta=self.finish();locked=self.store.set_lock(meta['archive_id'],True,'0')
        with self.assertRaises(RecordConflict):self.store.set_lock(meta['archive_id'],False,'0')
        rebuilt=Catalog(self.root/'rebuilt.sqlite');rebuilt.initialize()
        report=Store(rebuilt,self.volume).recover();self.assertEqual(report['failures'],0)
        self.assertTrue(rebuilt.get(meta['archive_id'])['locked'])
        fd,_=Store(rebuilt,self.volume).open(meta['archive_id']);os.close(fd)
        self.assertEqual(locked['lock_revision'],'1')

    def test_modified_content_cannot_be_read_or_pruned(self):
        meta=self.finish();path=self.disk/meta['relative_path']
        raw=path.read_bytes();self.assertIn(b'1234',raw)
        path.write_bytes(raw.replace(b'1234',b'1235'))
        with self.assertRaises((ValueError,VolumeError)):self.store.open(meta['archive_id'])
        with patch.object(self.volume,'space',return_value=(0,100)):
            result=self.store.prune(10)
        self.assertEqual(result['removed'],0);self.assertEqual(result['failures'],1);self.assertTrue(path.exists())

    def test_recovery_marks_corruption_invalid_and_a_missing_file_missing(self):
        first=self.finish();path=self.disk/first['relative_path'];raw=path.read_bytes()
        path.write_bytes(raw.replace(b'1234',b'1235'))
        self.assertEqual(self.store.recover()['failures'],1)
        self.assertEqual(self.catalog.get(first['archive_id'])['state'],'INVALID')
        self.writer.append([self.sample(2)]);second=self.writer.finish()
        (self.disk/second['relative_path']).unlink()
        self.store.recover()
        self.assertEqual(self.catalog.get(second['archive_id'])['state'],'MISSING')

    def test_low_space_stops_at_locked_and_never_deletes_incomplete(self):
        meta=self.finish();self.store.set_lock(meta['archive_id'],True,'0')
        self.writer.append([self.sample(2)]);self.writer.abort();self.store.recover()
        with patch.object(self.volume,'space',return_value=(0,100)):
            result=self.store.prune(10)
        self.assertFalse(result['space_available']);self.assertEqual(result['removed'],0)
        self.assertEqual(len(list(self.disk.rglob('*.jsonl'))),2)

    def test_deletion_intent_recovers_after_unlink_failure(self):
        meta=self.finish();original=self.volume.unlink
        def interrupted(relative):
            if relative.endswith('.json'):raise OSError('test interruption after data unlink')
            return original(relative)
        with patch.object(self.volume,'space',return_value=(0,100)),patch.object(self.volume,'unlink',side_effect=interrupted):
            self.assertEqual(self.store.prune(10)['failures'],1)
        self.assertEqual(self.catalog.get(meta['archive_id'])['state'],'DELETING')
        self.assertEqual(self.store.recover()['failures'],0)
        self.assertEqual(self.catalog.get(meta['archive_id'])['state'],'DELETED')
        self.assertEqual(self.catalog.list()['items'],[])

    def test_volume_replacement_never_redirects_write_into_new_directory(self):
        self.writer.append([self.sample()])
        moved=self.root/'removed-disk';self.disk.rename(moved);self.disk.mkdir()
        # A pinned synthetic descriptor proves addressing; production MountLease
        # additionally detects the namespace replacement and refuses writes.
        self.writer.append([self.sample(2)]);self.writer.finish()
        self.assertEqual(list(self.disk.iterdir()),[])
        self.assertEqual(len(list(moved.rglob('*.jsonl'))),1)
        self.writer.append([self.sample(3)])
        with patch.object(self.volume,'valid',return_value=False):
            with self.assertRaises(VolumeError):self.writer.append([self.sample(4)])

    def test_wrong_volume_evidence_hardlinks_symlinks_and_unknown_schema_refused(self):
        sample=self.sample();sample['evidence_kind']='device_observation'
        with self.assertRaises(ValueError):self.writer.append([sample])
        meta=self.finish();file=self.disk/meta['relative_path'];os.link(file,file.with_suffix('.link'))
        with self.assertRaises(VolumeError):self.store.open(meta['archive_id'])
        file.with_suffix('.link').unlink();file.rename(file.with_suffix('.saved'));file.symlink_to(file.with_suffix('.saved'))
        with self.assertRaises(OSError):self.store.open(meta['archive_id'])
        with sqlite3.connect(str(self.catalog.path)) as db:db.execute('PRAGMA user_version=99')
        with self.assertRaises(sqlite3.DatabaseError):self.catalog.list()

    def test_catalog_read_only_pagination_and_bounds(self):
        for i in range(1,5):self.writer.append([self.sample(i)]);self.writer.finish()
        first=self.catalog.list(limit=2);second=self.catalog.list(cursor=first['next_cursor'])
        self.assertEqual(len(first['items'])+len(second['items']),4)
        self.assertEqual(len({p['archive_id'] for p in first['items']+second['items']}),4)
        with self.catalog.connection() as db:
            with self.assertRaises(sqlite3.OperationalError):db.execute('DELETE FROM parts')
        for kwargs in ({'limit':201},{'cursor':'../file'},{'direction':'unknown'}):
            with self.assertRaises(ValueError):self.catalog.list(**kwargs)


if __name__=='__main__':unittest.main()
