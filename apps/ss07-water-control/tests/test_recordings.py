"""Synthetic container fixtures validate storage semantics, not video decoding."""
import http.client
import json
import os
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from ss07.api import Server
from ss07.catalog import Catalog, CatalogError, RecordConflict, RecordStore, recording_id, utc
from ss07.config import ConfigError, ROOT, load, validate
from ss07.mp4 import InvalidMP4, inspect as inspect_mp4
from ss07.recording_http import byte_range
from ss07.recorder import RecorderSession, duration_ms, render
from ss07.state import State, storage_status
from ss07.volume import DirectoryVolume, MountLease, VolumeError, open_directory


def box(kind, payload):
    return struct.pack('>I4s', len(payload) + 8, kind) + payload


CONTAINER = box(b'ftyp', b'isom\x00\x00\x02\x00') + box(b'moov', b'fixture') + box(b'moof', b'fixture') + box(b'mdat', b'not-video-fixture')


class RecordingFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.root = self.base / 'recordings'; self.root.mkdir()
        self.volume = DirectoryVolume(open_directory(self.root), 'test-volume', 'synthetic_test')
        self.catalog = Catalog(self.base / 'catalog.sqlite'); self.catalog.initialize()
        self.store = RecordStore(self.catalog, self.volume)
        self.session = dict(schema_version=1, session_id=uuid.uuid4().hex, boot_id='test-boot',
                            volume_uuid=self.volume.volume_uuid, evidence_kind='synthetic_test')
        for channel in ('front', 'back', 'left', 'right', 'down', 'sdi'):
            with self.volume.directory(self.session['session_id'] + '/' + channel, create=True):
                pass
        self.volume.write_json(self.session['session_id'] + '/session.json', self.session)
        self.counter = 0

    def tearDown(self):
        self.volume.close()
        self.tmp.cleanup()

    def segment(self, channel='front', complete=True, start=None, content=CONTAINER):
        self.counter += 1
        start = start or datetime.now(timezone.utc).replace(microsecond=self.counter)
        name = start.strftime('%Y-%m-%d_%H-%M-%S-%f') + '.mp4'
        relative = self.session['session_id'] + '/' + channel + '/' + name
        (self.root / relative).write_bytes(content)
        if complete:
            return self.store.finalize(relative, self.session, 2000, '100', '200')
        return relative


