"""Local account provisioning; no default passwords or network account mutation."""
import fcntl
import hashlib
import hmac
import os
import re
import secrets
import stat
import time
from contextlib import contextmanager
from pathlib import Path

from .config import ConfigError, decode_json, revision
from .persistence import RevisionConflict, atomic_json

ROLES = ('monitor', 'reader', 'maintainer')
USERNAME = re.compile(r'[a-z][a-z0-9_-]{1,31}')
KDF = dict(algorithm='scrypt', n=32768, r=8, p=3)


def password_bytes(password):
    if (not isinstance(password, str) or not 12 <= len(password) <= 128
            or any(ord(c) < 32 or ord(c) == 127 for c in password)):
        raise ConfigError('password requires 12-128 characters without control characters')
    return password.encode('utf-8')


def derive(password, salt):
    return hashlib.scrypt(password_bytes(password), salt=salt, n=KDF['n'], r=KDF['r'],
                          p=KDF['p'], dklen=32, maxmem=67108864)


def password_record(password):
    salt = secrets.token_bytes(16)
    return dict(KDF, salt=salt.hex(), digest=derive(password, salt).hex())


def verify_password(password, record):
    # Unknown names have the same KDF cost; this dummy hash cannot grant access.
    record = record or dict(KDF, salt='00'*16, digest='00'*32)
    return hmac.compare_digest(derive(password, bytes.fromhex(record['salt'])), bytes.fromhex(record['digest']))


def validate_accounts(data):
    if (type(data) is not dict or set(data) != {'schema_version', 'users'}
            or type(data['schema_version']) is not int or data['schema_version'] != 1
            or type(data['users']) is not dict or not 1 <= len(data['users']) <= 16):
        raise ConfigError('invalid accounts schema')
    for name, user in data['users'].items():
        if (not isinstance(name, str) or USERNAME.fullmatch(name) is None or type(user) is not dict
                or set(user) != {'role', 'enabled', 'password'} or user['role'] not in ROLES
                or type(user['enabled']) is not bool):
            raise ConfigError('invalid account')
        record = user['password']
        if type(record) is not dict or set(record) != set(KDF) | {'salt', 'digest'}:
            raise ConfigError('invalid password record')
        if any(type(record[k]) is not type(v) or record[k] != v for k, v in KDF.items()):
            raise ConfigError('unsupported password parameters')
        for key, size in (('salt', 32), ('digest', 64)):
            if not isinstance(record[key], str) or re.fullmatch('[0-9a-f]{%d}' % size, record[key]) is None:
                raise ConfigError('invalid password encoding')
    if not any(u['role'] == 'maintainer' and u['enabled'] for u in data['users'].values()):
        raise ConfigError('at least one enabled maintainer is required')
    return data


class Accounts:
    def __init__(self, path):
        self.path = Path(path).absolute()

    def read(self):
        fd = os.open(str(self.path), os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o027
                    or info.st_uid not in (0, os.geteuid())):
                raise ConfigError('accounts require an owner-controlled regular file with mode 0600 or 0640')
            return validate_accounts(decode_json(stream.read(65537)))

    @contextmanager
    def locked(self):
        fd = os.open(str(self.path) + '.lock', os.O_CREAT | os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode): raise ConfigError('invalid accounts lock')
            deadline = time.monotonic() + 2
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB); break
                except BlockingIOError:
                    if time.monotonic() >= deadline: raise ConfigError('accounts busy')
                    time.sleep(.01)
            yield
        finally:
            os.close(fd)

    def initialize(self, username, password):
        data = validate_accounts(dict(schema_version=1, users={username: dict(
            role='maintainer', enabled=True, password=password_record(password))}))
        with self.locked():
            if self.path.exists() or self.path.is_symlink(): raise RevisionConflict('accounts already exist')
            atomic_json(self.path, data, mode=0o640)
        return revision(data)

    def update(self, username, role, enabled, expected_revision, password=None):
        record = password_record(password) if password is not None else None
        with self.locked():
            previous = self.read()
            if revision(previous) != expected_revision: raise RevisionConflict('accounts changed; reload before editing')
            if record is None:
                if username not in previous['users']: raise ConfigError('new account requires a password')
                record = previous['users'][username]['password']
            previous['users'][username] = dict(role=role, enabled=enabled, password=record)
            validate_accounts(previous)
            info = self.path.stat()
            atomic_json(self.path, previous, mode=stat.S_IMODE(info.st_mode), group=info.st_gid)
        return revision(previous)

    def public(self):
        data = self.read()
        return dict(revision=revision(data), users={k: {field: u[field] for field in ('role', 'enabled')}
                                                  for k, u in data['users'].items()})
