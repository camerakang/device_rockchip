"""Test-only codec/transport objects; no manufacturer frames or real distances."""
import copy
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import urlopen

from ss07.api import Server
from ss07.config import ROOT, load
from ss07.config_runtime import fingerprint
from ss07.sonar_core import Reply, Timing
from ss07.sonar_driver import Profile, resolve
from ss07.sonar_journal import Journal
from ss07.sonar_status import inspect
from ss07.sonar_worker import Engine
from ss07.state import State
from test_sonar import measurement, tagged


class Sink:
    def __init__(self): self.events = []
    def submit(self, event): self.events.append(event)
    def status(self):
        return dict(error=None, queued_events=0, dropped_events='0', last_committed_monotonic_us=None, data_disk_archive=False)


class Codec:
    def __init__(self): self.replies = []; self.resets = 0
    def reset(self): self.resets += 1
    def encode(self, request): return b'fixture-command'
    def feed(self, raw): result, self.replies = self.replies, []; return result


def fixture():
    config = load(ROOT / 'config/system.example.json')
    config['sonar'].update(binding='/dev/serial/by-path/test-only-sonar', vendor_protocol='test-only')
    config['sonar']['uart'] = dict(baud=115200, data_bits=8, parity='none', stop_bits=1)
    for d, node in config['sonar']['nodes'].items(): node['address'] = d+'-address'
    addresses = {d: n['address'] for d, n in config['sonar']['nodes'].items()}
    def forbidden_open(config): raise AssertionError('fixture must not open serial hardware')
    profile = Profile('test-only', Timing(10000, 30000, 1000, 1000, 50000, 'wire_id'),
                      lambda c: addresses, Codec, forbidden_open)
    return config, addresses, profile


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.config, self.addresses, self.profile = fixture()
        self.sink = Sink()
        self.engine = Engine(self.addresses, self.profile, 'boot-test', 'collector-test', self.sink, 'synthetic_test')

    def test_production_registry_never_loads_config_strings_as_drivers(self):
        for identity in ('test-only', 'ss07.sonar_worker', '__import__("os")'):
            self.config['sonar']['vendor_protocol'] = identity
            with self.assertRaisesRegex(ValueError, 'sonar_protocol_unavailable'): resolve(self.config)

    def test_driver_canonicalization_rejects_duplicate_addresses_before_open(self):
        profile = Profile('test-only', self.profile.timing, lambda c: dict.fromkeys(self.addresses, 'same'),
                          Codec, self.profile.open_transport)
        with patch('ss07.sonar_driver.PROFILES', {'test-only': profile}):
            with self.assertRaisesRegex(ValueError, 'addresses'): resolve(self.config)

    def test_partial_write_requires_reconnect_without_replaying_command(self):
        engine = self.engine; engine.connection(True, 0)
        class Port:
            def write(_, raw): return len(raw)-1
        with self.assertRaisesRegex(OSError, 'partial_sonar_write'): engine.poll(Port(), lambda: 31000)
        self.assertEqual(self.sink.events[-1]['kind'], 'SEND_FAILED')
        self.assertIsNone(engine.poller.pending)
        engine.connection(False, 32000)
        engine.connection(True, 33000)
        self.assertIsNone(engine.poller.next_request(63999))
        self.assertEqual(engine.poller.next_request(64000).sensor_id, 'back')

    def test_invalid_frames_never_refresh_valid_age_and_decoder_work_is_bounded(self):
        engine = self.engine; engine.connection(True, 0)
        class Port:
            def write(_, raw): return len(raw)
        engine.poll(Port(), lambda: 31000)
        request = engine.poller.pending
        engine.codec.replies = [Reply(request.address, request.request_id, True, 1234, None, None)]
        engine.receive(b'fixture-reply', 32000)
        engine.receive(b'fixture-corruption', 33000)  # Codec rejected it: no Reply.
        self.assertEqual(engine.poller.last_received['front'], 32000)
        self.assertIsNone(engine.poller.status(82000)[0]['distance_mm'])
        for raw in (b'', b'x'*1025):
            with self.assertRaisesRegex(ValueError, 'invalid_sonar_read'): engine.receive(raw, 83000)
        engine.codec.replies = [Reply('front-address', '1', True, 0, None, None)]*33
        with self.assertRaisesRegex(ValueError, 'unbounded_sonar_decoder'): engine.receive(b'x', 83000)


