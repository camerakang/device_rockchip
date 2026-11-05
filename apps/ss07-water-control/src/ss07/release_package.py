"""Bounded package import into a new private directory; never extract onto the live root."""
import ctypes
import gzip
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tarfile
import tempfile

from .config import decode_json
from .release_contract import DEFAULT_FILES, bootstrap_files, system_files, contract

MAX_ARCHIVE = 128 * 1024 * 1024
MAX_CONTENT = 256 * 1024 * 1024
MAX_FILES = 1024
VERSION = re.compile(r'(?:0|[1-9][0-9]{0,3})\.(?:0|[1-9][0-9]{0,3})\.(?:0|[1-9][0-9]{0,3})')
# The last delivered package predates the explicit deployment contract. Its
# exact bytes, rather than arbitrary packages claiming version 0.7.0, are known.
LEGACY_070 = 'b3a2c0635fbc86f8457994dacb3dd2026eeb710996a0e6efeea783f591579814'


class PackageError(ValueError):
    pass


def digest(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''): result.update(block)
    return result.hexdigest()


def rename_new(source, destination):
    # Linux renameat2(RENAME_NOREPLACE) publishes a directory atomically without
    # replacing a destination created concurrently. No unsafe rename fallback.
    libc = ctypes.CDLL(None, use_errno=True)
    operation = libc.renameat2
    operation.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    operation.restype = ctypes.c_int
    if operation(-100, os.fsencode(source), -100, os.fsencode(destination), 1):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(destination))


def canonical(name):
    return (isinstance(name, str) and 0 < len(name) <= 512 and not name.startswith('/')
            and '\\' not in name and not any(ord(c) < 32 or ord(c) == 127 for c in name)
            and all(p not in ('', '.', '..') for p in name.split('/')) and str(PurePosixPath(name)) == name)


class LimitedReader:
    def __init__(self, stream): self.stream, self.total = stream, 0

    def read(self, size=-1):
        if size < 0: size = MAX_CONTENT + 1
        size = min(size, MAX_CONTENT + 1 - self.total)
        value = self.stream.read(size); self.total += len(value)
        if self.total > MAX_CONTENT: raise PackageError('package_expansion_limit')
        return value


def validate_manifest(value, archive_sha):
    if (type(value) is not dict or value.get('artifact') != 'ss07-arm64-application'
            or not isinstance(value.get('version'), str) or VERSION.fullmatch(value['version']) is None
            or value.get('architecture') != 'arm64' or value.get('firmware_image') is not False
            or value.get('services_enabled') is not False or type(value.get('files')) is not dict
            or type(value.get('symlinks')) is not dict or not 1 <= len(value['files']) <= MAX_FILES):
        raise PackageError('invalid_application_manifest')
    data = value.get('deployment')
    if data is None and value['version'] == '0.7.0' and archive_sha == LEGACY_070: data = contract(legacy=True)
    if (type(data) is not dict or type(data.get('schema_version')) is not int
            or type(data.get('data')) is not dict or any(type(v) is not int for v in data['data'].values())
            or data not in (contract(), contract(legacy=True))):
        raise PackageError('unsupported_deployment_contract')
    system, bootstrap = system_files(data['data']), bootstrap_files(data['data'])
    release = 'opt/ss07/releases/' + value['version'] + '/'
    for name, checksum in value['files'].items():
        if (not canonical(name) or not (name.startswith(release) or name in system + DEFAULT_FILES + bootstrap)
                or not isinstance(checksum, str) or re.fullmatch('[0-9a-f]{64}', checksum) is None):
            raise PackageError('package_path_or_digest_not_allowed')
    required = set(system + DEFAULT_FILES) | {release + n for n in (
        'tools/ss07', 'src/ss07/__init__.py', 'config/system.schema.json', 'bin/ss07-camera', 'bin/mediamtx', 'bin/caddy')}
    if tuple(map(int, value['version'].split('.'))) >= (0, 8, 0): required.update(bootstrap)
    if not required <= set(value['files']): raise PackageError('package_required_file_missing')
    if value['symlinks'] != {'opt/ss07/current': 'releases/' + value['version']}:
        raise PackageError('package_symlink_not_allowed')
    return data


