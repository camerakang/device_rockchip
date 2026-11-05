#!/usr/bin/env python3
"""Fetch exactly one pinned release into a NEW directory; no installation."""
import argparse
import hashlib
import io
import json
import tarfile
from pathlib import Path
from urllib.request import urlopen


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--arch', choices=('amd64', 'arm64'), required=True)
    parser.add_argument('--component', choices=('mediamtx', 'caddy'), default='mediamtx')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--archive', type=Path, help='verify an already downloaded archive offline')
    args = parser.parse_args()
    if args.output.exists() or args.output.is_symlink():
        parser.error('output must not exist')
    lock = json.loads(Path(__file__).with_name('dependencies.lock.json').read_text())[args.component]
    asset = lock['assets']['linux_' + args.arch]
    if args.archive:
        with args.archive.open('rb') as stream:
            raw = stream.read(64 * 1024 * 1024 + 1)
    else:
        with urlopen(asset['url'], timeout=45) as response:
            raw = response.read(64 * 1024 * 1024 + 1)
    if len(raw) > 64 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != asset['sha256']:
        parser.error('archive checksum/size mismatch')
    with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
        entries = tar.getmembers()
        expected_entries = {'mediamtx', 'mediamtx.yml', 'LICENSE'} if args.component == 'mediamtx' else {'caddy', 'README.md', 'LICENSE'}
        if {entry.name for entry in entries} != expected_entries or len(entries) != 3:
            parser.error('unexpected archive entries')
        if any(not e.isfile() or not 0 <= e.size <= 128 * 1024 * 1024 for e in entries):
            parser.error('invalid archive member')
        args.output.mkdir(parents=True, exist_ok=False)
        for entry in entries:
            target = args.output / entry.name
            with tar.extractfile(entry) as stream:
                target.write_bytes(stream.read())
            target.chmod(0o755 if entry.name == args.component else 0o644)
    binary_digest = hashlib.sha256((args.output / args.component).read_bytes()).hexdigest()
    if binary_digest != asset['binary_sha256']:
        parser.error('binary checksum mismatch')
    manifest = dict(version=lock['version'], platform='linux_' + args.arch,
                    archive_sha256=asset['sha256'],
                    binary_sha256=binary_digest)
    (args.output / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(args.output)


if __name__ == '__main__':
    main()
