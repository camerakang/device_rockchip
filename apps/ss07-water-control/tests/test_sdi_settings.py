"""SE2017 settings bridge against a fake encoder that, like the real one, accepts anything."""
import copy
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ss07.sdi_settings import EncoderClient, EncoderError

TABLE = {'dynamic': {'sStreamType': {'mainStream': {
            'iMaxRate': {'options': [256, 512, 1024, 2048, 4096, 8192, 12288, 16384, 24576, 32768]},
            'sResolution': {'options': ['1920*1080', '1280*720', '720*480']}}}},
         'static': {'iGOP': {'range': {'max': 300, 'min': 5}}, 'sFrameRate': {'options': ['24', '25', '30', '50', '60']},
                    'sH264Profile': {'options': ['highP', 'main', 'baseline']}, 'sRCMode': {'options': ['CBR', 'VBR']}}}


def encoder_state():
    return {
        'video': {'iGOP': 30, 'iMaxRate': 8192, 'iMinRate': 0, 'iTargetRate': 0, 'id': 0, 'sFrameRate': '60',
                  'sFrameRateIn': '60', 'sH264Profile': 'highP', 'sOutputDataType': 'H.264', 'sRCMode': 'VBR',
                  'sResolution': '1920*1080', 'sStreamType': 'mainStream', 'sVideoEncoderConfigurationName': 'VideoEncoder_0',
                  'sVideoEncoderConfigurationToken': 'VideoEncoderToken_0', 'sVideoSourceToken': 'VideoSource_0',
                  'sVideoSwitch': 'open'},
        'audio': {'iBitRate': 256000, 'iSampleRate': 48000, 'iVolume': 100, 'id': 0, 'sANS': 'close',
                  'sAudioSwitch': 'open', 'sBitWidth': '24bit', 'sEncodeType': 'AAC', 'sInput': 'micIn', 'sSoundChannel': 'L+R'},
        'info': [{'id': 2, 'name': 'model', 'ro': 'true', 'value': 'SE2017'},
                 {'id': 3, 'name': 'firmwareVersion', 'ro': 'true', 'value': 'V1.0.0.9'},
                 {'id': 8, 'name': 'videoSource', 'ro': 'false', 'value': 'SDI'},
                 {'id': 9, 'name': 'videoSourceList', 'ro': 'true', 'value': 'SDI,HDMI'},
                 {'id': 10, 'name': 'audioSource', 'ro': 'false', 'value': 'SDI'},
                 {'id': 11, 'name': 'audioSourceList', 'ro': 'true', 'value': 'SDI,HDMI,LINEIN'}],
        'status': [{'id': 5, 'name': 'inputVideoInfo', 'ro': 'true', 'value': 'SDI NoSignal N/A'}]}


class FakeEncoder(BaseHTTPRequestHandler):
    def log_message(self, *args): pass

    def send(self, value, cookie=None):
        raw = json.dumps(value).encode()
        self.send_response(200); self.send_header('Content-Type', 'application/json')
        if cookie: self.send_header('Set-Cookie', 'token=%s; path=/' % cookie)
        self.send_header('Content-Length', str(len(raw))); self.end_headers(); self.wfile.write(raw)

    def authed(self):
        return self.headers.get('Cookie') == 'token=' + self.server.token

    def do_GET(self):
        s, path = self.server.state, self.path.split('?')[0].replace('/cgi-bin/entry.cgi', '')
        if not self.authed(): return self.send({'error': {'code': 500, 'message': 'invalid token supplied'}})
        self.send({'/video/0': s['video'], '/audio/0': s['audio'], '/system/device-info': s['info'],
                   '/system/device-status': s['status'], '/system/para/video-encoder': json.dumps(TABLE)}[path])

    def do_PUT(self):
        s, path = self.server.state, self.path.replace('/cgi-bin/entry.cgi', '')
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        if path == '/system/login':
            ok = body['sUserName'] == 'admin'
            return self.send({'auth': 0, 'status': 0 if ok else -1}, self.server.token if ok else None)
        if not self.authed(): return self.send({'error': {'code': 500, 'message': 'invalid token supplied'}})
        self.server.puts.append((path, body))
        if path == '/video/0': s['video'].update(body)
        elif path == '/audio/0': s['audio'].update(body)
        elif path == '/system/device-info':
            for item in s['info']:
                if item['name'] == body['name']: item['value'] = body['value']
        self.send(body)


class SdiSettingsTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), FakeEncoder)
        self.server.state, self.server.token, self.server.puts = encoder_state(), 'abc', []
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.client = EncoderClient('127.0.0.1', {'username': 'admin', 'password': 'admin'}, port=self.server.server_port)

    def tearDown(self):
        self.server.shutdown(); self.server.server_close()

    def test_read_maps_encoder_fields_and_options(self):
        data = self.client.read()
        self.assertEqual(data['settings'], dict(video_source='SDI', resolution='1920x1080', fps=60, codec='H264',
                                                profile='high', rate_control='VBR', max_bitrate_kbps=8192, gop=30,
                                                audio_source='SDI', audio_bitrate=256000, audio_volume=100))
        self.assertEqual(data['input']['signal'], 'no_signal')
        self.assertEqual(data['options']['gop'], {'min': 5, 'max': 300})
        self.assertEqual(data['options']['codec'], ['H264'])  # RK publishes only H.264.
        self.assertEqual(data['options']['video_source'], ['SDI', 'HDMI'])

    def test_invalid_values_never_reach_the_encoder(self):
        rev = self.client.read()['revision']
        for change in ({'gop': 9999}, {'fps': 29}, {'codec': 'H265'}, {'resolution': '3840x2160'},
                       {'audio_volume': True}, {'password': 'x'}, {}):
            with self.assertRaises(EncoderError) as caught: self.client.update(rev, change)
            self.assertEqual((caught.exception.code, caught.exception.status), ('INVALID_SDI_SETTINGS', 422), change)
        self.assertEqual(self.server.puts, [])

    def test_update_writes_reads_back_and_checks_revision(self):
        rev = self.client.read()['revision']
        data = self.client.update(rev, {'gop': 60, 'fps': 25, 'max_bitrate_kbps': 4096, 'audio_volume': 80,
                                        'video_source': 'HDMI'})
        self.assertEqual(data['changed'], ['audio_volume', 'fps', 'gop', 'max_bitrate_kbps', 'video_source'])
        video = self.server.state['video']
        self.assertEqual((video['iGOP'], video['sFrameRate'], video['iMaxRate'], video['sH264Profile']), (60, '25', 4096, 'highP'))
        sent = dict(self.server.puts)['/video/0']
        self.assertNotIn('sVideoSourceToken', sent); self.assertNotIn('iTargetRate', sent)
        self.assertNotEqual(data['revision'], rev)
        with self.assertRaises(EncoderError) as caught: self.client.update(rev, {'gop': 90})
        self.assertEqual(caught.exception.status, 409)
        count = len(self.server.puts)
        self.assertEqual(self.client.update(data['revision'], {'gop': 60})['changed'], [])
        self.assertEqual(len(self.server.puts), count)

    def test_expired_session_relogs_once_and_bad_credentials_fail(self):
        self.client.read(); self.server.token = 'rotated'
        self.assertEqual(self.client.read()['settings']['gop'], 30)
        bad = EncoderClient('127.0.0.1', {'username': 'nobody', 'password': 'x'}, port=self.server.server_port)
        with self.assertRaises(EncoderError) as caught: bad.read()
        self.assertEqual(caught.exception.code, 'SDI_ENCODER_AUTH_FAILED')

    def test_unreachable_encoder(self):
        import socket
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0)); port = probe.getsockname()[1]  # Closed port: connection refused.
        with self.assertRaises(EncoderError) as caught:
            EncoderClient('127.0.0.1', {'username': 'admin', 'password': 'a'}, port=port, timeout=1).read()
        self.assertEqual((caught.exception.code, caught.exception.status), ('SDI_ENCODER_UNAVAILABLE', 503))


if __name__ == '__main__':
    unittest.main()
