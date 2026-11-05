"""Synthetic values and virtual clocks; no manufacturer protocol is implied."""
import copy
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from ss07.sonar_core import DIRECTIONS, Poller, Reply, Timing, event, validate_event
from ss07.sonar_journal import AsyncHistory, Journal
from ss07.timecode_model import unavailable


def tagged(sample):
    return dict(sample, time_tag=unavailable(sample['rk_boot_id'], int(sample['event_monotonic_us']),
                                            sample['timestamp_basis'], 'test_no_timecode', sample['evidence_kind']))


def measurement(n=1, sensor='front', received=1000):
    from ss07.sonar_core import Request
    return event('boot-test', 'collector-test', n, 'MEASUREMENT', received, 'synthetic_test',
                 Request(sensor, sensor+'-address', str(n)), Reply(sensor+'-address', str(n), True, 1234, None, None), 'wire_id')


class PollerTests(unittest.TestCase):
    def make(self, mode='wire_id'):
        self.samples = []
        self.timing = Timing(10000, 30000, 1000, 1000, 50000, mode)
        self.poller = Poller({d:d+'-address' for d in DIRECTIONS}, self.timing, 'boot-test', 'collector-test',
                             self.samples.append, 'synthetic_test')
        return self.poller

    def good(self, request, value=1234):
        return Reply(request.address, request.request_id, True, value, None, None)

    def test_offline_node_does_not_starve_other_directions(self):
        p = self.make(); sequence = []; now = 0
        for _ in range(15):
            request = p.next_request(now); self.assertIsNotNone(request)
            sequence.append(request.sensor_id); p.sent(now)
            if request.sensor_id == 'front': now += 10000; p.tick(now)
            else: now += 1000; self.assertTrue(p.receive(self.good(request), now))
            now += 1000
        self.assertEqual(sequence, list(DIRECTIONS)*3)
        self.assertEqual(p.counters['timeouts'], 3)
        self.assertEqual(p.counters['measurements'], 12)
        front = p.status(now)[0]
        self.assertFalse(front['valid']); self.assertIsNone(front['distance_mm']); self.assertIsNone(front['age_ms'])
        self.assertTrue(all(row['valid'] for row in p.status(now)[1:]))

    def test_wrong_address_replay_and_late_response_do_not_refresh_age(self):
        p = self.make(); req = p.next_request(0); p.sent(0)
        self.assertFalse(p.receive(Reply('other', req.request_id, True, 0, None, None), 100))
        self.assertFalse(p.receive(Reply(req.address, '999', True, 0, None, None), 200))
        self.assertTrue(p.receive(self.good(req), 1000))
        self.assertFalse(p.receive(self.good(req), 1500))
        nxt = p.next_request(2000); p.sent(2000)
        self.assertFalse(p.receive(self.good(nxt), 12000))
        self.assertEqual(p.counters['timeouts'], 1)
        self.assertEqual(p.status(51000)[0]['reason'], 'sample_stale')
        self.assertIsNone(p.status(51000)[0]['distance_mm'])

    def test_window_protocol_waits_entire_confirmed_late_reply_window(self):
        p = self.make('bounded_response_window'); req = p.next_request(0); p.sent(0)
        self.assertTrue(p.receive(Reply(req.address, None, True, 400, None, None), 1000))
        self.assertIsNone(p.next_request(29999))
        other = p.next_request(30000); self.assertEqual(other.sensor_id, 'back'); p.sent(30000)
        self.assertFalse(p.receive(Reply(req.address, None, True, 900, None, None), 31000))
        self.assertTrue(p.receive(Reply(other.address, None, True, 500, None, None), 32000))
        self.assertEqual(self.samples[0]['correlation_basis'], 'bounded_response_window')

    def test_invalid_measurement_is_null_but_real_zero_is_preserved(self):
        p = self.make(); req = p.next_request(0); p.sent(0)
        self.assertTrue(p.receive(self.good(req, 0), 1000))
        self.assertEqual(p.status(1000)[0]['distance_mm'], 0)
        req = p.next_request(2000); p.sent(2000)
        self.assertTrue(p.receive(Reply(req.address, req.request_id, False, None, 'no_echo', None), 3000))
        self.assertIsNone(p.status(3000)[1]['distance_mm'])
        self.assertEqual(p.status(3000)[1]['reason'], 'no_echo')
        with self.assertRaises(ValueError): validate_event(dict(self.samples[-1], distance_mm=0))

    def test_partial_configuration_no_fake_samples_and_no_reordered_addresses(self):
        p = self.make(); p.addresses['back'] = None
        req = p.next_request(0); p.send_failed(0)
        self.assertEqual(p.status(1000)[1]['reason'], 'node_unconfigured')
        self.assertIsNone(p.status(1000)[1]['seq'])
        self.assertIsNone(p.next_request(29999))
        self.assertEqual(p.next_request(30000).sensor_id, 'left')
        self.assertEqual(self.samples[0]['kind'], 'SEND_FAILED')
        self.assertIsNone(self.samples[0]['received_monotonic_us'])

    def test_reconnect_invalidates_all_samples_and_rejects_prior_request(self):
        p = self.make(); req = p.next_request(0); p.sent(0)
        self.assertTrue(p.receive(self.good(req), 1000))
        p.connection(False, 2000)
        self.assertFalse(p.status(2000)[0]['valid'])
        self.assertIsNone(p.status(2000)[0]['distance_mm'])
        p.connection(True, 3000)
        self.assertIsNone(p.next_request(33999))
        self.assertEqual(p.next_request(34000).sensor_id, 'back'); p.sent(34000)
        self.assertFalse(p.receive(self.good(req), 35000))
        self.assertEqual(p.status(35000)[0]['received_monotonic_us'], '1000')

    def test_clock_regression_and_unconfirmed_timing_are_rejected(self):
        p = self.make(); p.tick(100)
        with self.assertRaisesRegex(ValueError, 'clock_regressed'): p.next_request(99)
        for fields in ((1000, 999, 0, 1000, 1000, 'wire_id'), (1000, 1000, 0, 1000, 999, 'wire_id'),
                       (True, 1000, 0, 1000, 1000, 'wire_id'), (1000, 1000, 0, 1000, 1000, 'guess')):
            with self.assertRaises(ValueError): Timing(*fields)

    def test_time_tag_must_describe_original_event_and_same_evidence(self):
        sample = tagged(measurement(received=1000)); validate_event(sample, tagged=True)
        for changes in ({'event_monotonic_us':'1001'}, {'rk_boot_id':'another-boot'}, {'evidence_kind':'device_observation'},
                        {'measurement_time_us':'1000'}):
            with self.assertRaises(ValueError): validate_event(dict(sample, **changes), tagged=True)


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'sonar.sqlite'
        self.journal = Journal(self.path); self.journal.initialize()

    def test_atomic_batches_readonly_handle_and_explicit_retention(self):
        with patch('ss07.sonar_journal.MAX_EVENTS', 3):
            self.journal.append([tagged(measurement(i, received=i*1000)) for i in range(1,5)])
        data = self.journal.history(limit=2)
        self.assertEqual([s['seq'] for s in data['items']], ['4','3'])
        self.assertEqual(data['evicted_events'], '1')
        self.assertFalse(data['complete_history']); self.assertFalse(data['data_disk_archive'])
        self.assertEqual([s['seq'] for s in self.journal.history(cursor=data['next_cursor'])['items']], ['2'])
        duplicate = tagged(measurement(5))
        with self.assertRaises(sqlite3.IntegrityError): self.journal.append([duplicate, duplicate])
        self.assertEqual(self.journal.history()['retained_events'], 3)
        db = self.journal.connect()
        try:
            with self.assertRaises(sqlite3.OperationalError): db.execute('DELETE FROM events')
        finally: db.close()

    def test_u64_epoch_and_range_query_never_use_floating_point(self):
        from ss07.timecode_model import Mapper
        from ss07.timecode_wire import Packet
        mapper = Mapper('boot-test', 'receiver-test', 'synthetic_test')
        epoch, external = 2**63+11, 2**63+12345
        self.assertTrue(mapper.observe(Packet(epoch,1,external), 1000, 1000))
        samples = [measurement(1, received=1100), measurement(2, sensor='back', received=1200)]
        for sample in samples:
            at = int(sample['event_monotonic_us'])
            sample['time_tag'] = mapper.tag(at, 1300, 'sonar_receive', 'boot-test')
        self.journal.append(samples)
        data = self.journal.history(source_epoch=str(epoch), start_us=str(external+150), end_us=str(external+250))
        self.assertEqual([s['sensor_id'] for s in data['items']], ['back'])
        self.assertEqual(data['items'][0]['time_tag']['estimated_time_us'], str(external+200))
        self.assertEqual(self.journal.history(source_epoch=str(epoch-1))['items'], [])
        with self.assertRaises(ValueError): self.journal.history(start_us='1')
        with self.assertRaises(ValueError): self.journal.history(direction='../other')

    def test_restart_lifecycle_marks_unclean_previous_collector(self):
        first = tagged(event('boot-test','collector-old',0,'COLLECTOR_STARTED',1,'synthetic_test'))
        second = tagged(event('boot-test','collector-new',0,'COLLECTOR_STARTED',2,'synthetic_test'))
        self.journal.append([first], session='collector-old')
        self.journal.append([second], session='collector-new')
        self.assertEqual(self.journal.history()['items'][0]['reason'], 'unclean_previous_session')
        stopped = tagged(event('boot-test','collector-new',0,'COLLECTOR_STOPPED',3,'synthetic_test'))
        self.journal.append([stopped], session='collector-new', finish=True)
        third = tagged(event('boot-test','collector-third',0,'COLLECTOR_STARTED',4,'synthetic_test'))
        self.journal.append([third], session='collector-third')
        self.assertEqual(self.journal.history()['items'][0]['reason'], 'collector_started')

    def test_symlink_and_wrong_schema_refused(self):
        link = self.path.parent / 'link'; link.symlink_to(self.path)
        with self.assertRaises(OSError): Journal(link).initialize()
        with sqlite3.connect(str(self.path)) as db: db.execute('PRAGMA user_version=99')
        with self.assertRaises(ValueError): self.journal.history()

    def test_capacity_high_water_reclaims_before_database_is_full(self):
        with patch('ss07.sonar_journal.MAX_BYTES', 512*1024):
            for base in range(0, 1024, 32):
                self.journal.append([tagged(measurement(base+i+1)) for i in range(32)])
            data = self.journal.history()
            self.assertGreater(int(data['evicted_events']), 0)
            self.assertEqual(data['items'][0]['seq'], '1024')
            self.assertLessEqual(self.path.stat().st_size, 512*1024)

    def test_delayed_tag_uses_original_receive_epoch_and_not_latest_epoch(self):
        from ss07.timecode_model import Mapper
        from ss07.timecode_wire import Packet
        mapper = Mapper('boot-test', 'receiver-test', 'synthetic_test')
        mapper.observe(Packet(11, 1, 9000), 1000, 1000)
        sample = measurement(received=1100)
        mapper.observe(Packet(22, 1, 100), 2000, 2000)
        class Tagger:
            def tag(_, when, basis): return mapper.tag(when, 2100, basis, 'boot-test')
        sink = AsyncHistory(self.path, 'boot-test', 'collector-test', Tagger(), 'synthetic_test')
        value = sink.tag(sample)
        self.assertEqual(value['time_tag']['source_epoch'], '11')
        self.assertEqual(value['time_tag']['estimated_time_us'], '9100')
        self.assertEqual(value['received_monotonic_us'], '1100')

    def test_final_failed_write_is_reported_before_clean_shutdown(self):
        class Tagger:
            def tag(_, when, basis): return unavailable('boot-test', when, basis, 'test', 'synthetic_test')
        sink = AsyncHistory(self.path, 'boot-test', 'collector-test', Tagger(), 'synthetic_test')
        original = sink.journal.append
        initialized = threading.Event()
        def append(events, **kwargs):
            if events[0]['kind'] == 'MEASUREMENT': raise sqlite3.OperationalError('test disk failure')
            result = original(events, **kwargs)
            if events[0]['kind'] == 'COLLECTOR_STARTED': initialized.set()
            return result
        sink.journal.append = append
        sink.start()
        try:
            self.assertTrue(initialized.wait(3)); sink.submit(measurement())
        finally: sink.close()
        sink.thread.join(3)
        self.assertFalse(sink.thread.is_alive())
        rows = self.journal.history()
        self.assertEqual(rows['known_lost_events'], '1')
        self.assertEqual(rows['items'][0]['kind'], 'COLLECTOR_STOPPED')
        self.assertEqual(rows['items'][1]['kind'], 'HISTORY_GAP')

    def test_lifecycle_rejects_unrelated_session_marker(self):
        with self.assertRaises(ValueError): self.journal.append([tagged(measurement())], session='collector-test')
        with self.assertRaises(ValueError): self.journal.append([tagged(measurement())], finish=True)

    def test_slow_tag_or_disk_never_blocks_submit_and_loss_is_observable(self):
        entered, release = threading.Event(), threading.Event()
        class Tagger:
            def tag(_, when, basis):
                entered.set(); release.wait(5)
                return unavailable('boot-test', when, basis, 'test_no_timecode', 'synthetic_test')
        sink = AsyncHistory(self.path, 'boot-test', 'collector-test', Tagger(), 'synthetic_test')
        sink.start()
        try:
            sink.submit(measurement()); self.assertTrue(entered.wait(2))
            start = time.monotonic()
            for i in range(2,302): sink.submit(measurement(i, received=i*1000))
            self.assertLess(time.monotonic()-start, 2)
            self.assertGreater(int(sink.status()['dropped_events']), 0)
            self.assertLessEqual(sink.status()['queued_events'], 256)
        finally: release.set(); sink.close()
        deadline = time.monotonic()+5
        while sink.thread.is_alive() and time.monotonic() < deadline: sink.thread.join(.1)
        self.assertFalse(sink.thread.is_alive())
        rows = self.journal.history(limit=200)
        self.assertGreater(int(rows['known_lost_events']), 0)
        self.assertTrue(all(row['evidence_kind'] == 'synthetic_test' for row in rows['items']))


if __name__ == '__main__': unittest.main()