class CatalogTests(RecordingFixture):
    def test_native_hook_float_duration_rounding_and_bounds(self):
        for text, expected in (('1.1400000000000001', 1140), ('1.9999999999999998', 2000),
                               ('2.0399999999999996', 2040), ('1.23456789', 1235),
                               ('0.001', 1), ('86400', 86400000)):
            self.assertEqual(duration_ms(text), expected)
        for text in ('0', '0.0001', '-1', '86400.0001', 'NaN', 'inf', '1e99999', '1' * 1000, None):
            with self.assertRaises(CatalogError): duration_ms(text)

    def test_catalog_size_limit_survives_reopening(self):
        for _ in range(2):
            with self.catalog.connection(writable=True) as db:
                pages = db.execute('PRAGMA max_page_count').fetchone()[0]
                page_size = db.execute('PRAGMA page_size').fetchone()[0]
                self.assertLessEqual(pages * page_size, 1073741824)

    def test_unsafe_oldest_record_does_not_starve_later_cleanup(self):
        unsafe = self.segment(); ordinary = self.segment('back')
        (self.root / (unsafe['relative_path'] + '.json')).unlink()
        self.assertEqual(self.store.cleanup(2**62, batch=1)['refused'], 1)
        self.assertEqual(self.store.cleanup(2**62, batch=1)['deleted'], 1)
        self.assertTrue((self.root / unsafe['relative_path']).exists())
        self.assertFalse((self.root / ordinary['relative_path']).exists())

    def test_completed_index_pagination_filter_and_no_path_in_api(self):
        one = self.segment('front'); two = self.segment('back'); three = self.segment('front')
        first = self.catalog.list(limit=2)
        self.assertEqual([r['recording_id'] for r in first['items']], [three['recording_id'], two['recording_id']])
        second = self.catalog.list(limit=2, cursor=first['next_cursor'])
        self.assertEqual(second['items'][0]['recording_id'], one['recording_id'])
        self.assertIsNone(second['next_cursor'])
        self.assertEqual(len(self.catalog.list(channel='front')['items']), 2)
        for row in first['items']:
            self.assertNotIn('relative_path', row)
            self.assertEqual(row['evidence_kind'], 'synthetic_test')
            self.assertEqual(row['verification'], 'container_structure_only')
            self.assertEqual(row['time_tags'], {'start': None, 'end': None})
        with self.assertRaises(CatalogError):
            self.catalog.list(limit=100000)
        with self.assertRaises(CatalogError):
            self.catalog.list(start='2026-10-23T12:00:00')

    def test_lock_cas_and_space_cleanup_preserve_locked_file(self):
        protected = self.segment(); ordinary = self.segment('back')
        value = self.store.set_lock(protected['recording_id'], True, '0')
        self.assertTrue(value['locked']); self.assertEqual(value['lock_revision'], '1')
        with self.assertRaises(RecordConflict):
            self.store.set_lock(protected['recording_id'], False, '0')
        self.assertEqual(self.store.cleanup(2**62)['deleted'], 1)
        self.assertTrue((self.root / protected['relative_path']).exists())
        self.assertFalse((self.root / ordinary['relative_path']).exists())
        self.assertEqual(self.catalog.get(ordinary['recording_id'])['state'], 'DELETED')
        self.store.set_lock(protected['recording_id'], False, '1')
        self.assertEqual(self.store.cleanup(2**62)['deleted'], 1)

    def test_sidecar_lock_survives_database_failure_and_recovery(self):
        item = self.segment()
        with patch.object(Catalog, '_upsert', side_effect=sqlite3.OperationalError('injected disk full')):
            with self.assertRaises(sqlite3.Error):
                self.store.set_lock(item['recording_id'], True, '0')
        self.assertEqual(self.catalog.get(item['recording_id'])['locked'], 0)
        self.assertEqual(self.store.cleanup(2**62)['deleted'], 0)
        self.assertEqual(self.store.recover()['restored'], 1)
        self.assertEqual(self.catalog.get(item['recording_id'])['locked'], 1)

    def test_lost_catalog_rebuilds_completed_sidecars_and_preserves_lock(self):
        item = self.segment()
        self.store.set_lock(item['recording_id'], True, '0')
        self.catalog.path.unlink()
        self.catalog.initialize()
        self.assertEqual(self.store.recover()['restored'], 1)
        row = self.catalog.get(item['recording_id'])
        self.assertEqual(row['state'], 'COMPLETE')
        self.assertEqual(row['locked'], 1)
        fd, meta = self.store.open_recording(item['recording_id'])
        with os.fdopen(fd, 'rb') as stream:
            self.assertEqual(stream.read(), CONTAINER)

    def test_orphan_and_truncated_segments_are_never_complete_or_cleaned(self):
        relative = self.segment(complete=False, content=CONTAINER[:-3])
        with self.assertRaises(InvalidMP4):
            self.store.finalize(relative, self.session, 2000, '100', '200')
        self.assertEqual(self.catalog.list()['items'], [])
        self.assertEqual(self.store.recover()['incomplete'], 1)
        record_id = recording_id(self.volume.volume_uuid, relative)
        self.assertEqual(self.catalog.get(record_id)['state'], 'INCOMPLETE')
        self.assertEqual(self.catalog.get(record_id)['locked'], 1)
        with self.assertRaises(VolumeError):
            self.store.open_recording(record_id)
        self.assertEqual(self.store.cleanup(2**62)['deleted'], 0)

    def test_duplicate_completion_does_not_reset_lock_or_time(self):
        item = self.segment()
        self.store.set_lock(item['recording_id'], True, '0')
        duplicate = self.store.finalize(item['relative_path'], self.session, 4000, '300', '500')
        self.assertTrue(duplicate['locked'])
        self.assertEqual(duplicate['duration_ms'], 2000)
        self.assertEqual(duplicate['complete_monotonic_us'], '200')

    def test_recovery_reports_missing_and_changed_files(self):
        missing = self.segment('front'); changed = self.segment('back')
        (self.root / missing['relative_path']).unlink()
        with (self.root / changed['relative_path']).open('ab') as stream:
            stream.write(b'changed')
        self.store.recover()
        self.assertEqual(self.catalog.get(missing['recording_id'])['state'], 'MISSING')
        self.assertEqual(self.catalog.get(changed['recording_id'])['state'], 'INVALID')

    def test_retention_deletes_only_old_unlocked_finalized_files(self):
        old = self.segment(start=datetime.now(timezone.utc) - timedelta(days=10))
        recent = self.segment('back')
        self.assertEqual(self.store.cleanup(0, retention_days=7)['deleted'], 1)
        self.assertFalse((self.root / old['relative_path']).exists())
        self.assertTrue((self.root / recent['relative_path']).exists())


