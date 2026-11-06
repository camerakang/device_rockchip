import configparser
import json
from pathlib import Path
import unittest

from ss07 import __version__
from ss07.node_service import avahi_service

ROOT = Path(__file__).resolve().parents[1]


class MobocomNodeReleaseTest(unittest.TestCase):
    def test_unit_is_independent_and_sandboxed(self):
        unit = configparser.ConfigParser(strict=False, interpolation=None)
        unit.optionxform = str
        unit.read(ROOT / 'node/ss07-mobocom-node.service')
        text = (ROOT / 'node/ss07-mobocom-node.service').read_text()
        for word in ('Requires=', 'BindsTo=', 'PartOf=', 'ss07.target', 'ss07-media', 'ss07-api.service'):
            self.assertNotIn(word, text.replace('# Independent', '').split('[Unit]', 1)[1])
        service = unit['Service']
        self.assertEqual((service['Restart'], service['RestartSec']), ('always', '1'))
        self.assertEqual((service['NoNewPrivileges'], service['ProtectSystem']), ('yes', 'strict'))
        self.assertEqual(service['ReadWritePaths'], '/var/lib/ss07')
        self.assertEqual(service['CapabilityBoundingSet'], '')
        self.assertIn('network-online.target', unit['Unit']['After'])

    def test_firmware_version_and_sdk_lock_match(self):
        self.assertIn('version = "%s"' % __version__, (ROOT / 'node/Cargo.toml').read_text())
        lock = json.loads((ROOT / 'mobocom-sdk.lock.json').read_text())
        manifest = json.loads((ROOT / lock['path'] / 'sdk-manifest.json').read_text())
        for key in ('version', 'protocol', 'content_sha256', 'source_commit'):
            self.assertEqual(lock[key], manifest[key])
        self.assertTrue((ROOT / lock['path'] / 'docs/ss07/ss07_node_vectors.json').is_file())

    def test_mdns_record_uses_decimal_uid(self):
        text = avahi_service(8455818509601193611)
        self.assertIn('<type>_mobocom._udp</type>', text)
        self.assertIn('<port>47821</port>', text)
        self.assertIn('uid=8455818509601193611', text)
        self.assertIn('SS07-35F3BE8B', text)
