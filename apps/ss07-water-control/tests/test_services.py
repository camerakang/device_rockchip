import copy
import json
import os
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import urlopen

from ss07.api import Server
from ss07.config import ROOT, ConfigError, check_schema, load, revision, validate
from ss07.devices import check_cameras, inventory
from ss07.persistence import ConfigStore, RevisionConflict
from ss07.snapshots import SnapshotReader, publish, u64
from ss07.state import State, camera_status
from ss07.storage import inspect_storage


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'system.json'
        self.config = load(ROOT / 'config/system.example.json')
        self.store = ConfigStore(self.path)
        self.initial = self.store.initialize(self.config)

    def test_conflict_backup_and_pending_restart(self):
        state = State(self.config, config_path=self.path)
        candidate = copy.deepcopy(self.config)
        candidate['device_id'] = 'ss07-test'
        new_revision = self.store.update(candidate, self.initial)
        self.assertEqual(load(self.store.backup), self.config)
        self.assertEqual(revision(load(self.path)), new_revision)
        with self.assertRaises(RevisionConflict):
            self.store.update(self.config, self.initial)
        result = state.configuration()
        self.assertTrue(result['pending_restart'])
        self.assertEqual(result['loaded_revision'], self.initial)
        self.assertEqual(state.snapshot()['device_id'], self.config['device_id'])

    def test_replace_failure_keeps_previous_configuration(self):
        candidate = copy.deepcopy(self.config)
        candidate['device_id'] = 'replacement'
        original = os.replace
        def fail(source, target):
            if target == str(self.path):
                raise OSError('injected disk failure')
            original(source, target)
        with patch('ss07.persistence.os.replace', side_effect=fail), self.assertRaises(OSError):
            self.store.update(candidate, self.initial)
        self.assertEqual(load(self.path), self.config)
        self.assertEqual(list(self.path.parent.glob('.system.json-*')), [])

    def test_concurrent_expected_revision_allows_one_writer(self):
        barrier, outcomes = threading.Barrier(2), []
        def writer(name):
            candidate = copy.deepcopy(self.config)
            candidate['device_id'] = name
            barrier.wait()
            try:
                ConfigStore(self.path).update(candidate, self.initial)
                outcomes.append('saved')
            except RevisionConflict:
                outcomes.append('conflict')
        threads = [threading.Thread(target=writer, args=(name,)) for name in ('one', 'two')]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=5)
        self.assertCountEqual(outcomes, ['saved', 'conflict'])

    def test_no_overwrite_and_no_symlink(self):
        with self.assertRaises(RevisionConflict): self.store.initialize(self.config)
        link = self.path.parent / 'link.json'
        link.symlink_to(self.path)
        with self.assertRaises(ConfigError):
            ConfigStore(link).update(self.config, self.initial)

    def test_invalid_disk_config_does_not_replace_runtime(self):
        state = State(self.config, config_path=self.path)
        self.path.write_text('{broken')
        self.assertIsNone(state.configuration()['persisted'])
        self.assertIsNotNone(state.configuration()['error'])
        self.assertEqual(state.snapshot()['device_id'], self.config['device_id'])

    def test_strict_schema_and_url(self):
        with self.assertRaises(ConfigError): check_schema(1, {'type': 'integer', 'multipleOf': 2})
        for value in ('rtsp://host:70000/live/av0', 'rtsp://host:abc/live/av0'):
            candidate = copy.deepcopy(self.config)
            candidate['sdi']['rtsp_url'] = value
            with self.assertRaises(ConfigError): validate(candidate)
        self.config['cameras']['front']['binding'] = '/dev/v4l/by-path/..'
        with self.assertRaises(ConfigError): validate(self.config)


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = 1_000_000
        self.reader = SnapshotReader(self.tmp.name, 'test-boot', clock=lambda: self.now)
        self.sample = dict(schema_version=1, service='camera.front', instance_id='worker-one',
                           boot_id='test-boot', seq='1', observed_monotonic_us='1000000',
                           state='RUNNING', evidence_kind='device_observation', payload={})

    def write(self): publish(self.tmp.name, self.sample)

    def test_duplicate_does_not_refresh_age_and_stale_hides_payload(self):
        self.write()
        self.assertEqual(self.reader.read('camera.front')['state'], 'RUNNING')
        self.now += 2_000_000
        self.write()
        self.assertEqual(self.reader.read('camera.front')['age_ms'], 2000)
        self.now += 1_000_000
        status = self.reader.read('camera.front')
        self.assertEqual(status['state'], 'STALE')
        self.assertNotIn('payload', status)

    def test_boot_future_replay_and_restart(self):
        self.sample['boot_id'] = 'old-boot'
        self.write()
        self.assertEqual(self.reader.read('camera.front')['reason'], 'boot_mismatch')
        self.sample['boot_id'] = 'test-boot'
        self.sample['observed_monotonic_us'] = '1000001'
        self.write()
        self.assertEqual(self.reader.read('camera.front')['state'], 'ERROR')
        self.sample['observed_monotonic_us'] = '1000000'
        self.write()
        self.assertEqual(self.reader.read('camera.front')['state'], 'RUNNING')
        self.sample['payload'] = {'changed': True}
        self.write()
        self.assertEqual(self.reader.read('camera.front')['state'], 'ERROR')
        self.now += 10
        self.sample.update(instance_id='worker-two', observed_monotonic_us=str(self.now), payload={})
        self.write()
        self.assertEqual(self.reader.read('camera.front')['instance_id'], 'worker-two')

    def test_reject_symlink_and_oversized_files(self):
        target = Path(self.tmp.name) / 'camera.front.json'
        target.symlink_to('/dev/zero')
        self.assertEqual(self.reader.read('camera.front')['state'], 'ERROR')
        target.unlink()
        target.write_bytes(b' ' * 65537)
        self.assertEqual(self.reader.read('camera.front')['state'], 'ERROR')

    def test_synthetic_evidence_rejected(self):
        self.sample['evidence_kind'] = 'simulated'
        with self.assertRaises(ConfigError): self.write()
        for value in ('-1', '01', str(2**64), 42):
            with self.assertRaises(ConfigError): u64(value)

    def test_heartbeat_does_not_make_old_frame_healthy(self):
        worker = dict(state='RUNNING', reason=None, payload=dict(capture_state='STREAMING',
            publish_state='PUBLISHING', format='H264', width=1920, height=1080,
            fps_observed=25, bitrate_observed=None, last_frame_monotonic_us='1000000',
            restart_count=0, error=None))
        self.assertEqual(camera_status('front', worker, 1_100_000)['availability'], 'available')
        stale = camera_status('front', worker, 4_000_000)
        self.assertEqual(stale['capture_state'], 'STALE')
        self.assertEqual(stale['availability'], 'unavailable')
        worker['payload']['last_frame_monotonic_us'] = None
        self.assertEqual(camera_status('front', worker, 4_000_000)['capture_state'], 'ERROR')


