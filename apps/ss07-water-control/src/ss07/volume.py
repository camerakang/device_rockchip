"""Pinned directory descriptors: recording never falls back to a replacement path."""
import json
import os
import re
import stat
import uuid
from contextlib import contextmanager
from pathlib import Path

from .config import ConfigError, decode_json
from .storage import _unescape

DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


class VolumeError(OSError):
    pass


def parts(relative):
    if (not isinstance(relative, str) or len(relative) > 512 or relative.startswith('/')
            or any(ord(c) < 32 for c in relative)):
        raise VolumeError('invalid_relative_path')
    result = relative.split('/')
    if not 1 <= len(result) <= 6 or any(p in ('', '.', '..') for p in result):
        raise VolumeError('invalid_relative_path')
    return result


def mount_entries(raw=None):
    raw = Path('/proc/self/mountinfo').read_text() if raw is None else raw
    entries = []
    for line in raw.splitlines():
        before, after = line.split(' - ', 1)
        fields, fs = before.split(), after.split()
        entries.append(dict(id=int(fields[0]), device=fields[2], root=_unescape(fields[3]),
                            target=_unescape(fields[4]), options=fields[5].split(',') + fs[2].split(','),
                            filesystem=fs[0], source=_unescape(fs[1])))
    return entries


def descriptor_mount_id(fd):
    for line in Path('/proc/self/fdinfo/' + str(fd)).read_text().splitlines():
        if line.startswith('mnt_id:'):
            return int(line.split()[1])
    raise VolumeError('mount_identity_unavailable')


def open_directory(path):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise VolumeError('absolute_directory_required')
    fd = os.open('/', DIRECTORY_FLAGS)
    try:
        for name in path.parts[1:]:
            child = os.open(name, DIRECTORY_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except Exception:
        os.close(fd)
        raise


class DirectoryVolume:
    """FD-based file operations. Production acquisition is ONLY through MountLease.

    Tests explicitly construct this class with synthetic_test evidence; production
    CLI/config has no test directory or system-filesystem override.
    """
    def __init__(self, fd, volume_uuid, evidence_kind):
        if evidence_kind not in ('device_observation', 'synthetic_test'):
            raise ValueError('invalid volume evidence')
        self.fd, self.volume_uuid, self.evidence_kind = fd, volume_uuid, evidence_kind
        self.device = os.fstat(fd).st_dev
        self.inode = os.fstat(fd).st_ino
        self.mount_id = descriptor_mount_id(fd)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def valid(self):
        return self.fd is not None

    def require_valid(self):
        if not self.valid():
            raise VolumeError('volume_changed_or_unmounted')

    def _check_fd(self, fd):
        if os.fstat(fd).st_dev != self.device or descriptor_mount_id(fd) != self.mount_id:
            raise VolumeError('nested_mount_or_device_change')

    @contextmanager
    def directory(self, relative, create=False):
        self.require_valid()
        fd = os.dup(self.fd)
        try:
            for name in parts(relative):
                if create:
                    try:
                        os.mkdir(name, mode=0o750, dir_fd=fd)
                        os.fsync(fd)
                    except FileExistsError:
                        pass
                child = os.open(name, DIRECTORY_FLAGS, dir_fd=fd)
                try:
                    self._check_fd(child)
                except Exception:
                    os.close(child)
                    raise
                os.close(fd)
                fd = child
            yield fd
        finally:
            os.close(fd)

    @contextmanager
    def parent(self, relative):
        names = parts(relative)
        if len(names) == 1:
            self.require_valid()
            fd = os.dup(self.fd)
            try:
                yield fd, names[0]
            finally:
                os.close(fd)
        else:
            with self.directory('/'.join(names[:-1])) as fd:
                yield fd, names[-1]

    def open(self, relative):
        with self.parent(relative) as (parent, name):
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise VolumeError('recording_is_not_a_single_regular_file')
            self._check_fd(fd)
            return fd
        except Exception:
            os.close(fd)
            raise

    def read_json(self, relative):
        with os.fdopen(self.open(relative), 'rb') as stream:
            return decode_json(stream.read(65537))

    def write_json(self, relative, data):
        raw = (json.dumps(data, allow_nan=False, separators=(',', ':')) + '\n').encode()
        if len(raw) > 65536:
            raise ConfigError('metadata exceeds 64 KiB')
        with self.parent(relative) as (parent, name):
            temporary = '.' + uuid.uuid4().hex + '.tmp'
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640, dir_fd=parent)
            try:
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
                os.fsync(parent)
            finally:
                try:
                    os.unlink(temporary, dir_fd=parent)
                except FileNotFoundError:
                    pass

    def unlink(self, relative):
        with self.parent(relative) as (parent, name):
            os.unlink(name, dir_fd=parent)
            os.fsync(parent)

    def space(self):
        self.require_valid()
        usage = os.fstatvfs(self.fd)
        return usage.f_bavail * usage.f_frsize, usage.f_blocks * usage.f_frsize


