import json
import os
import tempfile
import threading
import time
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import test_auth
from test_auth import account_fixture
from ss07.api_clients import ApiClients
from ss07.gateway import render as render_gateway
from ss07.config import ROOT, load as load_config
from ss07.lan_monitor import LanMonitor, load
from ss07.rtsp_access import Authorization

INSIDE, OUTSIDE = '192.168.3.20', '10.9.8.7'


def write(path, value):
    path.write_text(json.dumps(value)); os.chmod(path, 0o644)


class FileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'lan-monitor.json'

    def test_every_bad_or_broad_file_keeps_mode_off(self):
        self.assertEqual(load(self.path)[:2], (False, 'file_missing'))
        base = dict(schema_version=1, enabled=True, allow_cidrs=['192.168.3.0/24'])
        cases = {'disabled': dict(base, enabled=False), 'allow_cidrs_empty': dict(base, allow_cidrs=[]),
                 'cidr_any_refused': dict(base, allow_cidrs=['192.168.3.0/24', '0.0.0.0/0']),
                 'cidr_invalid': dict(base, allow_cidrs=['192.168.3.1/24']), 'schema_invalid': dict(base, extra=1)}
        for reason, value in cases.items():
            write(self.path, value)
            self.assertEqual(load(self.path)[:2], (False, reason))
        self.path.write_text('{'); self.assertEqual(load(self.path)[:2], (False, 'file_invalid'))
        write(self.path, base); os.chmod(self.path, 0o666)
        self.assertEqual(load(self.path)[:2], (False, 'file_not_owner_controlled'))
        os.chmod(self.path, 0o644)
        monitor = LanMonitor(self.path, interval=0)
        self.assertTrue(monitor.allows(INSIDE)); self.assertFalse(monitor.allows(OUTSIDE))
        self.path.unlink(); self.assertFalse(monitor.allows(INSIDE))  # Removal applies without restart.

    def test_gateway_overwrites_client_supplied_peer_header(self):
        config = load_config(ROOT / 'config/system.example.json')
        text = render_gateway(config, json.loads((ROOT / 'config/gateway.example.json').read_text()))
        self.assertIn('header_up X-SS07-Peer {remote_host}', text)


