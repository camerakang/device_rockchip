"""Machine API clients such as mobocon: one random key per client, stored only as SHA-256.

A key authenticates HTTPS API requests (``Authorization: Bearer``) and native
RTSP reads (username = client name, password = key). Keys carry 256 bits of
entropy, so a fast digest is sufficient and no per-request KDF is needed.
"""
import hashlib
import hmac
import os
import re
import secrets
import stat

from .accounts import Accounts, ROLES, USERNAME
from .config import ConfigError, decode_json, revision
from .persistence import RevisionConflict, atomic_json

KEY = re.compile(r'[A-Za-z0-9_-]{43}')
EMPTY = dict(schema_version=1, clients={})


def digest(key):
    return hashlib.sha256(key.encode()).hexdigest()


def validate_clients(data):
    if (type(data) is not dict or set(data) != {'schema_version', 'clients'}
            or type(data['schema_version']) is not int or data['schema_version'] != 1
            or type(data['clients']) is not dict or len(data['clients']) > 16):
        raise ConfigError('invalid API client schema')
    for name, client in data['clients'].items():
        if (not isinstance(name, str) or USERNAME.fullmatch(name) is None or type(client) is not dict
                or set(client) != {'role', 'enabled', 'sha256'} or client['role'] not in ROLES
                or type(client['enabled']) is not bool or not isinstance(client['sha256'], str)
                or re.fullmatch('[0-9a-f]{64}', client['sha256']) is None):
            raise ConfigError('invalid API client')
    return data


class ApiClients(Accounts):
    """Same file ownership rules and lock as accounts; a missing file means no clients."""

    def read(self):
        try: fd = os.open(str(self.path), os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        except FileNotFoundError: return dict(EMPTY, clients={})
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o027
                    or info.st_uid not in (0, os.geteuid())):
                raise ConfigError('API clients require an owner-controlled regular file with mode 0600 or 0640')
            return validate_clients(decode_json(stream.read(65537)))

    def bearer(self, key):
        """Return (name, client) for an enabled client holding this key, else None."""
        if not isinstance(key, str) or KEY.fullmatch(key) is None: return None
        value, found = digest(key), None
        for name, client in self.read()['clients'].items():
            # Compare every entry so timing does not reveal the matching position.
            if hmac.compare_digest(client['sha256'], value) and client['enabled']: found = (name, client)
        return found

    def verify(self, name, key):
        if not isinstance(key, str) or KEY.fullmatch(key) is None: return None
        client = self.read()['clients'].get(name)
        if client and client['enabled'] and hmac.compare_digest(client['sha256'], digest(key)): return client
        return None

    def add(self, name, role, group=None):
        if not isinstance(name, str) or USERNAME.fullmatch(name) is None or role not in ROLES:
            raise ConfigError('client name must match [a-z][a-z0-9_-]{1,31}; role must be monitor/reader/maintainer')
        key = secrets.token_urlsafe(32)
        with self.locked():
            data = self.read()
            if name in data['clients']: raise RevisionConflict('client already exists; rotate or remove it first')
            data['clients'][name] = dict(role=role, enabled=True, sha256=digest(key))
            self._write(validate_clients(data), group)
        return key

    def rotate(self, name):
        key = secrets.token_urlsafe(32)
        with self.locked():
            data = self.read()
            if name not in data['clients']: raise ConfigError('unknown API client')
            data['clients'][name]['sha256'] = digest(key)
            self._write(data)
        return key

    def set(self, name, enabled=None, role=None):
        with self.locked():
            data = self.read()
            if name not in data['clients']: raise ConfigError('unknown API client')
            if enabled is not None: data['clients'][name]['enabled'] = enabled
            if role is not None: data['clients'][name]['role'] = role
            self._write(validate_clients(data))

    def remove(self, name):
        with self.locked():
            data = self.read()
            if data['clients'].pop(name, None) is None: raise ConfigError('unknown API client')
            self._write(data)

    def _write(self, data, group=None):
        try:
            info = self.path.stat()
            mode, group = stat.S_IMODE(info.st_mode), info.st_gid
        except FileNotFoundError: mode = 0o640
        atomic_json(self.path, data, mode=mode, group=group)

    def public(self):
        data = self.read()
        return dict(revision=revision(data), clients={k: {f: c[f] for f in ('role', 'enabled')}
                                                      for k, c in data['clients'].items()})