class MountLease(DirectoryVolume):
    @classmethod
    def acquire(cls, config, writable=False):
        identity = config.get('filesystem_uuid')
        if not isinstance(identity, str) or not re.fullmatch(r'[A-Za-z0-9-]{1,64}', identity):
            raise VolumeError('filesystem_uuid_unconfigured')
        target = config['mountpoint']
        entries = mount_entries()
        mount = next((e for e in reversed(entries) if e['target'] == target), None)
        root = next((e for e in reversed(entries) if e['target'] == '/'), None)
        if mount is None:
            raise VolumeError('mount_missing')
        if root is None or mount['device'] == root['device'] or mount['root'] != '/':
            raise VolumeError('system_or_bind_mount_refused')
        block = Path('/dev/disk/by-uuid') / identity
        info = block.stat()
        if not stat.S_ISBLK(info.st_mode) or '%d:%d' % (os.major(info.st_rdev), os.minor(info.st_rdev)) != mount['device']:
            raise VolumeError('filesystem_uuid_mismatch')
        if writable and 'ro' in mount['options']:
            raise VolumeError('read_only_volume')
        fd = open_directory(target)
        try:
            if descriptor_mount_id(fd) != mount['id']:
                raise VolumeError('mount_changed_during_open')
            if os.fstat(fd).st_dev != info.st_rdev:
                raise VolumeError('volume_device_mismatch')
            # Do not create the recordings directory until the real partition is pinned.
            if writable:
                try:
                    os.mkdir('recordings', mode=0o750, dir_fd=fd)
                    os.fsync(fd)
                except FileExistsError:
                    pass
            recfd = os.open('recordings', DIRECTORY_FLAGS, dir_fd=fd)
            try:
                if descriptor_mount_id(recfd) != mount['id'] or os.fstat(recfd).st_dev != info.st_rdev:
                    raise VolumeError('recordings_is_another_mount')
                lease = cls(recfd, identity, 'device_observation')
                lease.target, lease.expected_mount, lease.writable = target, mount, writable
                lease.require_valid()
                return lease
            except Exception:
                os.close(recfd)
                raise
        finally:
            os.close(fd)

    def valid(self):
        if self.fd is None:
            return False
        try:
            entries = mount_entries()
            mount = next((e for e in reversed(entries) if e['target'] == self.target), None)
            current = os.stat(self.target + '/recordings', follow_symlinks=False)
            return (mount is not None and mount['id'] == self.mount_id
                    and mount['device'] == self.expected_mount['device']
                    and (not self.writable or 'ro' not in mount['options'])
                    and os.stat(self.target).st_dev == self.device
                    and stat.S_ISDIR(current.st_mode) and current.st_dev == self.device and current.st_ino == self.inode
                    and not any(e['target'] == self.target + '/recordings' and e['id'] != self.mount_id for e in entries))
        except (OSError, ValueError):
            return False
