"""Check real OpenSSL TLS material and explicit missing-device provisioning."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from ss07.accounts import password_record
from ss07.provisioning import ProvisionError, check, validate_tls
from ss07.release_restore import write_file

ROOT = Path(__file__).resolve().parents[1]


class ProvisioningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(); cls.addClassCleanup(cls.temp.cleanup)
        cls.directory = Path(cls.temp.name)
        cls.accounts = json.dumps(dict(schema_version=1, users={'admin': dict(role='maintainer', enabled=True,
                                                   password=password_record('only-for-software-test'))})).encode()
        for name, san in (('good', 'IP:127.0.0.1'), ('other', 'DNS:other.test')):
            subprocess.run(['/usr/bin/openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                '-subj', '/CN=software.test', '-addext', 'subjectAltName=' + san,
                '-out', str(cls.directory / (name + '.crt')), '-keyout', str(cls.directory / (name + '.key'))],
                check=True, timeout=20, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def setUp(self):
        self.temp_root = tempfile.TemporaryDirectory(); self.addCleanup(self.temp_root.cleanup)
        self.root = Path(self.temp_root.name)
        self.groups = {name: os.getegid() for name in ('ss07-web', 'ss07-gateway')}
        config = (ROOT / 'config/system.example.json').read_bytes()
        for name in ('etc/ss07/system.json', 'etc/ss07-active/system.json'): write_file(self.root / name, config, 0o644)
        write_file(self.root / 'etc/ss07-credentials/accounts.json', self.accounts, 0o640)
        write_file(self.root / 'etc/ss07-tls/server.crt', (self.directory / 'good.crt').read_bytes(), 0o644)
        write_file(self.root / 'etc/ss07-tls/server.key', (self.directory / 'good.key').read_bytes(), 0o640)
        gateway = dict(schema_version=1, public_origin='https://127.0.0.1:8443', listen_ip='127.0.0.1', ice_ip='127.0.0.1',
                       certificate='/etc/ss07-tls/server.crt', private_key='/etc/ss07-tls/server.key')
        write_file(self.root / 'etc/ss07-gateway/network.json', json.dumps(gateway).encode(), 0o644)
        for path in self.root.rglob('*'):
            if path.is_dir(): path.chmod(0o755)

    def ready(self): return check(self.root, self.groups, {'127.0.0.1'})

    def test_missing_hardware_remains_unverified_with_valid_explicit_credentials(self):
        result = self.ready()
        self.assertTrue(result['ready']); self.assertFalse(result['hardware_verified'])
        self.assertFalse(result['client_certificate_trust_verified']); self.assertFalse(result['recording_enabled'])
        self.assertFalse((self.root / 'etc/ss07-provisioned').exists())

    def test_wrong_host_key_pair_or_interface_are_rejected(self):
        cert = (self.directory / 'good.crt').read_bytes(); key = (self.directory / 'good.key').read_bytes()
        with self.assertRaises(ProvisionError): validate_tls(cert, key, '192.168.3.47')
        with self.assertRaises(ProvisionError): validate_tls(cert, (self.directory / 'other.key').read_bytes(), '127.0.0.1')
        with self.assertRaisesRegex(ProvisionError, 'local_interface'): check(self.root, self.groups, {'192.168.3.233'})

    def test_missing_accounts_pending_configuration_and_secret_permissions_fail_closed(self):
        path = self.root / 'etc/ss07-credentials/accounts.json'; path.unlink()
        with self.assertRaises(FileNotFoundError): self.ready()
        write_file(path, self.accounts, 0o644)
        with self.assertRaisesRegex(ProvisionError, 'mode_invalid'): self.ready()
        path.chmod(0o640)
        configpath = self.root / 'etc/ss07/system.json'; config = json.loads(configpath.read_text()); config['device_id'] = 'changed'
        write_file(configpath, json.dumps(config).encode(), 0o644)
        with self.assertRaisesRegex(ProvisionError, 'pending_configuration'): self.ready()

    def test_secret_symlink_or_writable_parent_is_rejected(self):
        path = self.root / 'etc/ss07-tls/server.key'; raw = path.read_bytes(); path.unlink(); path.symlink_to(self.directory / 'good.key')
        with self.assertRaises((OSError, ProvisionError)): self.ready()
        path.unlink(); write_file(path, raw, 0o640); path.parent.chmod(0o777)
        with self.assertRaisesRegex(ProvisionError, 'directory_permissions'): self.ready()


if __name__ == '__main__': unittest.main()