class LanAPITests(test_auth.APIAuthTests):
    def setUp(self):
        super().setUp()
        self.lan_path = self.base / 'lan-monitor.json'
        write(self.lan_path, dict(schema_version=1, enabled=True, allow_cidrs=['192.168.3.0/24']))
        self.server.lan_monitor = LanMonitor(self.lan_path, interval=0)
        self.clients = ApiClients(self.base / 'api-clients.json')
        self.server.api_clients = self.clients
        self.key = self.clients.add('mobocon', 'maintainer')

    def lan(self, path, method='GET', ip=INSIDE, headers=None, body=None, raw=None):
        sent = dict({'X-SS07-Peer': ip}, **(headers or {}))
        if raw is None: return self.request(path, method, body, headers=sent)
        import http.client
        sent['Host'] = self.auth.authority
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=4)
        try:
            connection.request(method, path, raw, sent); response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally: connection.close()

    def test_route_matrix_for_allowed_and_other_sources(self):
        for path in ('/api/v1/status', '/api/v1/streams', '/api/v1/sonars', '/api/v1/timecode'):
            self.assertEqual(self.lan(path)[0], 200, path)
            self.assertEqual(self.lan(path, ip=OUTSIDE)[0], 401, path)
        streams = json.loads(self.lan('/api/v1/streams')[2])['data']['streams']
        self.assertTrue(all('@' not in (s.get('rtsp_url') or '') for s in streams))
        for path in ('/api/v1/config', '/api/v1/logs', '/api/v1/recordings', '/api/v1/timecode/history',
                     '/api/v1/sonars/history', '/api/v1/sonars/archives'):
            self.assertIn(self.lan(path)[0], (401, 403), path)
        self.assertIn(self.lan('/api/v1/config', 'PUT', body={})[0], (401, 403))
        for path in ('/api/v1/config/plan', '/api/v1/config/apply', '/api/v1/config/recover',
                     '/api/v1/recordings/' + str(uuid.uuid4()) + '/lock'):
            self.assertIn(self.lan(path, 'POST', body={})[0], (401, 403), path)
        self.assertEqual(self.lan('/api/v1/status', ip='not-an-ip')[0], 401)
        self.assertEqual(self.request('/api/v1/status')[0], 401)  # No peer header: no LAN access.
        # Bearer flow unchanged, and Bearer plus Cookie stays refused.
        bearer = {'Authorization': 'Bearer ' + self.key}
        self.assertEqual(self.lan('/api/v1/config', headers=bearer)[0], 200)
        self.assertEqual(self.lan('/api/v1/status', headers=dict(bearer, Cookie='a=b'))[0], 401)
        self.lan_path.unlink()
        self.assertEqual(self.lan('/api/v1/status')[0], 401)
        self.assertEqual(self.lan('/api/v1/status', headers=bearer)[0], 200)

    def test_keyless_whep_owned_per_source(self):
        session = str(uuid.uuid4())
        class Backend(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                self.send_response(201); self.send_header('Location', '/front/whep/' + session)
                self.send_header('Content-Length', '3'); self.end_headers(); self.wfile.write(b'sdp')
            def do_DELETE(self):
                self.send_response(200); self.send_header('Content-Length', '0'); self.end_headers()
        with ThreadingHTTPServer(('127.0.0.1', 0), Backend) as backend:
            thread = threading.Thread(target=backend.serve_forever, daemon=True); thread.start()
            self.server.state.config.setdefault('media', {})['webrtc_port'] = backend.server_port
            try:
                sdp = {'Content-Type': 'application/sdp'}
                status, headers, raw = self.lan('/media/front/whep', 'POST', headers=sdp, raw=b'offer')
                self.assertEqual(status, 201, raw)
                path = headers['Location']
                self.assertEqual(self.lan('/media/front/whep', 'POST', ip=OUTSIDE, headers=sdp, raw=b'offer')[0], 401)
                self.assertEqual(self.lan(path, 'DELETE', ip='192.168.3.21')[0], 404)
                self.assertEqual(self.lan(path, 'DELETE')[0], 200)
                self.assertFalse(self.server.grants.owns(path, 'lan-monitor:' + INSIDE))
            finally:
                backend.shutdown(); thread.join(3)


for _name in dir(test_auth.APIAuthTests):
    if _name.startswith('test_'):
        setattr(LanAPITests, _name, None)


class LanRTSPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        account_fixture(root / 'accounts.json')
        self.lan_path = root / 'lan-monitor.json'
        write(self.lan_path, dict(schema_version=1, enabled=True, allow_cidrs=['192.168.3.0/24']))
        self.policy = Authorization(root / 'accounts.json', root / 'audit.sqlite', None, self.lan_path)
        self.policy.lan.interval = 0
        self.assertTrue(self.policy.arm())

    def request(self, **kw):
        return dict(dict(ip=INSIDE, user='', password='', token='', action='read', path='front', protocol='rtsp',
                         id=str(uuid.uuid4()), query='', userAgent='ffmpeg'), **kw)

    def test_keyless_read_only_from_allowed_sources(self):
        for channel in ('front', 'back', 'left', 'right', 'down', 'sdi'):
            self.assertTrue(self.policy.authorize(self.request(path=channel)))
        for bad in (dict(ip=OUTSIDE), dict(action='publish'), dict(path='other'), dict(password='x', token='x'),
                    dict(query='a=b')):
            self.assertFalse(self.policy.authorize(self.request(**bad)), bad)
        write(self.lan_path, dict(schema_version=1, enabled=False, allow_cidrs=['192.168.3.0/24']))
        self.assertFalse(self.policy.authorize(self.request()))
        self.assertFalse(self.policy.ready)  # Policy change restarts the relay.


if __name__ == '__main__':
    unittest.main()