class ProjectionTests(unittest.TestCase):
    def setUp(self):
        self.config, addresses, self.profile = fixture()
        self.engine = Engine(addresses, self.profile, 'boot-test', 'collector-test', Sink(), 'synthetic_test')
        self.engine.connection(True, 0)
        request = self.engine.poller.next_request(31000); self.engine.poller.sent(31000)
        self.engine.poller.receive(Reply(request.address, request.request_id, True, 0, None, None), 32000)
        self.payload = self.engine.status(33000)
        self.worker = dict(state='RUNNING', reason=None, service_revision=fingerprint(self.config, 'sonar'),
                           instance_id='collector-test', observed_monotonic_us='33000', payload=self.payload)

    def read(self, now=34000):
        with patch('ss07.sonar_driver.PROFILES', {'test-only': self.profile}):
            return inspect(self.config, self.worker, now, 'boot-test')

    def test_synthetic_observation_cannot_be_promoted_to_real_status(self):
        self.assertEqual(self.read()['reason'], 'invalid_sonar_payload')
        self.assertTrue(all(not row['valid'] and row['distance_mm'] is None for row in self.read()['samples']))

    def test_api_recomputes_age_and_invalidates_stale_or_wrong_configuration(self):
        # Structural production envelope fixture only; never device evidence.
        self.payload['samples']['front']['evidence_kind'] = 'device_observation'
        data = self.read()
        self.assertTrue(data['samples'][0]['valid']); self.assertEqual(data['samples'][0]['distance_mm'], 0)
        self.assertEqual(data['samples'][0]['age_ms'], 2)
        self.assertEqual(self.read(82000)['samples'][0]['reason'], 'sample_stale')
        self.worker['state'] = 'STALE'
        self.assertTrue(all(not r['valid'] for r in self.read()['samples']))
        self.worker['state'] = 'RUNNING'; self.worker['service_revision'] = '0'*64
        self.assertEqual(self.read()['reason'], 'invalid_sonar_payload')

    def test_boot_instance_future_timestamp_and_replaced_adapter_rejected(self):
        original = copy.deepcopy(self.payload['samples']['front'])
        original['evidence_kind'] = 'device_observation'
        for changes in ({'rk_boot_id': 'another-boot'}, {'collector_instance_id': 'old-collector'},
                        {'address': 'other-adapter'}, {'event_monotonic_us': '999999'}, {'sample_id': None}):
            self.payload['samples']['front'] = dict(original, **changes)
            self.assertEqual(self.read()['reason'], 'invalid_sonar_payload')


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)/'sonar.sqlite'
        self.journal = Journal(self.path); self.journal.initialize()
        self.journal.append([tagged(measurement(i, received=i*1000)) for i in range(1, 5)])
        state = State(load(ROOT/'config/system.example.json'), sonar_journal_path=self.path)
        self.server = Server(('127.0.0.1', 0), state)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.base = 'http://127.0.0.1:'+str(self.server.server_port)
        self.addCleanup(self.close)

    def close(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(2)

    def get(self, path):
        try: response = urlopen(self.base+path, timeout=3)
        except HTTPError as exc: response = exc
        with response: return response.status, json.load(response)

    def test_history_paginates_and_declares_synthetic_diagnostic_retention(self):
        code, body = self.get('/api/v1/sonars/history?limit=2&direction=front')
        self.assertEqual(code, 200); data = body['data']
        self.assertEqual([s['seq'] for s in data['items']], ['4','3'])
        self.assertFalse(data['complete_history']); self.assertFalse(data['data_disk_archive'])
        self.assertTrue(all(s['evidence_kind'] == 'synthetic_test' for s in data['items']))
        code, body = self.get('/api/v1/sonars/history?cursor='+data['next_cursor'])
        self.assertEqual([s['seq'] for s in body['data']['items']], ['2','1'])
        self.assertTrue(all(not s['valid'] for s in self.get('/api/v1/sonars')[1]['data']['sonars']))

    def test_invalid_queries_and_unavailable_history_have_distinct_errors(self):
        for query in ('start_us=1', 'source_epoch=0', 'limit=201', 'limit=1&limit=2', 'direction=../etc',
                      'source_epoch=2&start_us=3&end_us=2', 'cursor=9223372036854775808', 'unknown=1'):
            self.assertEqual(self.get('/api/v1/sonars/history?'+query)[0], 422)
        self.path.unlink()
        self.assertEqual(self.get('/api/v1/sonars/history')[0], 503)


class WorkerProcessTests(unittest.TestCase):
    def test_unconfigured_cli_emits_only_availability_and_clean_lifecycle(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); journal = root/'history.sqlite'; state = root/'state'
            env = dict(os.environ, PYTHONPATH=str(ROOT/'src'), PYTHONDONTWRITEBYTECODE='1')
            interpreter = [sys.executable]
            if platform.machine() == 'aarch64' and Path('/qemu-aarch64-static').is_file():
                interpreter.insert(0, '/qemu-aarch64-static')
            proc = subprocess.Popen(interpreter + ['-m', 'ss07', 'sonar-worker', '--state-dir', str(state),
                                    '--sonar-journal', str(journal)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic()+10
                while True:
                    if proc.poll() is not None: self.fail('worker exited: '+proc.stderr.read().decode())
                    try: snapshot = json.loads((state/'sonar.json').read_text())
                    except FileNotFoundError: snapshot = {}
                    if snapshot.get('state') == 'UNAVAILABLE' and journal.exists(): break
                    if time.monotonic() >= deadline: self.fail('worker readiness timeout')
                    time.sleep(.02)
                self.assertEqual(snapshot['payload']['reason'], 'sonar_not_configured')
                self.assertFalse(snapshot['payload']['serial_connected'])
                self.assertNotIn('samples', snapshot['payload'])
                proc.terminate(); output, error = proc.communicate(timeout=5)
                self.assertEqual(proc.returncode, 0, error.decode())
                self.assertEqual(json.loads((state/'sonar.json').read_text())['state'], 'STOPPED')
                items = Journal(journal).history()['items']
                self.assertEqual([s['kind'] for s in items], ['COLLECTOR_STOPPED', 'COLLECTOR_STARTED'])
                self.assertTrue(all(s['distance_mm'] is None and not s['valid'] for s in items))
            finally:
                if proc.poll() is None: proc.kill(); proc.wait()
                proc.stdout.close(); proc.stderr.close()


if __name__ == '__main__': unittest.main()
