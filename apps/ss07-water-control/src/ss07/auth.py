"""Server-side bounded sessions; clients never choose roles or session identity."""
import hashlib
import hmac
import re
import secrets
import threading
import time
from collections import deque
from urllib.parse import urlsplit

from .accounts import Accounts, USERNAME, password_bytes, verify_password
from .config import revision

COOKIE = '__Host-SS07'
TOKEN = re.compile(r'[A-Za-z0-9_-]{43}')
PERMISSIONS = {'monitor': frozenset(('monitor',)),
               'reader': frozenset(('monitor', 'recordings.read')),
               'maintainer': frozenset(('monitor', 'recordings.read', 'maintain'))}


class AuthError(ValueError):
    def __init__(self, code, status=401):
        self.code, self.status = code, status
        super().__init__(code)


def public_origin(value):
    if not isinstance(value, str): raise ValueError('public origin must be a string')
    parsed = urlsplit(value)
    if (not isinstance(value, str) or len(value) > 300 or parsed.scheme != 'https'
            or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment
            or not parsed.hostname or re.fullmatch(r'[a-zA-Z0-9.-]{1,253}', parsed.hostname) is None
            or parsed.netloc.endswith(':') or parsed.port == 0):
        raise ValueError('public origin must be an HTTPS origin without a path or credentials')
    return parsed.scheme + '://' + parsed.netloc.lower()


def token_key(token):
    if not isinstance(token, str) or TOKEN.fullmatch(token) is None: raise AuthError('LOGIN_REQUIRED')
    return hashlib.sha256(token.encode()).hexdigest()


class Auth:
    IDLE_SECONDS, ABSOLUTE_SECONDS = 1800, 43200
    def __init__(self, accounts, audit, origin, clock=None, direct_access=False):
        self.direct_access = direct_access
        self.accounts = None if direct_access else Accounts(accounts)
        self.audit, self.origin, self.clock = audit, public_origin(origin), clock or time.monotonic
        self.authority = urlsplit(self.origin).netloc
        if self.accounts: self.accounts.read()  # Credential mode fails closed.
        self.lock = threading.RLock()
        self.hash_slots = threading.BoundedSemaphore(1)
        self.sessions, self.attempts, self.global_attempts = {}, {}, deque(maxlen=30)

    def _users(self):
        if self.direct_access:
            return {'local-operator': {'role': 'maintainer', 'enabled': True}}, 'direct-access-v1'
        try:
            data = self.accounts.read()
            return data['users'], revision(data)
        except (OSError, ValueError):
            with self.lock: self.sessions.clear()
            raise AuthError('AUTH_UNAVAILABLE', 503)

    def login(self, username, password):
        if self.direct_access: raise AuthError('LOGIN_DISABLED', 405)
        password_bytes(password)
        users, current = self._users()
        user = users.get(username) if isinstance(username, str) else None
        key = username if user else '_unknown'
        now = self.clock()
        with self.lock:
            for name in list(self.attempts):
                if name not in users and name != '_unknown': del self.attempts[name]
            bucket = self.attempts.setdefault(key, deque(maxlen=5))
            for values in (bucket, self.global_attempts):
                while values and now - values[0] >= 60: values.popleft()
            if len(bucket) >= 5 or len(self.global_attempts) >= 30: raise AuthError('LOGIN_RATE_LIMIT', 429)
            if not self.hash_slots.acquire(blocking=False): raise AuthError('LOGIN_BUSY', 429)
            bucket.append(now); self.global_attempts.append(now)
        try:
            valid = verify_password(password, user['password'] if user else None)
            actor = username if isinstance(username, str) and USERNAME.fullmatch(username) else 'unknown'
            if not valid or not user or not user['enabled'] or self._users()[1] != current:
                self.audit.append('LOGIN_FAILED', username=actor)
                raise AuthError('INVALID_CREDENTIALS')
            with self.lock:
                self._prune(self.clock(), current)
                if len(self.sessions) >= 32 and sum(v['username'] == username for v in self.sessions.values()) < 4:
                    raise AuthError('SESSION_LIMIT', 503)
            # Disk work never holds the session lock used by status and media.
            # The single login slot also bounds KDF/audit pressure.
            self.audit.append('LOGIN_OK', username=username, role=user['role'])
            token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            now = self.clock()
            with self.lock:
                self._prune(now, current)
                own = [(k, v) for k, v in self.sessions.items() if v['username'] == username]
                if len(own) >= 4: del self.sessions[min(own, key=lambda row: row[1]['created'])[0]]
                self.sessions[token_key(token)] = dict(username=username, role=user['role'], csrf=csrf,
                                                      created=now, used=now, revision=current)
            return token, self.session(token)
        finally: self.hash_slots.release()

    def _prune(self, now, current):
        for key, value in list(self.sessions.items()):
            if (value['revision'] != current or not 0 <= now-value['created'] < self.ABSOLUTE_SECONDS
                    or not 0 <= now-value['used'] < self.IDLE_SECONDS):
                del self.sessions[key]

    def session(self, token, permission=None, touch=True):
        key = token_key(token)
        _, current = self._users()
        now = self.clock()
        with self.lock:
            self._prune(now, current)
            if key not in self.sessions: raise AuthError('LOGIN_REQUIRED')
            session = self.sessions[key]
            if permission and permission not in PERMISSIONS[session['role']]: raise AuthError('FORBIDDEN', 403)
            if touch: session['used'] = now
            return dict(session, key=key, remaining_seconds=max(0, int(self.ABSOLUTE_SECONDS-(now-session['created']))))

    def active_key(self, key):
        _, current = self._users()
        with self.lock:
            self._prune(self.clock(), current)
            return key in self.sessions

    def active_keys(self):
        _, current = self._users()
        with self.lock:
            self._prune(self.clock(), current)
            return set(self.sessions)

    def logout(self, token):
        session = self.session(token)
        with self.lock: self.sessions.pop(session['key'], None)
        self.audit.append('LOGOUT', username=session['username'])

    @staticmethod
    def csrf(session, value):
        if not isinstance(value, str) or TOKEN.fullmatch(value) is None or not hmac.compare_digest(session['csrf'], value):
            raise AuthError('CSRF_DENIED', 403)

    def open_session(self):
        """Issue a browser-local session without requiring account credentials."""
        if not self.direct_access: raise AuthError('LOGIN_REQUIRED')
        _, current = self._users()
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self.lock:
            now = self.clock()
            self._prune(now, current)
            if len(self.sessions) >= 32: raise AuthError('SESSION_LIMIT', 503)
            self.audit.append('DIRECT_ACCESS', username='local-operator', role='maintainer')
            self.sessions[token_key(token)] = dict(username='local-operator', role='maintainer', csrf=csrf,
                                                  created=now, used=now, revision=current)
        return token, self.session(token)

    def public(self, session):
        return dict(authenticated=True, authentication_required=not self.direct_access, username=session['username'],
                    role=session['role'], permissions=sorted(PERMISSIONS[session['role']]),
                    csrf_token=session['csrf'], expires_in_seconds=session['remaining_seconds'])
