"""Optional RTSPS (and opt-in LAN plain RTSP) relay. Python authorizes connections; native code moves media.

Credentials are either a local account (scrypt) or a machine API client key
(``api_clients``: username = client name, password = key).

The relay is a separate native process. Revocation closes that whole process,
including in-flight SETUPs, without touching the internal recorder or WebRTC.
The callback contract is pinned to MediaMTX v1.21.2.
"""
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import stat
import subprocess
import threading
import time
import uuid
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .accounts import Accounts, USERNAME, password_bytes, verify_password
from .api_clients import ApiClients
from .audit import Audit
from .config import ConfigError, decode_json, revision
from .gateway import owns_listener, validate as validate_gateway
from .lan_monitor import LanMonitor
from .media import CHANNELS, settings
from .worker import terminate

DEFAULT = dict(schema_version=1, enabled=False, port=8322)
CALLBACK_FIELDS = {'ip', 'user', 'password', 'token', 'action', 'path', 'protocol', 'id', 'query', 'userAgent'}


def validate(value):
    # plain_port is an optional LAN-only plaintext RTSP listener for clients
    # such as mobocon/ffmpeg that cannot use RTSPS; it shares the same callback.
    if (type(value) is not dict or set(value) not in (set(DEFAULT), set(DEFAULT) | {'plain_port'})
            or type(value['schema_version']) is not int or value['schema_version'] != 1
            or type(value['enabled']) is not bool or type(value['port']) is not int
            or not 1024 <= value['port'] <= 65535):
        raise ConfigError('invalid native RTSP configuration')
    plain = value.get('plain_port')
    if plain is not None and (type(plain) is not int or not 1024 <= plain <= 65535 or plain == value['port']):
        raise ConfigError('invalid native RTSP configuration')
    return value


def listen_ports(profile):
    return [profile['port']] + ([profile['plain_port']] if profile.get('plain_port') is not None else [])


def load(path=None):
    if path is None: return dict(DEFAULT)
    try: fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError: return dict(DEFAULT)  # Older installs have no optional profile.
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o022
                or info.st_uid not in (0, os.geteuid())):
            raise ConfigError('native RTSP profile must be an owner-controlled regular file')
        return validate(decode_json(stream.read(4097), max_bytes=4096))


def check_ports(config, gateway, profile):
    validate(profile)
    if not profile['enabled']: return
    if gateway is None: raise ConfigError('native RTSP requires provisioned HTTPS/TLS settings')
    validate_gateway(gateway)
    ports = settings(config)
    occupied = [config['api']['port'], urlsplit(gateway['public_origin']).port]
    occupied += [v for k, v in ports.items() if k != 'rtp_base_port']
    occupied += list(range(ports['rtp_base_port'], ports['rtp_base_port'] + 10))
    if any(port in occupied for port in listen_ports(profile)):
        raise ConfigError('native RTSP and application ports overlap')


def render(config, gateway, profile, callback):
    check_ports(config, gateway, profile)
    if not profile['enabled']: raise ConfigError('native RTSP disabled')
    plain = profile.get('plain_port')
    extra = dict(rtspAddress=gateway['listen_ip'] + ':' + str(plain)) if plain is not None else {}
    return dict(extra, logLevel='warn', logDestinations=['stdout'], readTimeout='3s', writeTimeout='3s',
        writeQueueSize=256, authMethod='http', authHTTPAddress=callback, authHTTPExclude=[],
        api=False, metrics=False, pprof=False, playback=False, rtmp=False, hls=False, srt=False,
        webrtc=False, moq=False, rtsp=True, rtspTransports=['tcp'], rtspEncryption='optional' if plain is not None else 'strict',
        rtspAuthMethods=['basic'], rtspsAddress=gateway['listen_ip'] + ':' + str(profile['port']),
        rtspServerCert=gateway['certificate'], rtspServerKey=gateway['private_key'],
        pathDefaults=dict(record=False, maxReaders=4, sourceOnDemand=True,
                          sourceOnDemandStartTimeout='5s', sourceOnDemandCloseAfter='1s'),
        paths={channel: dict(source='rtsp://127.0.0.1:%d/%s' % (settings(config)['rtsp_port'], channel),
                             rtspTransport='tcp') for channel in CHANNELS})


