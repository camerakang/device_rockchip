#!/usr/bin/env python3
"""Extract hash-pinned ARM64 build/test packages. No host/board package installation."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sdk', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True, help='new sysroot directory')
    parser.add_argument('--cache', type=Path, required=True, help='download cache; existing verified .deb files may be reused')
    args = parser.parse_args()
    dest = args.output.absolute()
    if dest.exists() or dest.is_symlink():
        parser.error('output must not exist')
    lockfile = ROOT / 'packaging/arm64-sysroot.lock.json'
    lock = json.loads(lockfile.read_text())
    args.cache.mkdir(parents=True, exist_ok=True)
    verified = []
    for package in lock['packages']:
        if 'sdk_path' in package:
            path = args.sdk.resolve() / package['sdk_path']
        else:
            path = args.cache / package['filename']
            if not path.exists():
                # Exclusive creation and removal on failure avoid treating an interrupted download as a cache hit.
                partial = path.with_name(path.name + '.partial')
                if partial.exists():
                    parser.error('remove/review interrupted download: ' + str(partial))
                try:
                    with urllib.request.urlopen(package['url'], timeout=60) as response, partial.open('xb') as out:
                        total = 0
                        while True:
                            chunk = response.read(1024 * 1024)
                            if not chunk:
                                break
                            total += len(chunk)
                            if total > package['size']:
                                raise ValueError('download exceeds pinned package size')
                            out.write(chunk)
                    if digest(partial) != package['sha256']:
                        raise ValueError('download checksum mismatch: ' + package['filename'])
                    partial.rename(path)
                except BaseException:
                    if partial.exists():
                        partial.unlink()
                    raise
        if not path.is_file() or digest(path) != package['sha256']:
            parser.error('missing or mismatched package: ' + str(path))
        arch = subprocess.check_output(['dpkg-deb', '-f', str(path), 'Architecture'], text=True).strip()
        if arch not in ('all', 'arm64'):
            parser.error('non-ARM64 package: ' + str(path))
        verified.append(path)
    dest.mkdir(parents=True)
    for path in verified:
        subprocess.run(['dpkg-deb', '-x', str(path), str(dest)], check=True)
    # Absolute symlinks are valid on the target but would escape a cross sysroot.
    converted = []
    for directory, dirs, files in os.walk(str(dest), followlinks=False):
        for name in dirs + files:
            path = Path(directory) / name
            if path.is_symlink():
                target = os.readlink(str(path))
                if target.startswith('/'):
                    path.unlink()
                    path.symlink_to(os.path.relpath(str(dest / target.lstrip('/')), str(path.parent)))
                    converted.append(str(path.relative_to(dest)))
    (dest / 'ss07-sysroot.json').write_text(json.dumps({
        'artifact': 'ss07-arm64-build-test-sysroot', 'installable_rootfs': False,
        'architecture': 'arm64', 'lock_sha256': digest(lockfile),
        'package_count': len(verified), 'absolute_symlinks_converted': converted,
        'gstreamer_core': '1.18.5-1', 'plugins': 'Ubuntu focal 1.16.3; see lock',
        'package_scripts_executed': False, 'hardware_validated': False,
    }, indent=2) + '\n')
    print(dest)


if __name__ == '__main__':
    main()
