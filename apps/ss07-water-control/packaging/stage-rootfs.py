#!/usr/bin/env python3
"""Create a new staging tree only. Never install, enable a unit or change the host."""
import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from ss07 import __version__
from ss07.config import load
from ss07.release_install import write_bootstrap


def architecture(path):
    with path.open('rb') as stream:
        header = stream.read(20)
    if len(header) < 20 or header[:6] != b'\x7fELF\x02\x01':
        raise ValueError('expected a little-endian ELF64 binary')
    arch = {62: 'amd64', 183: 'arm64'}.get(int.from_bytes(header[18:20], 'little'))
    if arch is None:
        raise ValueError('unsupported binary architecture')
    return arch


def verified_asset(directory, component):
    manifest = json.loads((directory / 'manifest.json').read_text())
    digest = hashlib.sha256((directory / component).read_bytes()).hexdigest()
    arch = architecture(directory / component)
    lock = json.loads((ROOT / 'packaging/dependencies.lock.json').read_text())[component]
    expected = lock['assets']['linux_' + arch]
    if (manifest.get('version') != lock['version'] or digest != manifest.get('binary_sha256')
            or digest != expected['binary_sha256'] or manifest.get('archive_sha256') != expected['sha256']
            or manifest.get('platform') != 'linux_' + arch):
        raise ValueError(component + ' manifest/checksum mismatch')
    return arch


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destdir', required=True, type=Path)
    parser.add_argument('--native-dir', type=Path, help='optional target-architecture ss07-camera binary directory')
    parser.add_argument('--media-dir', type=Path, help='optional verified fetch-media.py output')
    parser.add_argument('--gateway-dir', type=Path, help='optional verified fetch-media.py --component caddy output')
    args = parser.parse_args()
    dest = args.destdir.absolute()
    if dest.exists() or dest.is_symlink():
        parser.error('destdir must be a new staging directory; existing roots are refused')
    if args.native_dir and not (args.native_dir / 'ss07-camera').is_file():
        parser.error('native-dir must contain ss07-camera')
    native_arch = architecture(args.native_dir / 'ss07-camera') if args.native_dir else None
    arches = {native_arch} if native_arch else set()
    try:
        for directory, component in ((args.media_dir, 'mediamtx'), (args.gateway_dir, 'caddy')):
            if directory: arches.add(verified_asset(directory, component))
    except (OSError, ValueError, KeyError) as exc: parser.error(str(exc))
    if len(arches) > 1: parser.error('camera, media and gateway architectures do not match')
    load(ROOT / 'config/system.example.json')
    dest.mkdir(parents=True)
    release = dest / 'opt/ss07/releases' / __version__
    release.mkdir(parents=True)
    for name in ('src', 'tools', 'config', 'web'):
        shutil.copytree(ROOT / name, release / name, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    (dest / 'opt/ss07/current').symlink_to('releases/' + __version__)
    if args.native_dir or args.media_dir or args.gateway_dir:
        (release / 'bin').mkdir()
    if args.native_dir:
        shutil.copy2(args.native_dir / 'ss07-camera', release / 'bin/ss07-camera')
    if args.media_dir:
        shutil.copy2(args.media_dir / 'mediamtx', release / 'bin/mediamtx')
        licenses = release / 'licenses'
        licenses.mkdir()
        shutil.copyfile(args.media_dir / 'LICENSE', licenses / 'mediamtx-MIT.txt')
        shutil.copyfile(args.media_dir / 'manifest.json', release / 'media-manifest.json')
    if args.gateway_dir:
        shutil.copy2(args.gateway_dir / 'caddy', release / 'bin/caddy')
        licenses = release / 'licenses'; licenses.mkdir(exist_ok=True)
        shutil.copyfile(args.gateway_dir / 'LICENSE', licenses / 'caddy-Apache-2.0.txt')
        shutil.copyfile(args.gateway_dir / 'manifest.json', release / 'gateway-manifest.json')
    config = dest / 'etc/ss07'
    config.mkdir(parents=True)
    shutil.copyfile(ROOT / 'config/system.example.json', config / 'system.json')
    active = dest / 'etc/ss07-active'
    active.mkdir(parents=True)
    shutil.copyfile(ROOT / 'config/system.example.json', active / 'system.json')
    units = dest / 'usr/lib/systemd/system'
    units.mkdir(parents=True)
    for source in (ROOT / 'systemd').glob('*.service'):
        shutil.copyfile(source, units / source.name)
    shutil.copyfile(ROOT / 'packaging/image/ss07.target', units / 'ss07.target')
    for kind in ('sysusers', 'tmpfiles'):
        target = dest / ('usr/lib/' + kind + '.d')
        target.mkdir(parents=True)
        shutil.copyfile(ROOT / ('systemd/ss07-' + kind + '.conf'), target / 'ss07.conf')
    write_bootstrap(dest)
    print('Staged at {}. No service enabled; no recordings directory created.'.format(dest))


if __name__ == '__main__':
    main()
