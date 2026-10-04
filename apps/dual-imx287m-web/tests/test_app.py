import io
import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import Handler, ThreadingHTTPServer, read_exact, validate_config


class ConfigurationTests(unittest.TestCase):
    def test_high_speed_default_and_slow_trigger_headroom(self):
        self.assertEqual(validate_config({})['camera_fps'], 320)
        self.assertEqual(validate_config({'format': 'raw8', 'fps': 30})['camera_fps'], 60)
        self.assertEqual(validate_config({'mode': 'free', 'fps': 320})['camera_fps'], 320)

    def test_invalid_inputs(self):
        for body in ({'fps': 320}, {'fps': float('nan')}, {'fps': True},
                     {'fps': 0}, {'format': {}}, {'mode': []}, {'preview_fps': 31},
                     {'exposure_us': 4000}, {'exposure_us': 1.5}, {'auto_contrast': 1},
                     {'unknown': 'value'}, []):
            with self.subTest(body=body), self.assertRaises(ValueError):
                validate_config(body)

    def test_raw12_boundary(self):
        self.assertEqual(validate_config({'fps': 319.4})['fps'], 319.4)
        with self.assertRaises(ValueError):
            validate_config({'fps': 319.5})

    def test_pipe_short_reads_and_eof(self):
        class Fragmented(io.BytesIO):
            def read(self, size):
                return super().read(min(size, 2))
        self.assertEqual(read_exact(Fragmented(b'abcdef'), 6), b'abcdef')
        with self.assertRaises(EOFError):
            read_exact(Fragmented(b'abc'), 6)


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.server.daemon_threads = True
        cls.engine = Mock()
        cls.engine.status.return_value = {'phase': 'stopped'}
        cls.engine.condition = threading.Condition()
        cls.engine.frames = [{'jpeg': None}, {'jpeg': None}]
        cls.engine.start.side_effect = validate_config
        cls.server.engine = cls.engine
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = 'http://127.0.0.1:%d' % cls.server.server_port

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def request(self, path, data=None, headers=None):
        request = urllib.request.Request(self.url + path, data=data, headers=headers or {})
        try:
            response = urllib.request.urlopen(request, timeout=3)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            return response.status, response.read()

    def test_status_and_static_whitelist(self):
        self.assertEqual(self.request('/api/status')[0], 200)
        self.assertEqual(self.request('/')[0], 200)
        self.assertEqual(self.request('/../app.py')[0], 404)
        self.assertEqual(self.request('/snapshot/0.jpg')[0], 503)

    def test_bad_config_and_cross_origin(self):
        self.assertEqual(self.request('/api/start', b'{"format":{}}')[0], 400)
        self.assertEqual(self.request('/api/start', b'{"fps":320}')[0], 400)
        self.assertEqual(self.request('/api/start', b'{}', {'Origin': 'http://other-host'})[0], 403)
        self.assertEqual(self.request('/api/start', b'{}')[0], 200)
        self.assertEqual(self.request('/api/stop', b'{}')[0], 200)


if __name__ == '__main__':
    unittest.main()
