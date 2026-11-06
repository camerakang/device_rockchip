"""Optional key-less LAN monitor access (lan-monitor).

Sources inside allow_cidrs may read status and pull video without a key. The
allowed routes are fixed here on the server; configuration cannot widen them.
An unreadable, invalid or over-broad file disables the mode instead of failing.
"""
import ipaddress
import os
import re
import stat
import threading
import time

from .config import decode_json

PRINCIPAL = 'lan-monitor'
PEER_HEADER = 'X-SS07-Peer'  # Set by the HTTPS gateway from the TCP peer, replacing any client value.
GET_ROUTES = frozenset(('/api/v1/status', '/api/v1/streams', '/api/v1/sonars', '/api/v1/timecode', '/api/v1/events'))
CREATE = re.compile(r'/media/(front|back|left|right|down|sdi)/whep')
SESSION = re.compile(r'/media/(front|back|left|right|down|sdi)/whep/[a-fA-F0-9-]{36}')
MAX_CIDRS = 16


def log(message):
    print('lan-monitor: ' + message, flush=True)


def load(path):
    """Return (enabled, reason, networks); never raises for a bad file."""
    if path is None: return False, 'not_configured', ()
    try: fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError: return False, 'file_missing', ()
    except OSError: return False, 'file_unreadable', ()
    try:
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o022
                    or info.st_uid not in (0, os.geteuid())):
                return False, 'file_not_owner_controlled', ()
            value = decode_json(stream.read(4097), max_bytes=4096)
    except (OSError, ValueError): return False, 'file_invalid', ()
    if (type(value) is not dict or set(value) != {'schema_version', 'enabled', 'allow_cidrs'}
            or type(value['schema_version']) is not int or value['schema_version'] != 1
            or type(value['enabled']) is not bool or type(value['allow_cidrs']) is not list):
        return False, 'schema_invalid', ()
    if not value['enabled']: return False, 'disabled', ()
    if not value['allow_cidrs']: return False, 'allow_cidrs_empty', ()
    if len(value['allow_cidrs']) > MAX_CIDRS: return False, 'too_many_cidrs', ()
    networks = []
    for item in value['allow_cidrs']:
        try:
            if not isinstance(item, str): raise ValueError('cidr')
            network = ipaddress.IPv4Network(item, strict=True)
        except ValueError: return False, 'cidr_invalid', ()
        if network.prefixlen == 0: return False, 'cidr_any_refused', ()
        networks.append(network)
    return True, None, tuple(networks)


class LanMonitor:
    """Re-reads the file at most once per second, so removing or disabling it takes effect without restart."""

    def __init__(self, path=None, interval=1.0):
        self.path, self.interval = path, interval
        self.lock = threading.Lock()
        self.denied_at, self.denied_count = -1e9, 0
        self.checked_at, self.signature = time.monotonic(), self._signature()
        self.enabled, self.reason, self.networks = load(path)

    def _signature(self):
        if self.path is None: return None
        try: info = os.stat(str(self.path), follow_symlinks=False)
        except OSError: return 'missing'
        return (info.st_ino, info.st_size, info.st_mtime_ns, info.st_mode, info.st_uid)

    def refresh(self):
        if self.path is None: return
        now = time.monotonic()
        with self.lock:
            if now - self.checked_at < self.interval: return
            self.checked_at = now
            signature = self._signature()
            if signature == self.signature: return
            self.signature = signature
            before = self.describe()
            self.enabled, self.reason, self.networks = load(self.path)
        if self.describe() != before: log(self.describe())

    def state(self):
        """Hashable view used to restart the RTSP relay when the policy changes."""
        self.refresh()
        return dict(enabled=self.enabled, allow_cidrs=[str(n) for n in self.networks])

    def describe(self):
        if self.enabled: return 'enabled for ' + ','.join(str(n) for n in self.networks)
        return 'disabled (' + self.reason + ')'

    def allows(self, ip):
        self.refresh()
        if not self.enabled or not isinstance(ip, str) or len(ip) > 64: return False
        try: address = ipaddress.IPv4Address(ip)
        except ValueError: return False
        return any(address in network for network in self.networks)

    def denied(self, ip, what):
        """At most one log line per 10 s; the count covers the suppressed ones."""
        now = time.monotonic()
        with self.lock:
            self.denied_count += 1
            if now - self.denied_at < 10: return
            count, self.denied_count, self.denied_at = self.denied_count, 0, now
        shown = ip if isinstance(ip, str) and re.fullmatch(r'[0-9.]{7,15}', ip) else '?'
        log('refused %s from %s (%d refusals in window)' % (what, shown, count))

    @staticmethod
    def route_allowed(method, path):
        if method in ('GET', 'HEAD'): return path in GET_ROUTES
        if method == 'POST': return CREATE.fullmatch(path) is not None
        if method == 'DELETE': return SESSION.fullmatch(path) is not None
        return False

    @staticmethod
    def owner(ip):
        return PRINCIPAL + ':' + ip
