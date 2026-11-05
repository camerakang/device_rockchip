"""Machine API clients (mobocon): Bearer HTTPS access and RTSP pull credentials."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from ss07.api_clients import ApiClients
from ss07.config import ConfigError, ROOT, load as load_config
from ss07.persistence import RevisionConflict, atomic_json
from ss07.rtsp_access import Authorization, DEFAULT, check_ports, render, validate
from ss07.state import native_access
import test_auth
from test_auth import account_fixture


class ApiClientFileTests(unittest.TestCase):
    def test_key_shown_once_stored_as_digest_and_lifecycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'api-clients.json'; clients = ApiClients(path)
            self.assertEqual(clients.read()['clients'], {})  # Missing file: no clients.
            key = clients.add('mobocon', 'maintainer')
            self.assertNotIn(key, path.read_text())
            self.assertEqual(clients.bearer(key)[0], 'mobocon')
            self.assertIsNotNone(clients.verify('mobocon', key))
            self.assertIsNone(clients.verify('other', key))
            self.assertIsNone(clients.bearer('x' * 43))
            with self.assertRaises(RevisionConflict): clients.add('mobocon', 'monitor')
            clients.set('mobocon', enabled=False); self.assertIsNone(clients.bearer(key))
            clients.set('mobocon', enabled=True); new = clients.rotate('mobocon')
            self.assertIsNone(clients.bearer(key)); self.assertIsNotNone(clients.bearer(new))
            path.chmod(0o644)
            with self.assertRaises(ConfigError): clients.read()
            path.chmod(0o640); clients.remove('mobocon')
            self.assertEqual(clients.public()['clients'], {})

    def test_cli_prints_key_once_and_list_hides_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / 'api-clients.json')
            run = lambda *a: subprocess.run([sys.executable, '-m', 'ss07', 'api-client', *a, '--api-clients', path],
                                            capture_output=True, text=True, timeout=10)
            added = run('add', '--name', 'mobocon')
            self.assertEqual(added.returncode, 0, added.stderr)
            key = json.loads(added.stdout)['key']
            listed = run('list')
            self.assertNotIn(key, listed.stdout)
            self.assertEqual(json.loads(listed.stdout)['clients']['mobocon'], {'role': 'maintainer', 'enabled': True})


class BearerAPITests(test_auth.APIAuthTests):
    def setUp(self):
        super().setUp()
        self.clients = ApiClients(self.base / 'api-clients.json')
        self.server.api_clients = self.clients
        self.key = self.clients.add('mobocon', 'maintainer')
        self.viewer = self.clients.add('wall', 'monitor')

    def bearer(self, path, method='GET', body=None, key=None, headers=None, body_raw=None):
        sent = dict({'Authorization': 'Bearer ' + (key or self.key)}, **(headers or {}))
        if body_raw is None: return self.request(path, method, body, headers=sent)
        import http.client
        sent['Host'] = self.auth.authority
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=4)
        try:
            connection.request(method, path, body_raw, sent); response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally: connection.close()

    def test_bearer_reads_and_writes_config_without_browser_csrf(self):
        status, _, raw = self.bearer('/api/v1/config')
        self.assertEqual(status, 200, raw)
        current = json.loads(raw)['data']
        streams = json.loads(self.bearer('/api/v1/streams')[2])['data']['streams']
        self.assertEqual(len(streams), 6); self.assertTrue(all('rtsp_url' in s for s in streams))
        with patch.object(self.server.maintenance, 'request', return_value={'data': {}, 'request_id': 'test'}) as op:
            self.assertEqual(self.bearer('/api/v1/config/plan', 'POST', {})[0], 200)
            self.assertEqual(op.call_args.args[0], {'username': 'mobocon', 'role': 'maintainer'})
        self.assertIn('revision', current)

    def test_bad_disabled_role_cookie_mix_and_media_are_rejected(self):
        self.assertEqual(self.bearer('/api/v1/status', key='y' * 43)[0], 401)
        self.assertEqual(self.request('/api/v1/status', headers={'Authorization': 'Basic abc'})[0], 401)
        self.assertEqual(self.bearer('/api/v1/status', key=self.viewer)[0], 200)
        self.assertEqual(self.bearer('/api/v1/config', key=self.viewer)[0], 403)
        self.assertEqual(self.bearer('/api/v1/config/plan', 'POST', {}, key=self.viewer)[0], 403)
        self.assertEqual(self.bearer('/api/v1/status', headers={'Cookie': 'a=b'})[0], 401)
        self.assertEqual(self.bearer('/api/v1/logout', 'POST', {})[0], 422)
        self.clients.set('mobocon', enabled=False)
        self.assertEqual(self.bearer('/api/v1/status')[0], 401)
        self.server.api_clients = None
        self.assertEqual(self.bearer('/api/v1/status', key=self.viewer)[0], 401)


    def test_bearer_webrtc_session_ownership_and_rotation_revokes(self):
        session = '11111111-2222-3333-4444-555555555555'
        deleted = []
        class Backend(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                self.send_response(201); self.send_header('Location', '/front/whep/' + session)
                self.send_header('Content-Length', '3'); self.end_headers(); self.wfile.write(b'sdp')
            def do_DELETE(self):
                deleted.append(self.path)
                self.send_response(200); self.send_header('Content-Length', '0'); self.end_headers()
        with ThreadingHTTPServer(('127.0.0.1', 0), Backend) as backend:
            thread = threading.Thread(target=backend.serve_forever, daemon=True); thread.start()
            self.server.state.config.setdefault('media', {})['webrtc_port'] = backend.server_port
            try:
                sdp = {'Content-Type': 'application/sdp', 'Origin': 'app://mobocon'}
                status, headers, raw = self.bearer('/media/front/whep', 'POST', None, headers=sdp, body_raw=b'offer')
                self.assertEqual((status, headers.get('Location')), (201, '/media/front/whep/' + session), raw)
                path = '/media/front/whep/' + session
                self.assertEqual(self.bearer(path, 'DELETE', key=self.viewer)[0], 404)
                self.clients.rotate('mobocon')
                deadline = time.monotonic() + 5
                while path not in ['/media' + p for p in deleted] and time.monotonic() < deadline: time.sleep(.1)
                self.assertIn('/front/whep/' + session, deleted)
                self.assertFalse(self.server.grants.owns(path, 'api-client:mobocon'))
            finally:
                backend.shutdown(); thread.join(3)


for _name in dir(test_auth.APIAuthTests):
    if _name.startswith('test_'):
        setattr(BearerAPITests, _name, None)


class RTSPClientTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        account_fixture(self.root / 'accounts.json')
        self.clients = ApiClients(self.root / 'api-clients.json')
        self.key = self.clients.add('mobocon', 'reader')
        self.policy = Authorization(self.root / 'accounts.json', self.root / 'audit.sqlite', self.root / 'api-clients.json')
        self.assertTrue(self.policy.arm())
        self.config = load_config(ROOT / 'config/system.example.json')
        self.gateway = dict(schema_version=1, public_origin='https://127.0.0.1:8443', listen_ip='127.0.0.1',
                            ice_ip='127.0.0.1', certificate='/c.pem', private_key='/k.pem')

    def request(self, **kw):
        password = kw.pop('password', self.key)
        return dict(dict(ip='127.0.0.1', user='mobocon', password=password, token=password, action='read',
                         path='front', protocol='rtsp', id=str(uuid.uuid4()), query='', userAgent='ffmpeg'), **kw)

    def test_client_key_reads_without_kdf_and_revocation_disarms(self):
        with patch('ss07.rtsp_access.verify_password', side_effect=AssertionError('no KDF for keys')):
            self.assertTrue(self.policy.authorize(self.request()))
            self.assertFalse(self.policy.authorize(self.request(password='z' * 43)))
            self.assertFalse(self.policy.authorize(self.request(action='publish')))
        self.clients.set('mobocon', enabled=False)  # Any credential change closes the relay.
        self.assertFalse(self.policy.authorize(self.request()))
        self.assertFalse(self.policy.ready)

    def test_plain_port_profile_render_and_conflicts(self):
        profile = dict(DEFAULT, enabled=True, plain_port=8555)
        self.assertEqual(validate(profile), profile)
        for bad in (dict(profile, plain_port=8322), dict(profile, plain_port=80), dict(profile, plain_port='8555')):
            with self.assertRaises(ConfigError): validate(bad)
        with self.assertRaises(ConfigError): check_ports(self.config, self.gateway, dict(profile, plain_port=8554))
        value = render(self.config, self.gateway, profile, 'http://127.0.0.1:1/x')
        self.assertEqual((value['rtspEncryption'], value['rtspAddress']), ('optional', '127.0.0.1:8555'))
        self.assertEqual(value['rtspTransports'], ['tcp'])
        self.assertEqual(render(self.config, self.gateway, dict(DEFAULT, enabled=True), 'http://x/y')['rtspEncryption'], 'strict')
        worker = dict(state='RUNNING', payload={'native_rtsp': dict(enabled=True, state='READY', base_url='rtsps://h:8322',
                                                                     plain_base_url='rtsp://h:8555')})
        self.assertEqual(native_access(worker)['plain_base_url'], 'rtsp://h:8555')
        worker['payload']['native_rtsp']['plain_base_url'] = 'rtsp://u:p@h:8555'
        self.assertIsNone(native_access(worker)['plain_base_url'])