class Authorization:
    def __init__(self, accounts, audit, api_clients=None, lan_monitor=None):
        self.accounts, self.audit = Accounts(accounts), Audit(audit)
        self.lan = LanMonitor(lan_monitor)
        self.clients = ApiClients(api_clients) if api_clients else None
        self.lock = threading.RLock()
        self.kdf = threading.BoundedSemaphore(1)
        self.secret = secrets.token_bytes(32)
        self.cache, self.attempts = {}, {}
        self.pending = {}
        self.global_attempts = deque()
        self.active_revision = None
        self.generation = 0
        self.ready = False
        self.reason = 'accounts_unavailable'

    def disarm(self, reason):
        with self.lock:
            self.ready, self.reason = False, reason
            self.cache.clear()
            self.generation += 1

    def credentials(self):
        data = self.accounts.read()
        clients = self.clients.read() if self.clients else None
        current = revision(data) if clients is None else revision(dict(accounts=revision(data), clients=revision(clients)))
        lan = self.lan.state()
        if lan['enabled']:  # Any lan-monitor change also disarms and restarts the relay.
            current = revision(dict(credentials=current, lan_monitor=lan))
        return data, clients, current

    def arm(self):
        """Only call after the preceding native relay has been confirmed dead."""
        with self.lock:
            try:
                data, _, current = self.credentials()
                self.audit.initialize()
                self.audit.append('RTSP_ACCESS_START', revision=current)
            except (OSError, ValueError, sqlite3.Error):
                self.disarm('accounts_or_audit_unavailable')
                return False
            self.active_revision = current
            allowed = set(data['users']) | {'unknown'}
            self.attempts = {k: v for k, v in self.attempts.items() if k in allowed}
            self.generation += 1
            self.cache.clear()
            self.ready, self.reason = True, None
            return True

    def refresh(self):
        with self.lock:
            try: data, clients, current = self.credentials()
            except (OSError, ValueError):
                self.disarm('accounts_unavailable')
                return None
            if current != self.active_revision:
                self.disarm('accounts_changed')
                return None
            if not self.ready: return None
            return dict(data, clients=(clients or {}).get('clients', {}))

    def record_revocation(self):
        try: self.audit.append('RTSP_REVOKE', outcome='relay_closed')
        except (OSError, ValueError, sqlite3.Error): pass

    def authorize(self, value):
        # No localhost bypass on this external relay, including local clients.
        if type(value) is not dict or set(value) != CALLBACK_FIELDS: return False
        if any(not isinstance(value[k], str) for k in CALLBACK_FIELDS): return False
        if (value['protocol'] != 'rtsp' or value['action'] != 'read' or value['path'] not in CHANNELS
                or value['query'] or len(value['userAgent']) > 512 or len(value['ip']) > 64
                or (value['user'] != '' and USERNAME.fullmatch(value['user']) is None)): return False
        try:
            if str(uuid.UUID(value['id'])) != value['id']: return False
            if value['user'] != '' or value['password'] != '': password_bytes(value['password'])
        except (ValueError, AttributeError): return False
        # MediaMTX copies the supplied password into token in HTTP auth mode.
        if value['token'] != value['password']: return False
        if value['user'] == '':
            if value['password'] != '': return False
            # Key-less read for allow-listed LAN sources only; write/publish never reach here.
            with self.lock:
                if self.refresh() is None: return False
            good = self.lan.allows(value['ip'])
            if not good: self.lan.denied(value['ip'], 'rtsp')
            return good
        key = hmac.new(self.secret, json.dumps([value['ip'], value['user'], value['password']],
                       separators=(',', ':')).encode(), hashlib.sha256).digest()
        with self.lock:
            data = self.refresh()
            if data is None: return False
            client = data['clients'].get(value['user'])
            if client is not None:
                # 256-bit key: a fast digest check, no KDF slot or attempt bucket needed.
                good = bool(client['enabled'] and self.clients.verify(value['user'], value['password']))
                try: self.audit.append('RTSP_LOGIN_OK' if good else 'RTSP_LOGIN_FAILED', username=value['user'])
                except (OSError, ValueError, sqlite3.Error):
                    self.disarm('audit_unavailable'); return False
                return good
            generation = self.generation
            now = time.monotonic()
            self.cache = {k: expiry for k, expiry in self.cache.items() if expiry > now}
            if key in self.cache: return True
            waiting = self.pending.get(key)
            user = data['users'].get(value['user'])
            name = value['user'] if user is not None else 'unknown'
            if waiting is None:
                bucket = self.attempts.setdefault(name, deque())
                for items in (bucket, self.global_attempts):
                    while items and items[0] <= now - 60: items.popleft()
                if len(bucket) >= 5 or len(self.global_attempts) >= 30: return False
                if not self.kdf.acquire(blocking=False): return False
                self.pending[key] = threading.Event()
                bucket.append(now); self.global_attempts.append(now)
        if waiting is not None:
            # Six tiles may authenticate simultaneously with one account. Share
            # just that in-flight KDF, while keeping all other expensive work capped.
            if not waiting.wait(2): return False
            with self.lock:
                return (self.refresh() is not None and self.generation == generation
                        and self.cache.get(key, 0) > time.monotonic())
        try:
            good = verify_password(value['password'], user['password'] if user else None)
            with self.lock:
                if self.refresh() is None or self.generation != generation: return False
                good = bool(good and user and user['enabled'])
                try: self.audit.append('RTSP_LOGIN_OK' if good else 'RTSP_LOGIN_FAILED', username=name)
                except (OSError, ValueError, sqlite3.Error):
                    self.disarm('audit_unavailable'); return False
                if good:
                    if len(self.cache) >= 64: self.cache.pop(next(iter(self.cache)))
                    # Bound the native DESCRIBE + SETUP handshake cost. New reads
                    # after this window must check the password again.
                    self.cache[key] = time.monotonic() + 15
                return good
        except (ValueError, OSError): return False
        finally:
            with self.lock:
                self.pending.pop(key).set()
                self.kdf.release()


