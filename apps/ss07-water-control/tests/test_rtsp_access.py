import copy
import http.client
import json
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from ss07.accounts import Accounts, password_record
from ss07.config import ConfigError, ROOT, load as load_config
from ss07.persistence import atomic_json
from ss07.rtsp_access import Authorization, Callback, DEFAULT, Relay, check_ports, load, render
from ss07.state import native_access


class RTSPAccessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password = 'synthetic-test-password'
        cls.record = password_record(cls.password)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.accounts = self.root / 'accounts.json'
        self.data = dict(schema_version=1, users={name: dict(role=role, enabled=True, password=self.record)
            for name, role in [('admin', 'maintainer'), ('viewer', 'monitor'), ('reader', 'reader')]})
        self.save()
        self.policy = Authorization(self.accounts, self.root / 'audit.sqlite')
        self.assertTrue(self.policy.arm())
        self.config = load_config(ROOT / 'config/system.example.json')
        self.gateway = dict(schema_version=1, public_origin='https://127.0.0.1:8443', listen_ip='127.0.0.1',
                            ice_ip='127.0.0.1', certificate='/etc/ss07-tls/cert.pem', private_key='/etc/ss07-tls/key.pem')
        self.profile = dict(DEFAULT, enabled=True)

    def save(self): atomic_json(self.accounts, self.data, mode=0o640)

    def request(self, **kw):
        return dict(dict(ip='127.0.0.1', user='viewer', password=self.password, token=self.password,
                         action='read', path='front', protocol='rtsp', id=str(uuid.uuid4()), query='', userAgent='test'), **kw)

    def test_all_roles_can_read_and_handshake_reuses_one_kdf(self):
        from ss07.accounts import verify_password
        with patch('ss07.rtsp_access.verify_password', wraps=verify_password) as verify:
            for user in self.data['users']:
                for path in ('front', 'back', 'left', 'right', 'down', 'sdi'):
                    self.assertTrue(self.policy.authorize(self.request(user=user, path=path)))
            self.assertEqual(verify.call_count, 3)
        events = self.policy.audit.history()['items']
        self.assertEqual(sum(e['kind'] == 'RTSP_LOGIN_OK' for e in events), 3)
        self.assertNotIn(self.password, json.dumps(events))

    def test_no_localhost_or_token_publish_query_bypass(self):
        for kwargs in (dict(user='', password='', token=''), dict(token='jwt'), dict(query='token=jwt'),
                       dict(action='publish'), dict(action='api'), dict(path='other'), dict(protocol='webrtc'),
                       dict(id='not-an-id'), dict(user='viewer\r\nInjected'), dict(password='short', token='short')):
            self.assertFalse(self.policy.authorize(self.request(**kwargs)), kwargs)
        self.assertFalse(self.policy.authorize(dict(self.request(), unexpected=True)))
        self.assertFalse(self.policy.authorize(self.request(id=None)))
        self.assertFalse(self.policy.authorize(self.request(user='unknown')))

    def test_revision_change_requires_old_relay_teardown_before_rearming(self):
        self.assertTrue(self.policy.authorize(self.request()))
        self.data['users']['viewer']['enabled'] = False; self.save()
        self.assertFalse(self.policy.authorize(self.request()))
        self.assertFalse(self.policy.ready)
        self.assertIsNone(self.policy.refresh())
        self.assertTrue(self.policy.arm())
        self.assertFalse(self.policy.authorize(self.request()))
        self.assertTrue(self.policy.authorize(self.request(user='admin')))

    def test_account_file_loss_blocks_cached_grants(self):
        self.assertTrue(self.policy.authorize(self.request()))
        self.accounts.unlink()
        self.assertFalse(self.policy.authorize(self.request()))
        self.save()
        self.assertFalse(self.policy.authorize(self.request()))  # Explicit relay re-arm required.
        self.assertTrue(self.policy.arm())
        self.assertTrue(self.policy.authorize(self.request()))

    def test_revision_change_during_kdf_cannot_create_a_grant(self):
        entered, done = threading.Event(), threading.Event()
        def verify(*args): entered.set(); done.wait(5); return True
        result = []
        with patch('ss07.rtsp_access.verify_password', side_effect=verify):
            thread = threading.Thread(target=lambda: result.append(self.policy.authorize(self.request())))
            thread.start()
            try:
                self.assertTrue(entered.wait(2))
                self.data['users']['viewer']['role'] = 'reader'; self.save()
                self.assertIsNone(self.policy.refresh())
                self.assertTrue(self.policy.arm())
            finally: done.set(); thread.join(5)
        self.assertEqual(result, [False])
        self.assertEqual(self.policy.cache, {})

    def test_unknown_users_share_rate_limit_and_only_one_kdf_runs(self):
        with patch('ss07.rtsp_access.verify_password', return_value=False) as verify:
            for number in range(10):
                self.assertFalse(self.policy.authorize(self.request(user='missing%d' % number)))
            self.assertEqual(verify.call_count, 5)
        self.assertEqual(set(self.policy.attempts), {'unknown'})
        self.policy.kdf.acquire()
        try:
            with patch('ss07.rtsp_access.verify_password') as verify:
                self.assertFalse(self.policy.authorize(self.request(user='admin')))
                verify.assert_not_called()
        finally: self.policy.kdf.release()

    def test_six_simultaneous_tiles_share_one_password_check(self):
        entered, release = threading.Event(), threading.Event()
        barrier = threading.Barrier(6)
        results = []
        def verify(*args): entered.set(); release.wait(2); return True
        def connect(): barrier.wait(); results.append(self.policy.authorize(self.request()))
        with patch('ss07.rtsp_access.verify_password', side_effect=verify) as check:
            threads = [threading.Thread(target=connect) for _ in range(6)]
            for thread in threads: thread.start()
            try: self.assertTrue(entered.wait(2))
            finally:
                release.set()
                for thread in threads: thread.join(3)
            self.assertEqual(results, [True] * 6)
            self.assertEqual(check.call_count, 1)

    def test_cache_expiry_rechecks_password_and_audit_failure_revokes(self):
        self.assertTrue(self.policy.authorize(self.request()))
        self.policy.cache = {k: 0 for k in self.policy.cache}
        with patch.object(self.policy.audit, 'append', side_effect=OSError('disk full')):
            self.assertFalse(self.policy.authorize(self.request()))
        self.assertFalse(self.policy.ready)
        self.assertEqual(self.policy.reason, 'audit_unavailable')

    def test_callback_capability_bounded_body_and_no_redirects(self):
        callback = Callback(self.policy)
        self.addCleanup(callback.close)
        def send(path, data, headers=None):
            connection = http.client.HTTPConnection('127.0.0.1', callback.server_port, timeout=3)
            try:
                connection.request('POST', path, data, headers or {'Content-Type': 'application/json'})
                response = connection.getresponse(); raw = response.read()
                self.assertEqual(raw, b'')
                self.assertIsNone(response.getheader('Location'))
                return response.status
            finally: connection.close()
        raw = json.dumps(self.request())
        self.assertEqual(send('/wrong', raw), 403)
        self.assertEqual(send(callback.path, raw, {'Content-Type': 'text/plain'}), 403)
        self.assertEqual(send(callback.path, b'x' * 4097), 403)
        self.assertEqual(send(callback.path, '{broken'), 403)
        self.assertEqual(send(callback.path, raw), 204)
        self.data['users']['viewer']['enabled'] = False; self.save()
        self.assertEqual(send(callback.path, raw), 403)

    def test_profile_default_permissions_conflicts_and_strict_tls_relay(self):
        self.assertEqual(load(), DEFAULT)
        self.assertEqual(load(self.root / 'absent.json'), DEFAULT)
        path = self.root / 'rtsp.json'
        atomic_json(path, self.profile, mode=0o644)
        self.assertEqual(load(path), self.profile)
        path.chmod(0o666)
        with self.assertRaises(ConfigError): load(path)
        path.unlink(); path.symlink_to(self.accounts)
        with self.assertRaises(OSError): load(path)
        for port in (self.config['api']['port'], 8554, 8443, 9997, 19002):
            with self.assertRaises(ConfigError): check_ports(self.config, self.gateway, dict(self.profile, port=port))
        value = render(self.config, self.gateway, self.profile, 'http://127.0.0.1:12345/private')
        self.assertEqual(value['rtspEncryption'], 'strict')
        self.assertFalse(value['api'])
        self.assertFalse(value['webrtc'])
        self.assertTrue(value['pathDefaults']['sourceOnDemand'])
        self.assertEqual(len(value['paths']), 6)

    def test_disabled_relay_opens_no_callback_and_needs_no_credentials(self):
        with patch('ss07.rtsp_access.Callback') as callback:
            relay = Relay(self.config, None, DEFAULT, None, None, '/nonexistent', self.root)
            relay.tick(); relay.close()
            callback.assert_not_called()
            self.assertEqual(relay.snapshot()['state'], 'DISABLED')

    def test_state_never_publishes_a_stale_or_credential_bearing_url(self):
        worker = dict(state='RUNNING', payload={'native_rtsp': dict(enabled=True, state='READY', base_url='rtsps://host:8322')})
        self.assertEqual(native_access(worker)['state'], 'READY')
        worker['state'] = 'STALE'
        self.assertIsNone(native_access(worker)['base_url'])
        worker['state'] = 'RUNNING'
        for url in ('rtsps://user:secret@host:8322', 'rtsps://host:8322/path', 'http://host:8322'):
            worker['payload']['native_rtsp']['base_url'] = url
            self.assertIsNone(native_access(worker)['base_url'])


if __name__ == '__main__': unittest.main()
