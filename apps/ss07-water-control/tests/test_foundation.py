import copy
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from ss07.api import Server, State
from ss07.config import ConfigError, ROOT, load, read_json, validate
from ss07.probe import command


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.config = load(ROOT/'config/system.example.json')

    def test_default_no_invented_parameters(self):
        self.assertTrue(all(c['mode'] is None and c['binding'] is None
                            for c in self.config['cameras'].values()))
        self.assertIsNone(self.config['sonar']['vendor_protocol'])
        self.assertTrue(all(v is None for v in self.config['timecode']['uart'].values()))

    def test_invalid_contracts(self):
        mutations = [lambda c: c.update(motor={}),
                     lambda c: c.update(schema_version=True),
                     lambda c: c['api'].update(host='0.0.0.0'),
                     lambda c: c['api'].update(port=65536),
                     lambda c: c['timecode'].update(read_only=False),
                     lambda c: c['timecode'].update(binding='/dev/ttyUSB0'),
                     lambda c: c['timecode'].update(binding='/dev/serial/by-id/../ttyUSB0'),
                     lambda c: c['cameras']['front'].update(binding='/dev/video0'),
                     lambda c: c['cameras'].pop('down'),
                     lambda c: c['sdi'].update(rtsp_url='rtsp://user:secret@camera/live'),
                     lambda c: c['storage'].update(mountpoint='/'),
                     lambda c: c['cameras']['front'].update(mode={'fourcc':'H264','width':1920,
                         'height':1080,'fps_num':25,'fps_den':0})]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                c = copy.deepcopy(self.config)
                mutate(c)
                with self.assertRaises(ConfigError):
                    validate(c)

    def test_stable_binding_and_requested_mode(self):
        self.config['cameras']['front'].update(binding='/dev/v4l/by-path/usb-port-video-index0',
                  mode={'fourcc':'H264','width':1920,'height':1080,'fps_num':25,'fps_den':1})
        validate(self.config)  # structural validity, NOT a claim of device support

    def test_duplicate_bindings_and_addresses(self):
        for kind in ('cameras', 'serial', 'addresses'):
            c = copy.deepcopy(self.config)
            if kind == 'cameras':
                for d in ('front','left'):
                    c['cameras'][d]['binding']='/dev/v4l/by-id/same'
            elif kind == 'serial':
                for k in ('sonar','timecode'):
                    c[k]['binding']='/dev/serial/by-id/same'
            else:
                for d in ('front','left'):
                    c['sonar']['nodes'][d]['address']='vendor-address-1'
            with self.subTest(kind=kind), self.assertRaises(ConfigError):
                validate(c)

    def test_corrupt_json(self):
        for raw in (b'{"a":1,"a":2}', b'{"a":NaN}', b'[]'*40000, b'\xff', b'['*2000):
            with tempfile.NamedTemporaryFile() as f:
                f.write(raw)
                f.flush()
                with self.subTest(raw=raw[:20]), self.assertRaises(ConfigError):
                    read_json(f.name)


class ProbeTests(unittest.TestCase):
    def test_missing_tool(self):
        self.assertEqual(command(['ss07-nonexistent-tool'])['state'], 'missing_tool')

    def test_error_timeout_and_output_bound(self):
        self.assertEqual(command([sys.executable,'-c','raise SystemExit(3)'])['returncode'],3)
        self.assertEqual(command([sys.executable,'-c','import time; time.sleep(1)'],
                                 timeout=.05)['state'], 'timeout')
        result=command([sys.executable,'-c','print("x"*10000)'], limit=128)
        self.assertEqual(result['state'], 'output_limit')
        self.assertEqual(len(result['output']), 128)


class APITests(unittest.TestCase):
    def setUp(self):
        self.state=State(load(ROOT/'config/system.example.json'))
        self.server=Server(('127.0.0.1',0),self.state)
        self.thread=threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.base=f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def get(self,path):
        with urlopen(self.base+path, timeout=2) as response:
            self.assertEqual(response.headers['Cache-Control'], 'no-store')
            return json.load(response)['data']

    def test_missing_hardware_is_not_healthy(self):
        s=self.get('/api/v1/status')
        self.assertEqual(len(s['streams']),6)
        self.assertEqual(len(s['sonars']),5)
        self.assertTrue(all(x['distance_mm'] is None and not x['valid'] for x in s['sonars']))
        self.assertTrue(all(x['fps_observed'] is None and x['live_url'] is None for x in s['streams']))
        self.assertEqual(s['timecode']['quality'],'UNAVAILABLE')
        self.assertFalse(s['storage']['recording_enabled'])
        self.assertIsInstance(s['local_monotonic_us'],str)
        self.assertGreater(int(self.get('/api/v1/status')['seq']),int(s['seq']))

    def test_all_read_endpoints(self):
        for key in ('streams','sonars','timecode'):
            self.assertIn(key,self.get('/api/v1/'+key))
        cfg=self.get('/api/v1/config')
        self.assertEqual(list(cfg['runtime']),['api'])
        self.assertFalse(cfg['mutable'])

    def test_write_and_unimplemented_rejected(self):
        for method,path,code in [('PUT','/api/v1/config',405),('POST','/api/v1/motor',405),
                                 ('GET','/api/v1/recordings',503),('GET','/streams',404),
                                 ('GET','/../../etc/passwd',404)]:
            with self.subTest(method=method,path=path), self.assertRaises(HTTPError) as ctx:
                urlopen(Request(self.base+path,method=method),timeout=2)
            self.assertEqual(ctx.exception.code,code)
            self.assertIn('error',json.load(ctx.exception))

    def test_partial_client_does_not_block_status(self):
        with socket.create_connection(self.server.server_address, timeout=2) as client:
            client.sendall(b'GET /api/v1/status HTTP/1.1\r\n')
            self.assertEqual(self.get('/api/v1/status')['service_states']['api'],'RUNNING')


if __name__ == '__main__':
    unittest.main()