class VolumeTests(RecordingFixture):
    def test_native_child_terminates_when_supervisor_is_killed(self):
        script = '''import os,subprocess,sys,time
from pathlib import Path
child=subprocess.Popen([sys.executable,sys.argv[1],str(os.getpid()),'/bin/sleep','30'])
deadline=time.monotonic()+3
while time.monotonic()<deadline:
    if Path('/proc/%d/comm'%child.pid).read_text().strip()=='sleep':break
    time.sleep(.01)
else:raise RuntimeError('child failed to exec')
print(child.pid,flush=True)
time.sleep(30)
'''
        parent = subprocess.Popen([sys.executable, '-c', script, str(ROOT / 'tools/ss07-record-child.py')],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        child = None
        try:
            import selectors
            with selectors.DefaultSelector() as selector:
                selector.register(parent.stdout, selectors.EVENT_READ)
                self.assertTrue(selector.select(timeout=5), 'child startup timeout')
            child = int(parent.stdout.readline())
            parent.kill(); parent.communicate(timeout=3)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                try:
                    state = Path('/proc/%d/stat' % child).read_text().split()[2]
                except FileNotFoundError:
                    child = None
                    return
                if state == 'Z':
                    child = None
                    return
                time.sleep(.02)
            self.fail('native child survived supervisor death')
        finally:
            if parent.poll() is None:
                parent.kill()
            parent.communicate(timeout=3)
            if child is not None:
                try:
                    os.kill(child, 9)
                except ProcessLookupError:
                    pass

    def test_readonly_api_can_observe_writer_but_stale_or_wrong_volume_cannot(self):
        config = load(ROOT / 'config/system.example.json')['storage']
        config.update(recording_enabled=True, filesystem_uuid='test-volume')
        worker = dict(state='RUNNING', payload=dict(volume_uuid='test-volume', gate='RECORDING',
            recording_enabled=True, recording_active=True, channels_recording=['front']))
        def lease(*_args, **_kwargs):
            return DirectoryVolume(open_directory(self.root), 'test-volume', 'synthetic_test')
        readonly = dict(mount_present=True, writable=False, free_bytes=None,
                        recording_enabled=False, state='NOT_IMPLEMENTED', gate='READ_ONLY')
        with patch('ss07.state.inspect_storage', return_value=readonly), patch('ss07.state.MountLease.acquire', side_effect=lease):
            status = storage_status(config, worker)
            self.assertTrue(status['recording_active']); self.assertFalse(status['observer_writable'])
            worker['state'] = 'STALE'
            self.assertFalse(storage_status(config, worker)['recording_active'])
            worker['state'] = 'RUNNING'; worker['payload']['volume_uuid'] = 'other-volume'
            self.assertFalse(storage_status(config, worker)['recording_active'])
        worker['payload']['volume_uuid'] = 'test-volume'
        with patch('ss07.state.MountLease.acquire', side_effect=VolumeError('unmounted')):
            self.assertFalse(storage_status(config, worker)['recording_enabled'])

    def test_open_file_without_advancing_bytes_becomes_inactive(self):
        relative = self.segment(complete=False)
        recorder = RecorderSession(load(ROOT / 'config/system.example.json'), self.catalog,
                                   self.volume, '/unused-binary', 'test-boot')
        recorder.pending[relative] = '1'
        with patch('ss07.recorder.monotonic_us', return_value=1000000):
            self.assertTrue(recorder.status()['recording_active'])
        with patch('ss07.recorder.monotonic_us', return_value=4000000):
            self.assertFalse(recorder.status()['recording_active'])

    def test_no_system_disk_override_and_no_creation_before_identity_gate(self):
        cfg = load(ROOT / 'config/system.example.json')['storage']
        with patch('ss07.volume.os.mkdir') as mkdir:
            with self.assertRaisesRegex(VolumeError, 'uuid_unconfigured'):
                MountLease.acquire(cfg, writable=True)
            mkdir.assert_not_called()
        cfg['filesystem_uuid'] = 'any-uuid'
        entries = [dict(id=1, device='8:1', target='/', root='/', options=[]),
                   dict(id=2, device='8:1', target='/data/ss07', root='/', options=[])]
        with patch('ss07.volume.mount_entries', return_value=entries), patch('ss07.volume.os.mkdir') as mkdir:
            with self.assertRaisesRegex(VolumeError, 'system_or_bind'):
                MountLease.acquire(cfg, writable=True)
            mkdir.assert_not_called()

    def test_fd_survives_path_replacement_without_writing_replacement_directory(self):
        original = self.base / 'detached'
        self.root.rename(original); self.root.mkdir()
        self.volume.write_json('pinned.json', {'evidence_kind': 'synthetic_test'})
        self.assertTrue((original / 'pinned.json').exists())
        self.assertFalse((self.root / 'pinned.json').exists())

    def test_symlink_hardlink_fifo_and_parent_traversal_rejected(self):
        outside = self.base / 'outside'; outside.write_bytes(b'private')
        (self.root / 'link').symlink_to(outside)
        os.link(outside, self.root / 'hardlink')
        os.mkfifo(self.root / 'fifo')
        for name in ('link', 'hardlink', 'fifo', '../outside', '/etc/passwd'):
            with self.subTest(name=name), self.assertRaises(OSError):
                self.volume.open(name)
        (self.root / 'linked-directory').symlink_to(self.base)
        with self.assertRaises(OSError):
            self.volume.write_json('linked-directory/outside', {})
        self.assertEqual(outside.read_bytes(), b'private')

    def test_schema_requires_uuid_and_bounded_reserve(self):
        config = load(ROOT / 'config/system.example.json')
        config['storage']['recording_enabled'] = True
        with self.assertRaises(ConfigError):
            validate(config)
        config['storage']['filesystem_uuid'] = 'real-uuid-needed'
        validate(config)
        config['storage']['reserve_bytes'] = '1'
        with self.assertRaises(ConfigError):
            validate(config)

    def test_recorder_has_no_listeners_no_auto_delete_and_uses_inherited_fd(self):
        config = load(ROOT / 'config/system.example.json')
        result = render(config, self.volume.fd, self.session['session_id'])
        for service in ('rtsp', 'webrtc', 'api', 'playback', 'hls'):
            self.assertFalse(result[service])
        self.assertTrue(result['pathDefaults']['recordPath'].startswith('/proc/self/fd/'))
        self.assertEqual(result['pathDefaults']['recordDeleteAfter'], '0s')
        self.assertEqual(len(result['paths']), 6)


class RecordingHTTPTests(RecordingFixture):
    def test_range_parser(self):
        self.assertEqual(byte_range(None, 100), (0, 99, False))
        self.assertEqual(byte_range('bytes=5-', 100), (5, 99, True))
        self.assertEqual(byte_range('bytes=-10', 100), (90, 99, True))
        self.assertEqual(byte_range('bytes=5-999', 100), (5, 99, True))
        for value in ('bytes=100-', 'bytes=-0', 'bytes=20-19', 'bytes=1-2,4-5', 'bytes=-', 'other=1-2'):
            with self.assertRaises(ValueError):
                byte_range(value, 100)

    def test_content_range_download_missing_disk_and_path_confinement(self):
        item = self.segment()
        config = load(ROOT / 'config/system.example.json')
        state = State(config, catalog_path=self.catalog.path)
        def volume(*args, **kwargs):
            return DirectoryVolume(open_directory(self.root), 'test-volume', 'synthetic_test')
        with Server(('127.0.0.1', 0), state) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            def request(path, headers=None, method='GET'):
                conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
                conn.request(method, path, headers=headers or {})
                response = conn.getresponse(); raw = response.read()
                result = response.status, dict(response.getheaders()), raw
                conn.close(); return result
            path = '/api/v1/recordings/' + item['recording_id'] + '/content'
            try:
                with patch('ss07.recording_http.MountLease.acquire', side_effect=volume):
                    status, headers, raw = request(path, {'Range': 'bytes=3-15'})
                    self.assertEqual(status, 206); self.assertEqual(raw, CONTAINER[3:16])
                    self.assertEqual(headers['Content-Range'], 'bytes 3-15/' + str(len(CONTAINER)))
                    self.assertEqual(request(path, {'Range': 'bytes=999999-'})[0], 416)
                    self.assertEqual(request(path, {'Range': 'bytes=0-3,5-8'})[0], 416)
                    status, headers, raw = request(path + '?download=1')
                    self.assertEqual(raw, CONTAINER); self.assertIn('attachment', headers['Content-Disposition'])
                    status, headers, raw = request(path, method='HEAD')
                    self.assertEqual(raw, b''); self.assertEqual(int(headers['Content-Length']), len(CONTAINER))
                    data = json.loads(request('/api/v1/recordings?limit=1')[2])['data']
                    self.assertEqual(data['items'][0]['availability'], 'available')
                    self.assertNotIn('relative_path', data['items'][0])
                    self.assertEqual(request('/api/v1/recordings?channel=invalid')[0], 422)
                    self.assertEqual(request('/api/v1/recordings/../../etc/passwd/content')[0], 404)
                with patch('ss07.recording_http.MountLease.acquire', side_effect=VolumeError('mount_missing')):
                    self.assertEqual(request(path)[0], 409)
                    data = json.loads(request('/api/v1/recordings')[2])['data']
                    self.assertEqual(data['items'][0]['availability'], 'disk_offline')
            finally:
                server.shutdown(); thread.join(timeout=4)


if __name__ == '__main__':
    unittest.main()
