"""Bounded local persistence. Changes remain pending until services load them."""
import fcntl
import json
import os
import stat
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from .config import ConfigError, MAX_CONFIG_BYTES, load, revision, validate


class RevisionConflict(ConfigError):
    pass


def atomic_json(path, value, mode=0o600, group=None):
    path = Path(path)
    raw = (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode()
    if len(raw) > MAX_CONFIG_BYTES:
        raise ConfigError('JSON exceeds 64 KiB')
    # Caller creates/owns the destination directory; never follow a target symlink.
    if path.is_symlink():
        raise ConfigError('refusing symlink destination')
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'wb') as stream:
            if group is not None: os.fchown(stream.fileno(), -1, group)
            os.fchmod(stream.fileno(), mode)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, str(path))
        directory = os.open(str(path.parent), os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class ConfigStore:
    def __init__(self, path):
        self.path = Path(path).absolute()
        self.backup = self.path.with_name(self.path.name + '.previous')

    @contextmanager
    def locked(self):
        fd = os.open(str(self.path) + '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode): raise ConfigError('invalid configuration lock')
            deadline = time.monotonic() + 2
            while True:
                try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB); break
                except BlockingIOError:
                    if time.monotonic() >= deadline: raise ConfigError('configuration writer busy')
                    time.sleep(.01)
            yield
        finally:
            os.close(fd)

    def initialize(self, candidate):
        validate(candidate)
        with self.locked():
            if self.path.exists() or self.path.is_symlink():
                raise RevisionConflict('configuration already exists')
            atomic_json(self.path, candidate)
        return revision(candidate)

    def update(self, candidate, expected_revision):
        validate(candidate)
        with self.locked():
            if self.path.is_symlink():
                raise ConfigError('refusing symlink configuration')
            previous = load(self.path)
            if revision(previous) != expected_revision:
                raise RevisionConflict('configuration revision changed; reload before editing')
            if revision(candidate) != expected_revision:
                mode = stat.S_IMODE(self.path.stat().st_mode) & 0o666
                atomic_json(self.backup, previous, mode=mode)
                atomic_json(self.path, candidate, mode=mode)
        return revision(candidate)
