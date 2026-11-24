import json
import os
from pathlib import Path
import tempfile
import unittest

from ss07.sdi import SDI_LIVE, load_encoder, resolve_sdi_url
from ss07.sdi_discovery import discover, sync_sdi


class SdiDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='ss07-sdi-disc-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'etc/ss07-active').mkdir(parents=True)
        (self.root / 'etc/ss07-credentials').mkdir(parents=True)
        (self.root / 'run').mkdir(mode=0o755)
        cfg = self.root / 'etc/ss07-active/system.json'
        cfg.write_text(json.dumps({'sdi': {'rtsp_url': 'rtsp://192.168.3.47:5554/live/av'}}))
        cfg.chmod(0o644)
        self.cred = self.root / 'etc/ss07-credentials/sdi-encoder.json'
        self.cred.write_text(json.dumps({
            'schema_version': 1, 'model': 'avmatrix-se2017',
            'username': 'admin', 'password': 'secret',
        }))
        self.cred.chmod(0o640)
        self.interfaces = [{
            'ifname': 'eth0', 'flags': ['UP'],
            'addr_info': [{'family': 'inet', 'scope': 'global', 'local': '10.10.32.213', 'prefixlen': 24}],
        }]

    def test_resolve_sdi_url(self):
        live = self.root / SDI_LIVE.lstrip('/')
        live.parent.mkdir(parents=True, exist_ok=True)
        url = 'rtsp://192.168.3.47:5554/live/av'
        self.assertEqual(resolve_sdi_url(url, live_path=live), url)
        live.write_text(json.dumps({'host': '10.10.32.157'}))
        live.chmod(0o644)
        self.assertEqual(resolve_sdi_url(url, live_path=live), 'rtsp://10.10.32.157:5554/live/av')
        live.chmod(0o666)
        self.assertEqual(resolve_sdi_url(url, live_path=live), url)

    def test_discover_and_sync_sdi_subnet_scan(self):
        probe = lambda host, creds, rtsp_port=5554: host == '10.10.32.157'
        tcp = lambda host, port: host in ('10.10.32.157', '10.10.32.99')
        neigh = lambda: {'10.10.32.157': '3c:6a:2c:d5:7f:88'}
        host, changed = sync_sdi(
            self.root, self.interfaces, interface_name='eth0', uid=os.geteuid(),
            probe=probe, tcp_open=tcp, read_neigh=neigh,
        )
        self.assertEqual((host, changed), ('10.10.32.157', True))
        live = self.root / SDI_LIVE.lstrip('/')
        self.assertEqual(resolve_sdi_url('rtsp://192.168.3.47:5554/live/av', live_path=live),
                         'rtsp://10.10.32.157:5554/live/av')
        # Second call is idempotent (changed=False).
        host2, changed2 = sync_sdi(
            self.root, self.interfaces, interface_name='eth0', uid=os.geteuid(),
            probe=probe, tcp_open=tcp, read_neigh=neigh,
        )
        self.assertEqual((host2, changed2), ('10.10.32.157', False))

    def test_ambiguous_without_mac_and_pinned_with_mac(self):
        creds = load_encoder(self.cred)
        probe = lambda host, c, rtsp_port=5554: host in ('10.10.32.157', '10.10.32.158')
        tcp = lambda host, port: host in ('10.10.32.157', '10.10.32.158')
        neigh = lambda: {'10.10.32.157': '3c:6a:2c:d5:7f:88', '10.10.32.158': 'aa:bb:cc:dd:ee:ff'}
        self.assertEqual(
            discover('192.168.3.47', creds, self.interfaces, interface_name='eth0',
                     probe=probe, tcp_open=tcp, read_neigh=neigh),
            (None, None),
        )
        pinned = dict(creds, mac='3c:6a:2c:d5:7f:88')
        self.assertEqual(
            discover('192.168.3.47', pinned, self.interfaces, interface_name='eth0',
                     probe=probe, tcp_open=tcp, read_neigh=neigh),
            ('10.10.32.157', '3c:6a:2c:d5:7f:88'),
        )
