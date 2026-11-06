import json
import tempfile
import unittest
from pathlib import Path
from tools.package_sdk import install, verify, sha

class PackageTests(unittest.TestCase):
    def test_existing_consumer_path_and_traversal(self):
        with tempfile.TemporaryDirectory() as temporary:
            project=Path(temporary)
            files={'sample.hpp':b'example\n'}
            hashes={name:sha(data) for name,data in files.items()}
            manifest=dict(schema=2,version='test',protocol='0x21',timecode_protocol='0x12',source_commit='test',source_dirty=True,files=hashes,content_sha256=sha(json.dumps(hashes,sort_keys=True,separators=(',',':')).encode()))
            files['sdk-manifest.json']=json.dumps(manifest).encode()
            (project/'mobocom-sdk.lock.json').write_text(json.dumps(dict(manifest,path='lib/device_protocol')))
            install(project,files,manifest)
            self.assertEqual(verify(project/'lib/device_protocol')['version'],'test')
            self.assertFalse((project/'lib/mobocom_protocol').exists())
            install(project,files,manifest)
            (project/'mobocom-sdk.lock.json').write_text(json.dumps({'path':'../escape'}))
            with self.assertRaises(ValueError):install(project,files,manifest)

    def test_upgrade_from_managed_v21_and_reject_unknown_protocol(self):
        def snapshot(protocol,version):
            files={'sample.hpp':version.encode()}
            hashes={name:sha(data) for name,data in files.items()}
            manifest=dict(schema=2,version=version,protocol=protocol,timecode_protocol='0x12',source_commit='test',source_dirty=True,files=hashes,content_sha256=sha(json.dumps(hashes,sort_keys=True,separators=(',',':')).encode()))
            files['sdk-manifest.json']=json.dumps(manifest).encode()
            return files,manifest
        with tempfile.TemporaryDirectory() as temporary:
            project=Path(temporary)
            install(project,*snapshot('0x21','2.1.0'))
            install(project,*snapshot('0x22','2.2.0-network.1'))
            self.assertEqual(verify(project/'lib/mobocom_protocol')['protocol'],'0x22')
            files,manifest=snapshot('0x99','bad')
            (project/'lib/mobocom_protocol/sdk-manifest.json').write_bytes(files['sdk-manifest.json'])
            with self.assertRaisesRegex(ValueError,'not a managed'):verify(project/'lib/mobocom_protocol')
