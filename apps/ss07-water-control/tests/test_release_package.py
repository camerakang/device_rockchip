"""Import untrusted archive structure without executing code or touching a live root."""
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import unittest

from ss07.release_contract import BOOTSTRAP_FILES, DEFAULT_FILES, SYSTEM_FILES, contract
from ss07.release_package import PackageError, digest, rename_new, unpack


def fixture(path, version='0.8.0', modify=None, members=None):
    prefix = 'ss07-' + version + '-linux-arm64'
    release = 'opt/ss07/releases/' + version + '/'
    files = {name: b'fixture only\n' for name in SYSTEM_FILES + DEFAULT_FILES + BOOTSTRAP_FILES}
    files.update({release + name: b'fixture only\n' for name in (
        'tools/ss07', 'src/ss07/__init__.py', 'config/system.schema.json', 'bin/ss07-camera', 'bin/mediamtx', 'bin/caddy')})
    value = dict(artifact='ss07-arm64-application', version=version, architecture='arm64', firmware_image=False,
                 services_enabled=False, deployment=contract(), files={n: hashlib.sha256(v).hexdigest() for n, v in files.items()},
                 symlinks={'opt/ss07/current': 'releases/' + version})
    if modify: modify(value, files)
    with tarfile.open(path, 'w:gz') as tar:
        for name, raw in dict(files, **{'SS07-MANIFEST.json': json.dumps(value).encode()}).items():
            item = tarfile.TarInfo(prefix + '/' + name); item.mode = 0o775 if '/bin/' in name else 0o664; item.size = len(raw)
            tar.addfile(item, io.BytesIO(raw))
        link = tarfile.TarInfo(prefix + '/opt/ss07/current'); link.type = tarfile.SYMTYPE; link.linkname = 'releases/' + version
        tar.addfile(link)
        if members: members(tar, prefix)
    return digest(path)


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.archive = self.root / 'application.tar.gz'; self.target = self.root / 'import'

    def rejects(self, **kwargs):
        checksum = fixture(self.archive, **kwargs)
        with self.assertRaises((ValueError, OSError)): unpack(self.archive, checksum, self.target)
        self.assertFalse(self.target.exists())
        self.assertEqual(list(self.root.glob('.ss07-import-*')), [])

    def test_valid_import_exact_hashes_normalized_permissions_and_no_existing_replacement(self):
        checksum = fixture(self.archive)
        result = unpack(self.archive, checksum, self.target)
        self.assertEqual(result['deployment'], contract())
        self.assertEqual(result['archive_sha256'], checksum)
        self.assertEqual((self.target / 'opt/ss07/releases/0.8.0/bin/caddy').stat().st_mode & 0o777, 0o755)
        self.assertEqual((self.target / 'etc/ss07/system.json').stat().st_mode & 0o777, 0o644)
        with self.assertRaises(PackageError): unpack(self.archive, checksum, self.target)

    def test_digest_mismatch_publishes_nothing(self):
        fixture(self.archive)
        with self.assertRaisesRegex(PackageError, 'checksum_mismatch'): unpack(self.archive, '0' * 64, self.target)
        self.assertFalse(self.target.exists())

    def test_extra_unlisted_file_and_modified_content_fail(self):
        for modify in (lambda v, f: f.update({'etc/ssh/sshd_config': b'no'}),
                       lambda v, f: f.update({DEFAULT_FILES[0]: b'changed'})):
            with self.subTest(modify=modify): self.rejects(modify=modify)

    def test_paths_and_links_cannot_escape_private_tree(self):
        def malicious(kind):
            def add(tar, prefix):
                item = tarfile.TarInfo(prefix + '/../../outside' if kind == 'traversal' else prefix + '/extra')
                if kind == 'symlink': item.type = tarfile.SYMTYPE; item.linkname = '/etc'
                if kind == 'hardlink': item.type = tarfile.LNKTYPE; item.linkname = prefix + '/etc/ss07/system.json'
                if kind == 'device': item.type = tarfile.CHRTYPE
                tar.addfile(item)
            return add
        for kind in ('traversal', 'symlink', 'hardlink', 'device'):
            with self.subTest(kind=kind): self.rejects(members=malicious(kind))
        self.assertFalse((self.root / 'outside').exists())

    def test_duplicate_paths_and_special_modes_are_refused(self):
        def duplicate(tar, prefix):
            item = tarfile.TarInfo(prefix + '/' + DEFAULT_FILES[0]); tar.addfile(item)
        def privileged(tar, prefix):
            item = tarfile.TarInfo(prefix + '/special'); item.mode = 0o4755; tar.addfile(item)
        for add in (duplicate, privileged):
            with self.subTest(add=add): self.rejects(members=add)

    def test_missing_file_contract_or_wrong_version_refused(self):
        for modify in (lambda v, f: v.pop('deployment'), lambda v, f: v.update(version='0.9.0'),
                       lambda v, f: v['deployment']['data'].update(catalog=True),
                       lambda v, f: v['files'].pop(SYSTEM_FILES[0])):
            with self.subTest(modify=modify): self.rejects(modify=modify)

    def test_atomic_publish_never_overwrites_even_an_empty_destination(self):
        first, second = self.root / 'first', self.root / 'second'
        first.mkdir(); second.mkdir()
        with self.assertRaises(FileExistsError): rename_new(first, second)
        self.assertTrue(first.is_dir()); self.assertTrue(second.is_dir())

    def test_pre_sonar_configuration_contract_is_not_assumed_compatible(self):
        def previous_contract(value,files):
            value['deployment']['data']=dict(config=1,accounts=1,catalog=2,recording_metadata=1,timecode_journal=1,
                audit=1,application_journal=1,worker_snapshot=1)
        self.rejects(modify=previous_contract)


if __name__ == '__main__': unittest.main()