def unpack(archive, expected_sha256, output):
    """The caller trusts the expected digest from a separately verified release record.

    Copy/hash the same private bytes subsequently parsed, reject extra paths and
    links, normalize owner-write/executable modes, then publish a complete tree.
    No package executable or maintainer script runs during import.
    """
    if not isinstance(expected_sha256, str) or re.fullmatch('[0-9a-f]{64}', expected_sha256) is None:
        raise PackageError('invalid_package_checksum')
    output = Path(output).absolute()
    if output.exists() or output.is_symlink(): raise PackageError('package_destination_exists')
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.ss07-import-', dir=str(output.parent)) as temporary:
        temp = Path(temporary); packed = temp / 'archive.tar.gz'
        fd = os.open(str(archive), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as source, packed.open('xb') as target:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_ARCHIVE:
                raise PackageError('invalid_package_file')
            size, actual = 0, hashlib.sha256()
            while True:
                block = source.read(1024 * 1024)
                if not block: break
                size += len(block)
                if size > MAX_ARCHIVE: raise PackageError('package_archive_limit')
                actual.update(block); target.write(block)
            target.flush(); os.fsync(target.fileno())
        if actual.hexdigest() != expected_sha256: raise PackageError('package_checksum_mismatch')
        tree = temp / 'tree'; tree.mkdir(mode=0o700)
        files, links, directories, seen = {}, {}, set(), set()
        prefix, members = None, 0
        try:
            with gzip.open(packed, 'rb') as compressed, tarfile.open(fileobj=LimitedReader(compressed), mode='r|') as tar:
                for item in tar:
                    members += 1
                    if members > MAX_FILES * 2: raise PackageError('package_member_limit')
                    name = item.name.rstrip('/') if item.isdir() else item.name
                    if not canonical(name) or name in seen: raise PackageError('duplicate_or_invalid_archive_path')
                    seen.add(name)
                    top, _, relative = name.partition('/')
                    if prefix is None:
                        match = re.fullmatch(r'ss07-([0-9]+\.[0-9]+\.[0-9]+)-linux-arm64', top)
                        if not match or VERSION.fullmatch(match[1]) is None: raise PackageError('invalid_archive_root')
                        prefix = top
                    if top != prefix or (not relative and not item.isdir()): raise PackageError('invalid_archive_root')
                    if item.uid != 0 or item.gid != 0 or item.mode & 0o7000:
                        raise PackageError('unsafe_package_owner_or_mode')
                    if not relative: continue
                    if item.isdir(): directories.add(relative); continue
                    if not canonical(relative): raise PackageError('invalid_package_path')
                    if item.issym():
                        if relative != 'opt/ss07/current' or item.linkname != 'releases/' + match_version(prefix):
                            raise PackageError('package_symlink_not_allowed')
                        links[relative] = item.linkname; continue
                    if not item.isfile() or item.issparse() or not 0 <= item.size <= MAX_ARCHIVE:
                        raise PackageError('package_member_type_or_size')
                    if len(files) >= MAX_FILES: raise PackageError('package_file_limit')
                    path = tree / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    total, checksum = 0, hashlib.sha256()
                    with tar.extractfile(item) as source, path.open('xb') as target:
                        while True:
                            block = source.read(1024 * 1024)
                            if not block: break
                            total += len(block); target.write(block); checksum.update(block)
                        if total != item.size: raise PackageError('truncated_package_member')
                        os.fchmod(target.fileno(), 0o755 if item.mode & 0o111 else 0o644)
                        target.flush(); os.fsync(target.fileno())
                    files[relative] = checksum.hexdigest()
        except (tarfile.TarError, EOFError) as exc: raise PackageError('invalid_archive') from exc
        manifest_path = tree / 'SS07-MANIFEST.json'
        with manifest_path.open('rb') as source: value = decode_json(source.read(1048577), max_bytes=1048576)
        deployment = validate_manifest(value, expected_sha256)
        if value['version'] != match_version(prefix): raise PackageError('package_version_mismatch')
        manifest_digest = files.pop('SS07-MANIFEST.json', None)
        if files != value['files'] or links != value['symlinks']:
            raise PackageError('package_manifest_content_mismatch')
        allowed_dirs = {str(p) for name in list(files) + list(links) for p in PurePosixPath(name).parents if str(p) != '.'}
        if not directories <= allowed_dirs or any(name in allowed_dirs for name in list(files) + list(links)):
            raise PackageError('package_path_type_conflict')
        for name, target in links.items():
            path = tree / name; path.parent.mkdir(parents=True, exist_ok=True); path.symlink_to(target)
        receipt = dict(schema_version=1, archive_sha256=expected_sha256, manifest_sha256=manifest_digest,
                       version=value['version'], deployment=deployment)
        (tree / 'SS07-IMPORT.json').write_text(json.dumps(receipt, indent=2) + '\n')
        with (tree / 'SS07-IMPORT.json').open('rb') as stream: os.fsync(stream.fileno())
        # Every directory entry must be durable before publishing the root name.
        for path in [p for p in tree.rglob('*') if p.is_dir() and not p.is_symlink()] + [tree]:
            fd = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY)
            try: os.fsync(fd)
            finally: os.close(fd)
        rename_new(tree, output)
        fd = os.open(str(output.parent), os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(fd)
        finally: os.close(fd)
        return receipt


def match_version(prefix):
    if not isinstance(prefix, str): raise PackageError('empty_package')
    return prefix[len('ss07-'):-len('-linux-arm64')]
