import copy
import json
import http.client
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from ss07.camera import command, telemetry
from ss07.camera import run as run_camera
from ss07.config import ConfigError, ROOT, load, validate
from ss07.media import CHANNELS, MediaObserver, render, settings
from ss07.snapshots import SnapshotReader
from ss07.state import attach_media, empty_camera
from ss07.worker import Worker
from ss07.api import Server
from ss07.state import State
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class MediaTests(unittest.TestCase):
    def setUp(self):
        self.config = load(ROOT / 'config/system.example.json')

    def test_native_command_never_creates_an_encoder_or_shell(self):
        camera = self.config['cameras']['front']
        camera.update(binding='/dev/v4l/by-path/platform-usb-0:1.2-video-index0',
                      mode=dict(fourcc='H264', width=1920, height=1080, fps_num=25, fps_den=1))
        argv = command(self.config, 'front', '/app/bin/ss07-camera')
        self.assertIn(camera['binding'], argv)
        self.assertNotIn('!', argv)
        self.assertEqual(argv[-1], '19000')
        camera['mode']['fourcc'] = 'MJPG'
        with self.assertRaisesRegex(ConfigError, 'native_h264'):
            command(self.config, 'front', '/app/bin/ss07-camera')
        self.config['cameras']['front']['binding'] = None
        with self.assertRaisesRegex(ConfigError, 'unconfigured'):
            command(self.config, 'front', '/app/bin/ss07-camera')

    def test_media_is_loopback_only_and_missing_disk_cannot_enable_recording(self):
        config = render(self.config)
        self.assertEqual(set(config['paths']), set(CHANNELS))
        for key in ('rtspAddress', 'apiAddress', 'webrtcAddress', 'webrtcLocalUDPAddress'):
            self.assertTrue(config[key].startswith('127.0.0.1:'))
        self.assertFalse(config['pathDefaults']['record'])
        self.assertFalse(config['playback'])
        permissions = config['authInternalUsers'][0]['permissions']
        self.assertNotIn('publish', [p['action'] for p in permissions])
        self.assertEqual(config['paths']['sdi']['source'], 'publisher')
        self.config['sdi']['rtsp_url'] = 'rtsp://192.168.3.47:554/live/av0'
        self.assertEqual(render(self.config)['paths']['sdi']['source'], self.config['sdi']['rtsp_url'])

    def test_config_port_conflicts_and_old_config_defaults(self):
        self.assertEqual(settings(self.config)['rtsp_port'], 8554)
        for media in ({'rtsp_port': self.config['api']['port']}, {'ice_port': 19002},
                      {'rtp_base_port': 65530}, {'unknown': 12}):
            candidate = copy.deepcopy(self.config)
            candidate['media'] = media
            with self.assertRaises(ConfigError):
                validate(candidate)

    def test_ready_without_increasing_bytes_is_not_live(self):
        observer = MediaObserver()
        item = {'ready': True, 'codec': 'H264', 'bytes_received': '12', 'ready_time': 'session-1'}
        self.assertFalse(observer.observe({'front': item}, now=100)['streams']['front']['ready'])
        item['bytes_received'] = '24'
        result = observer.observe({'front': item}, now=200)
        self.assertTrue(result['streams']['front']['ready'])
        self.assertFalse(observer.observe({'front': item}, now=3000200)['streams']['front']['ready'])
        item['ready_time'] = 'session-2'
        item['bytes_received'] = '1'
        self.assertFalse(observer.observe({'front': item}, now=3000300)['streams']['front']['ready'])
        item['bytes_received'] = '100'
        self.assertTrue(observer.observe({'front': item}, now=3000400)['streams']['front']['ready'])
        self.assertFalse(observer.observe({}, now=3000500)['streams']['front']['ready'])

    def test_bad_or_synthetic_telemetry_cannot_be_published_as_hardware(self):
        data = dict(evidence_kind='device_observation', observed_monotonic_us='100',
                    last_frame_monotonic_us='95', frames='2', bytes='1000',
                    fps_observed=25.0, bitrate_observed=10000)
        self.assertEqual(telemetry(json.dumps(data), now=110), data)
        for fields in ({'evidence_kind': 'synthetic_test'}, {'observed_monotonic_us': '111'},
                       {'fps_observed': float('inf')}, {'last_frame_monotonic_us': '101'},
                       {'frames': '-1'}, {'extra': True}):
            with self.assertRaises(ConfigError):
                telemetry(json.dumps(dict(data, **fields)), now=110)
        with self.assertRaises(ConfigError):
            telemetry(json.dumps(data), now=3000100)
        with self.assertRaises(ConfigError):
            telemetry(json.dumps(data), previous=data, now=110)

    def test_sdi_reports_size_rates_and_no_signal(self):
        item = dict(ready=True, codec='H264', last_data_monotonic_us='100', width=1920, height=1080,
                    fps_observed=60.0, bitrate_observed=8.0e6, input_signal='no_signal', input_info='SDI NoSignal N/A')
        worker = dict(state='RUNNING', payload={'streams': {'sdi': item}})
        streams = [empty_camera('sdi')]
        attach_media(streams, worker, 1000)
        s = streams[0]
        self.assertEqual((s['width'], s['height'], s['fps_observed'], s['bitrate_observed']), (1920, 1080, 60.0, 8.0e6))
        self.assertEqual((s['availability'], s['capture_state'], s['error'], s['live_url']),
                         ('no_signal', 'NO_SIGNAL', 'sdi_no_signal', None))
        for signal in ('present', 'unknown'):
            streams = [empty_camera('sdi')]
            attach_media(streams, dict(state='RUNNING', payload={'streams': {'sdi': dict(item, input_signal=signal)}}), 1000)
            self.assertEqual((streams[0]['availability'], streams[0]['input_signal']), ('available', signal))
        streams = [empty_camera('sdi')]
        attach_media(streams, dict(state='RUNNING', payload={'streams': {'sdi': dict(item, width=True, fps_observed=float('nan'))}}), 1000)
        self.assertEqual((streams[0]['width'], streams[0]['fps_observed']), (None, None))

    def test_sdi_encoder_text_and_credentials(self):
        from ss07.sdi import classify, load_encoder, video_control
        self.assertEqual(classify('SDI NoSignal N/A')[0], 'no_signal')
        self.assertEqual(classify('SDI 1920x1080p60')[0], 'present')
        self.assertEqual(classify('')[0], 'unknown')
        sdp = 'v=0\r\nm=video 0 RTP/AVP 96\r\na=rtpmap:96 H264/90000\r\na=control:trackID=0\r\nm=audio 0 RTP/AVP 97\r\na=control:trackID=1\r\n'
        self.assertEqual(video_control(sdp), 'trackID=0')
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'sdi-encoder.json'
            self.assertIsNone(load_encoder(path))
            path.write_text(json.dumps(dict(schema_version=1, model='avmatrix-se2017', username='admin', password='x')))
            path.chmod(0o640); self.assertEqual(load_encoder(path)['username'], 'admin')
            path.chmod(0o644)
            with self.assertRaises(ValueError): load_encoder(path)

    def test_media_observer_measures_bitrate(self):
        observer = MediaObserver()
        item = {'ready': True, 'codec': 'H264', 'bytes_received': '0', 'ready_time': 't', 'width': 1920, 'height': 1080}
        observer.observe({'sdi': item}, now=0)
        item['bytes_received'] = '1000000'
        result = observer.observe({'sdi': item}, now=1000000)['streams']['sdi']
        self.assertEqual((result['bitrate_observed'], result['width']), (8000000.0, 1920))

    def test_media_status_age_gates_live_url(self):
        streams = [empty_camera('sdi')]
        worker = dict(state='RUNNING', payload={'streams': {'sdi': dict(ready=True, codec='H264',
                      last_data_monotonic_us='100')}})
        attach_media(streams, worker, 1000)
        self.assertEqual(streams[0]['live_url'], '/media/sdi/whep')
        attach_media(streams, worker, 3000100)
        self.assertIsNone(streams[0]['live_url'])
        self.assertEqual(streams[0]['availability'], 'unavailable')
        attach_media(streams, worker, 1000)
        self.assertIsNotNone(streams[0]['live_url'])
        attach_media(streams, {'state': 'RUNNING', 'payload': {'streams': []}}, 1001)
        self.assertIsNone(streams[0]['live_url'])
        self.assertEqual(streams[0]['publish_state'], 'UNAVAILABLE')

    def test_only_one_worker_owns_a_channel_and_stop_is_visible(self):
        with tempfile.TemporaryDirectory() as tmp:
            with Worker(tmp, 'camera.front') as worker:
                worker.emit('STARTING', {'reason': 'test_lifecycle_only'})
                with self.assertRaises(BlockingIOError):
                    with Worker(tmp, 'camera.front'):
                        pass
                reader = SnapshotReader(tmp, worker.boot)
                self.assertEqual(reader.read('camera.front')['state'], 'STARTING')
            self.assertEqual(reader.read('camera.front')['state'], 'STOPPED')
            with Worker(tmp, 'camera.front'):
                pass

    def test_unconfigured_worker_never_launches_a_capture_process(self):
        class StopAfterObservation:
            stopped = False
            def is_set(self):
                return self.stopped
            def wait(self, timeout):
                self.stopped = True
        with tempfile.TemporaryDirectory() as tmp, patch('ss07.camera.subprocess.Popen') as popen:
            with patch.object(Worker, 'emit', autospec=True) as emit:
                run_camera(self.config, 'front', tmp, '/unused', StopAfterObservation())
                self.assertEqual(emit.call_args_list[0].args[1:],
                                 ('UNAVAILABLE', {'reason': 'camera_unconfigured'}))
            popen.assert_not_called()

    def test_whep_proxy_confines_routes_origin_and_session_location(self):
        requests = []
        session = '11111111-2222-3333-4444-555555555555'
        class Backend(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                requests.append((self.command, self.path, self.rfile.read(int(self.headers['Content-Length']))))
                self.send_response(201)
                self.send_header('Location', '/front/whep/' + session)
                self.send_header('Content-Length', '3')
                self.end_headers()
                self.wfile.write(b'sdp')
            def do_DELETE(self):
                requests.append((self.command, self.path, b''))
                self.send_response(200)
                self.send_header('Content-Length', '0')
                self.end_headers()
        with ThreadingHTTPServer(('127.0.0.1', 0), Backend) as backend:
            thread = threading.Thread(target=backend.serve_forever, daemon=True); thread.start()
            self.config['media'] = {'webrtc_port': backend.server_port}
            with Server(('127.0.0.1', 0), State(self.config)) as api:
                api_thread = threading.Thread(target=api.serve_forever, daemon=True); api_thread.start()
                def request(method, path, body=b'', origin=None):
                    conn = http.client.HTTPConnection('127.0.0.1', api.server_port, timeout=2)
                    headers = {'Content-Type': 'application/sdp'}
                    if origin:
                        headers['Origin'] = origin
                    conn.request(method, path, body, headers)
                    response = conn.getresponse(); response.read()
                    result = response.status, response.getheader('Location')
                    conn.close()
                    return result
                try:
                    self.assertEqual(request('POST', '/media/front/whep', b'offer'),
                                     (201, '/media/front/whep/' + session))
                    self.assertEqual(request('DELETE', '/media/front/whep/' + session)[0], 200)
                    self.assertEqual(request('POST', '/media/front/whep', b'offer', 'https://untrusted.example')[0], 403)
                    self.assertEqual(request('POST', '/media/../v3/config/global/patch', b'{}')[0], 404)
                    self.assertEqual(request('POST', '/media/front/whep?url=http://example.org', b'offer')[0], 404)
                    self.assertEqual(request('POST', '/media/front/whep', b'x' * 32769)[0], 400)
                    self.assertEqual(len(requests), 2)
                finally:
                    api.shutdown(); api_thread.join(timeout=3)
            backend.shutdown(); thread.join(timeout=3)


if __name__ == '__main__':
    unittest.main()
