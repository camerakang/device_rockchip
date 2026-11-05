#!/usr/bin/env python3
"""Build a checked source application tarball, not a firmware image or installation."""
import argparse
import hashlib
import io
import json
import os
import platform
import subprocess
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from ss07 import __version__


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True, help='new output directory')
    args = parser.parse_args()
    if args.output.exists() or args.output.is_symlink():
        parser.error('output must not exist')
    env = dict(os.environ, PYTHONPATH=str(ROOT / 'src'), PYTHONDONTWRITEBYTECODE='1')
    subprocess.run([sys.executable, '-m', 'ss07', 'validate'], env=env, check=True)
    subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', str(ROOT / 'tests'), '-v'],
                   env=env, check=True)
    files = []
    for directory in ('src', 'config', 'tools', 'tests', 'packaging', 'docs', 'web', 'systemd'):
        files += [p for p in (ROOT / directory).rglob('*') if p.is_file()
                  and '__pycache__' not in p.parts and p.suffix != '.pyc']
    files += [ROOT / 'README.md', ROOT / 'Makefile', ROOT / 'CMakeLists.txt']
    manifest = {'artifact': 'ss07-source-app', 'version': __version__,
                'requires': {'management': 'Python >=3.8, standard library only',
                             'recording_catalog': 'Python sqlite3 with SQLite >=3.24; target observed 3.31.1',
                             'camera': 'C++17 build, GStreamer >=1.18 and pinned runtime plugins',
                             'media': 'MediaMTX v1.21.2, see packaging/dependencies.lock.json',
                             'https': 'Caddy v2.11.7; explicit TLS certificate and accounts; no default credentials'},
                'contains_native_binaries': False,
                'build_python': platform.python_version(), 'build_machine': platform.machine(),
                'hardware_validated': False,
                'files': {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                          for p in sorted(files)}}
    args.output.mkdir(parents=True, exist_ok=False)
    name = 'ss07-' + __version__
    archive = args.output / (name + '.tar.gz')
    with tarfile.open(archive, 'w:gz') as tar:
        for p in sorted(files):
            tar.add(p, arcname=name + '/' + str(p.relative_to(ROOT)), recursive=False)
        raw = (json.dumps(manifest, indent=2) + '\n').encode()
        info = tarfile.TarInfo(name + '/manifest.json')
        info.size = len(raw)
        tar.addfile(info, io.BytesIO(raw))
    (args.output / 'SHA256SUMS').write_text(hashlib.sha256(archive.read_bytes()).hexdigest() + '  ' + archive.name + '\n')
    print(archive)


if __name__ == '__main__':
    main()