class DeviceTests(unittest.TestCase):
    def test_saved_real_inventory_identity_and_mode(self):
        report = json.loads((ROOT / 'docs/evidence/rk3588-192.168.3.233-probe.json').read_text())
        normalized = inventory(report)
        self.assertEqual(len(normalized['cameras']), 5)
        self.assertTrue(all(d['identity_strategy'] == 'by-path' for d in normalized['cameras']))
        self.assertTrue(all(d['role'] == 'camera_interface' for d in normalized['serial_devices']))
        config = load(ROOT / 'config/system.example.json')
        camera = normalized['cameras'][0]
        config['cameras']['front'] = dict(binding=camera['by_path'][0],
             mode=dict(fourcc='H264', width=1920, height=1080, fps_num=25, fps_den=1))
        self.assertEqual(check_cameras(config, report)['channels'][0]['state'], 'ENUMERATED')
        config['cameras']['front']['mode']['fps_num'] = 500
        self.assertEqual(check_cameras(config, report)['channels'][0]['state'], 'UNSUPPORTED_MODE')
        config['cameras']['front']['binding'] = '/dev/v4l/by-id/usb-webcamvendor_webcamproduct_00000000-video-index0'
        self.assertEqual(check_cameras(config, report)['channels'][0]['state'], 'AMBIGUOUS_IDENTITY')


class StorageTests(unittest.TestCase):
    def test_mount_missing_readonly_systemdisk_and_eligible(self):
        cfg = load(ROOT / 'config/system.example.json')['storage']
        root = '1 0 8:1 / / rw - ext4 /dev/root rw\n'
        self.assertEqual(inspect_storage(cfg, root)['gate'], 'MOUNT_MISSING')
        same = root + '2 1 8:1 /data /data/ss07 rw - ext4 /dev/root rw\n'
        self.assertEqual(inspect_storage(cfg, same)['gate'], 'SYSTEM_FILESYSTEM')
        readonly = root + '2 1 8:2 / /data/ss07 ro - ext4 /dev/sda ro\n'
        self.assertEqual(inspect_storage(cfg, readonly)['gate'], 'READ_ONLY')
        separate = root + '2 1 8:2 / /data/ss07 rw - ext4 /dev/sda rw\n'
        with patch('ss07.storage.os.statvfs') as usage, patch('ss07.storage.os.access', return_value=True):
            usage.return_value.f_bavail = 100
            usage.return_value.f_frsize = 4096
            result = inspect_storage(cfg, separate)
        self.assertEqual(result['gate'], 'ELIGIBLE')
        self.assertEqual(result['free_bytes'], '409600')
        self.assertFalse(result['recording_enabled'])


class EventTests(unittest.TestCase):
    def setUp(self):
        self.server = Server(('127.0.0.1', 0), State(load(ROOT / 'config/system.example.json')))
        self.server.event_interval = .03
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.base = 'http://127.0.0.1:' + str(self.server.server_port)
        self.clients = []

    def tearDown(self):
        self.server.shutdown()
        for client in self.clients: client.close()
        self.server.server_close()
        self.thread.join()

    def test_events_snapshot_client_limit_and_http_availability(self):
        for _ in range(4):
            response = urlopen(self.base + '/api/v1/events', timeout=2)
            self.clients.append(response)
            self.assertEqual(response.headers.get_content_type(), 'text/event-stream')
            lines = [response.readline() for _ in range(5)]
            data = json.loads(next(line[6:] for line in lines if line.startswith(b'data: ')))
            self.assertEqual(data['data']['timecode']['quality'], 'UNAVAILABLE')
        with self.assertRaises(HTTPError) as ctx:
            urlopen(self.base + '/api/v1/events', timeout=2)
        self.assertEqual(ctx.exception.code, 503)
        with urlopen(self.base + '/healthz', timeout=2) as response:
            self.assertFalse(json.load(response)['data']['hardware_ready'])

    def test_static_assets_and_path_confinement(self):
        with urlopen(self.base + '/', timeout=2) as response:
            self.assertIn('视频与声呐', response.read().decode())
            self.assertIn("frame-ancestors 'none'", response.headers['Content-Security-Policy'])
        with self.assertRaises(HTTPError) as ctx:
            urlopen(self.base + '/../config/system.example.json', timeout=2)
        self.assertEqual(ctx.exception.code, 404)


if __name__ == '__main__': unittest.main()
