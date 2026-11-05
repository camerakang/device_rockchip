#!/usr/bin/env python3
"""Run under an isolated user+mount namespace. Tests use tmpfs, NOT a physical disk."""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from ss07.volume import MountLease, VolumeError, mount_entries, open_directory


with tempfile.TemporaryDirectory(prefix='ss07-detach-test-') as tmp:
    subprocess.run(['mount', '--make-rprivate', '/'], check=True)
    subprocess.run(['mount', '-t', 'tmpfs', '-o', 'size=16m', 'ss07-test', tmp], check=True)
    detached = False
    try:
        path = Path(tmp) / 'recordings'; path.mkdir()
        with MountLease(open_directory(path), 'test-volume', 'synthetic_test') as volume:
            volume.target = tmp
            volume.expected_mount = next(e for e in mount_entries() if e['target'] == tmp)
            volume.writable = True
            assert volume.valid()
            volume.write_json('before.json', {'fixture': True})
            subprocess.run(['umount', '-l', tmp], check=True)
            detached = True
            assert not volume.valid()
            try:
                volume.write_json('blocked.json', {})
                raise AssertionError('write gate did not close')
            except VolumeError:
                pass
            # Even a native child still using its inherited FD can only reach the
            # detached test filesystem, never a same-named directory on the root disk.
            raw_path = '/proc/self/fd/%d/after-detach.txt' % volume.fd
            with open(raw_path, 'w') as stream:
                stream.write('synthetic test')
            assert not path.exists()
            assert Path(raw_path).read_text() == 'synthetic test'
        print(json.dumps({'evidence_kind': 'synthetic_test', 'filesystem': 'tmpfs',
                          'lazy_unmount_gate': 'passed', 'native_fd_no_system_disk_fallback': 'passed',
                          'hardware_verified': False}))
    finally:
        if not detached:
            subprocess.run(['umount', '-l', tmp], check=False)
