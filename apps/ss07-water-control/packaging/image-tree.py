#!/usr/bin/env python3
"""Snapshot a rootfs tree under fakeroot; do not follow symlinks or read host paths."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import stat


def snapshot(root):
    result = {}
    paths = [root]
    for directory, dirs, files in os.walk(str(root), followlinks=False):
        paths.extend(Path(directory) / name for name in dirs + files)
    for path in sorted(paths):
        value = path.lstat()
        name = '/' + str(path.relative_to(root)) if path != root else '/'
        kind = ('symlink' if stat.S_ISLNK(value.st_mode) else 'directory' if stat.S_ISDIR(value.st_mode)
                else 'file' if stat.S_ISREG(value.st_mode) else 'character' if stat.S_ISCHR(value.st_mode)
                else 'block' if stat.S_ISBLK(value.st_mode) else 'fifo' if stat.S_ISFIFO(value.st_mode) else 'other')
        entry = {'kind': kind, 'mode': stat.S_IMODE(value.st_mode), 'uid': value.st_uid, 'gid': value.st_gid}
        if kind == 'file':
            hashed = hashlib.sha256()
            with path.open('rb') as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                    hashed.update(chunk)
            entry.update(size=value.st_size, sha256=hashed.hexdigest())
        elif kind == 'symlink':
            entry['target'] = os.readlink(str(path))
        elif kind in ('character', 'block'):
            entry['device'] = [os.major(value.st_rdev), os.minor(value.st_rdev)]
        result[name] = entry
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('output must not exist')
    args.output.write_text(json.dumps(snapshot(args.root.resolve()), indent=2, sort_keys=True) + '\n')


if __name__ == '__main__':
    main()