class Callback(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = False
    request_queue_size = 16

    def __init__(self, policy):
        self.policy = policy
        self.path = '/' + secrets.token_urlsafe(32)
        self.slots = threading.BoundedSemaphore(16)
        super().__init__(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.serve_forever, kwargs={'poll_interval': .1}, daemon=True)
        self.thread.start()

    @property
    def url(self): return 'http://127.0.0.1:%d%s' % (self.server_port, self.path)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request); return
        try: super().process_request(request, client_address)
        except Exception:
            self.slots.release(); raise

    def process_request_thread(self, request, client_address):
        try: super().process_request_thread(request, client_address)
        finally: self.slots.release()

    def handle_error(self, request, client_address): pass  # No request contents in logs.

    def close(self):
        self.shutdown(); self.server_close(); self.thread.join(timeout=2)


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        self.request.settimeout(2)
        super().setup()

    def log_message(self, *args): pass

    def do_POST(self):
        good = False
        try:
            sizes = self.headers.get_all('Content-Length', [])
            if (hmac.compare_digest(self.path, self.server.path) and len(sizes) == 1
                    and len(sizes[0]) <= 4 and sizes[0].isdigit() and 0 < int(sizes[0]) <= 4096
                    and self.headers.get('Transfer-Encoding') is None
                    and self.headers.get('Content-Type') == 'application/json'):
                value = decode_json(self.rfile.read(int(sizes[0])), max_bytes=4096)
                good = self.server.policy.authorize(value)
        except (OSError, ValueError, sqlite3.Error): pass
        self.send_response(204 if good else 403)
        self.send_header('Content-Length', '0')
        self.send_header('Connection', 'close')
        self.end_headers()
        self.close_connection = True


class Relay:
    def __init__(self, config, gateway, profile, accounts, audit, binary, directory, api_clients=None, lan_monitor=None):
        self.config, self.gateway, self.profile = config, gateway, profile
        self.binary, self.directory = binary, Path(directory)
        self.proc = self.callback = self.policy = None
        self.reason, self.next_poll, self.retry_at, self.delay = None, 0, 0, 1
        check_ports(config, gateway, profile)
        if profile['enabled']:
            if not accounts or not audit: raise ConfigError('native RTSP requires accounts and audit paths')
            self.policy = Authorization(accounts, audit, api_clients, lan_monitor)
            from .lan_monitor import log as lan_log
            lan_log('rtsp relay ' + self.policy.lan.describe())
            self.callback = Callback(self.policy)

    def close_child(self):
        if self.proc is not None:
            terminate(self.proc)
            self.proc = None
            self.policy.record_revocation()

    def tick(self):
        if self.policy is None: return
        now = time.monotonic()
        if now < self.next_poll: return
        self.next_poll = now + .25
        if self.proc is not None:
            self.policy.refresh()
            if not self.policy.ready or self.proc.poll() is not None:
                self.reason = self.policy.reason or 'native_rtsp_exited'
                self.policy.disarm(self.reason)
                self.close_child()
                self.retry_at = now + self.delay
                self.delay = min(self.delay * 2, 30)
                return
            if all(owns_listener(self.proc.pid, port) for port in listen_ports(self.profile)): self.delay = 1
        elif now >= self.retry_at:
            if not self.policy.arm():
                self.reason = self.policy.reason
                self.retry_at = now + self.delay
                self.delay = min(self.delay * 2, 30)
                return
            path = self.directory / 'rtsp-relay.json'
            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'w') as out: json.dump(render(self.config, self.gateway, self.profile, self.callback.url), out)
            try:
                self.proc = subprocess.Popen([str(self.binary), str(path)], stdout=subprocess.DEVNULL,
                                             stderr=subprocess.DEVNULL, start_new_session=True)
                self.reason = None
            except OSError:
                self.reason = 'native_rtsp_start_failed'
                self.policy.disarm(self.reason)
                self.retry_at = now + self.delay
                self.delay = min(self.delay * 2, 30)

    def snapshot(self):
        if self.policy is None: return dict(enabled=False, state='DISABLED', reason='not_enabled')
        ready = bool(self.policy.ready and self.proc and self.proc.poll() is None
                     and all(owns_listener(self.proc.pid, port) for port in listen_ports(self.profile)))
        host = urlsplit(self.gateway['public_origin']).hostname
        plain = self.profile.get('plain_port')
        return dict(enabled=True, state='READY' if ready else ('ERROR' if self.reason else 'STARTING'),
                    reason=None if ready else (self.reason or 'native_rtsp_not_listening'),
                    base_url='rtsps://%s:%d' % (host, self.profile['port']),
                    plain_base_url='rtsp://%s:%d' % (host, plain) if plain is not None else None,
                    transport='tcp', max_readers_per_channel=4,
                    account_revision=self.policy.active_revision if ready else None)

    def close(self):
        if self.policy: self.policy.disarm('stopped')
        try: self.close_child()
        finally:
            if self.callback: self.callback.close()
