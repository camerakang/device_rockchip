import json
import unittest
from ss07.config import validate
from ss07.sonar_driver import resolve
from ss07.sonar_status import inspect
from ss07.config_runtime import fingerprint
from ss07.sonar_worker import Engine
from test_sonar_runtime import fixture, Sink


def frame(seq, ch, **changes):
    f = dict(v=1, seq=seq, ms=seq, ch=ch, id=185, state='ok', mm=150, thr_mv=800)
    return json.dumps(dict(f, **changes)).encode() + b'\n'


class V11Tests(unittest.TestCase):
    def setUp(self):
        self.config = fixture()[0]
        s = self.config['sonar']
        s['vendor_protocol'] = 'sonar_drive_v1_1'
        for i, node in enumerate(s['nodes'].values(), 1):
            node['address'] = str(i)
        profile, addresses = resolve(validate(self.config))
        self.e = Engine(addresses, profile, 'boot', 'collector', Sink(), 'synthetic_test')
        self.e.connection(True, 0)

    def test_five_channels_and_expiry(self):
        e = self.e
        e.poll(object())  # read-only stream: no write method
        data = b''.join(frame(i, i) for i in range(1,6))
        for i in range(0, len(data), 37):
            e.receive(data[i:i+37], 100)
        self.assertEqual(len(e.poller.latest), 5)
        p = e.status(101)
        for row in p['samples'].values(): # structural API fixture only
            row['evidence_kind'] = 'device_observation'
            self.assertIsNone(row['request_id'])
        w = dict(state='RUNNING', instance_id='collector', payload=p,
                 service_revision=fingerprint(self.config,'sonar'), observed_monotonic_us='101')
        self.assertTrue(all(r['distance_mm']==150 for r in inspect(self.config,w,101,'boot')['samples']))
        self.assertTrue(all(r['distance_mm'] is None for r in inspect(self.config,w,1500100,'boot')['samples']))

    def test_resync_faults_and_reset(self):
        e = self.e
        e.receive(b'x'*512 + frame(1,1) + b'{"ack":true}\n', 1)
        self.assertFalse(e.poller.latest)
        e.receive(frame(2**32-1,1) + frame(0,2) + frame(3,3), 2)
        self.assertEqual(e.poller.counters['missing_frames'], 2)
        e.receive(frame(4,1,state='no_echo',mm=None) + frame(5,2,id=-1), 3)
        self.assertEqual(e.poller.latest['front']['reason'], 'no_echo')
        self.assertEqual(e.poller.latest['back']['reason'], 'wrong_id')
        e.receive(frame(0,4), 4)
        self.assertEqual(e.poller.counters['sequence_resets'], 1)
        self.assertFalse(e.poller.status(4)[2]['valid'])
        e.connection(False,5)
        self.assertFalse(any(row['valid'] for row in e.poller.status(5)))
