import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

import test_provisioning
from ss07 import gateway, media, rtsp_access
from ss07.auth import Auth
from ss07.config import ConfigError
from ss07.network_sync import CONFIG, KEY, CERT, LIVE, policy, sync
from ss07.provisioning import check, validate_tls


class DynamicTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = test_provisioning.ProvisioningTests
        cls.fixture.setUpClass()
        cls.addClassCleanup(cls.fixture.doClassCleanups)

    def setUp(self):
        f = self.fixture()
        f.setUp()
        self.addCleanup(f.doCleanups)
        self.root, self.groups = f.root, f.groups
        (self.root / 'run').mkdir(mode=0o755)
        self.path = self.root / CONFIG
        self.path.write_text(json.dumps(dict(schema_version=2, interface='eth0')))
        self.key = self.root / KEY[1:]
        self.cert = self.root / CERT[1:]

    def change(self, ip=None):
        addrs = ['127.0.0.1','169.254.1.2'] + ([ip] if ip else [])
        return sync(self.root, [dict(ifname='eth0',flags=['UP'],addr_info=[
            dict(local=a,scope='global',family='inet') for a in addrs])],os.getegid(),os.geteuid())

    def test_move_and_disconnect(self):
        original_key = self.key.read_bytes()
        previous = None
        ca = None
        for ip in ('192.168.3.233','10.10.32.213'):
            self.assertEqual(self.change(ip),ip)
            current_ca = (self.cert.parent / 'network-ca.crt').read_bytes()
            if ca is not None: self.assertEqual(ca, current_ca)
            ca = current_ca
            self.assertEqual((self.cert.parent / 'network-ca.key').stat().st_mode & 0o777, 0o600)
            g = gateway.load(self.path,root=self.root)
            config=json.loads((self.root/'etc/ss07/system.json').read_text())
            origin='https://'+ip+':8443'
            self.assertEqual(check(self.root,self.groups,{ip})['public_origin'],origin)
            self.assertIn('bind '+ip,gateway.render(config,g))
            mt=media.render(config,g)
            self.assertEqual((mt['webrtcAllowOrigins'],mt['webrtcLocalUDPAddress'],mt['webrtcAdditionalHosts']),
                             ([origin],ip+':8189',[ip]))
            self.assertEqual(Auth(None,None,g['public_origin'],direct_access=True).origin,origin)
            relay=rtsp_access.render(config,g,dict(schema_version=1,enabled=True,port=8322,plain_port=8555),'http://localhost')
            self.assertEqual((relay['rtspsAddress'],relay['rtspAddress']),(ip+':8322',ip+':8555'))
            blob=self.cert.read_bytes()
            validate_tls(blob,original_key,'127.0.0.1')
            self.assertNotEqual(previous,blob)
            self.change(ip)
            self.assertEqual(self.cert.read_bytes(),blob)
            previous=blob
        self.assertEqual(original_key,self.key.read_bytes())
        self.assertIsNone(self.change())
        self.assertFalse((self.root/LIVE).exists())
        with self.assertRaisesRegex(ConfigError,'awaiting_interface'):
            gateway.load(self.path,root=self.root)
        with patch('ss07.gateway.wait_for_address',side_effect=lambda _:self.change('10.10.32.214')):
            self.assertEqual(gateway.load(self.path,root=self.root,wait=5)['listen_ip'],'10.10.32.214')

    def test_unsafe_files_never_rotated(self):
        for path, mode in ((self.key,0o644),(self.cert,0o666),(self.path,0o666)):
            before = path.stat().st_mode
            path.chmod(mode)
            with self.assertRaises(ConfigError): self.change('10.10.32.213')
            path.chmod(before)
        with self.assertRaises(ConfigError): policy(dict(schema_version=2, interface='../eth0'))
