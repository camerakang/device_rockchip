"""Bind live WHEP grants to login sessions, revoke them without stopping recording."""
import http.client
import os
import re
import stat
import threading
import time
import uuid

from .auth import AuthError
from .config import decode_json
from .media import settings
from .persistence import atomic_json

SESSION = re.compile(r'/media/(front|back|left|right|down|sdi)/whep/([a-fA-F0-9-]{36})')


def save_cleanup_config(path, config):
    atomic_json(path, dict(schema_version=1, media_api_port=settings(config)['api_port']), mode=0o600)


def load_cleanup_config(path):
    fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid not in (0, os.geteuid()) or info.st_mode & 0o077:
            raise ValueError('untrusted media cleanup state')
        value = decode_json(stream.read(1025), max_bytes=1024)
    if (type(value) is not dict or set(value) != {'schema_version', 'media_api_port'}
            or type(value['schema_version']) is not int or value['schema_version'] != 1
            or type(value['media_api_port']) is not int or not 1024 <= value['media_api_port'] <= 65535):
        raise ValueError('invalid media cleanup state')
    return dict(media=dict(api_port=value['media_api_port']))


def media_call(port, method, path):
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=.5)
    try:
        conn.request(method, path)
        response = conn.getresponse(); raw = response.read(262145)
        if len(raw) > 262144: raise ValueError('media response too large')
        return response.status, raw
    finally: conn.close()


def inventory(config):
    port = settings(config)['api_port']
    status, raw = media_call(port, 'GET', '/v3/webrtc/sessions/list?itemsPerPage=128')
    if status != 200: raise OSError('media session inventory unavailable')
    result = decode_json(raw, max_bytes=262144)
    if type(result) is not dict: raise ValueError('invalid media inventory')
    items = result.get('items')
    if not isinstance(items, list) or len(items) > 128: raise ValueError('invalid media inventory')
    for item in items:
        if type(item) is not dict: raise ValueError('invalid media session')
        identity = item.get('id')
        if not isinstance(identity, str) or re.fullmatch('[a-fA-F0-9-]{36}', identity) is None:
            raise ValueError('invalid media session ID')
        if not isinstance(item.get('userAgent', ''), str): raise ValueError('invalid media agent')
    return items


def kick_orphans(config, maximum=8):
    """Only called before issuing live-video grants or by the API stop hook."""
    port = settings(config)['api_port']
    items = inventory(config)
    for item in items[:maximum]:
        identity = item['id']
        status, _ = media_call(port, 'POST', '/v3/webrtc/sessions/kick/' + identity)
        if status not in (200, 404): raise OSError('media session revoke failed')
    return not items


def close_all(config):
    """systemd stop hook, including abnormal API exits; never stops publishers."""
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:
        try:
            if kick_orphans(config): return dict(webrtc_sessions_closed=True)
        except (OSError, ValueError, http.client.HTTPException):
            pass
        time.sleep(.1)
    raise OSError('could not confirm WebRTC sessions closed')


class Grants:
    def __init__(self, config, auth, client_keys=None):
        self.config, self.auth, self.client_keys = config, auth, client_keys
        self.lock = threading.Lock()
        self.paths, self.pending = {}, {}
        self.ready = False
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def begin(self, key):
        with self.lock:
            if self.stop.is_set(): raise AuthError('SERVICE_STOPPING', 503)
            if not self.ready: raise AuthError('MEDIA_AUTH_STARTING', 503)
            if len(self.paths) + sum(self.pending.values()) >= 48: raise AuthError('MEDIA_SESSION_LIMIT', 503)
            if sum(value['owner'] == key for value in self.paths.values()) + self.pending.get(key, 0) >= 12:
                raise AuthError('MEDIA_SESSION_LIMIT', 503)
            self.pending[key] = self.pending.get(key, 0) + 1
            return 'ss07-grant-' + uuid.uuid4().hex

    def end(self, key):
        with self.lock:
            self.pending[key] -= 1
            if not self.pending[key]: del self.pending[key]

    def add(self, path, key, marker):
        if SESSION.fullmatch(path) is None: raise ValueError('invalid WHEP grant')
        with self.lock: self.paths[path] = dict(owner=key, marker=marker)

    def owns(self, path, key):
        with self.lock: return self.paths.get(path, {}).get('owner') == key

    def prune_ended(self):
        # WHEP resource secrets differ from the public native session IDs.
        # A per-request User-Agent marker correlates them without exposing either.
        with self.lock: snapshot = dict(self.paths)
        if not snapshot: return
        markers = {item.get('userAgent') for item in inventory(self.config)}
        with self.lock:
            for path, value in snapshot.items():
                if value['marker'] not in markers and self.paths.get(path) == value:
                    del self.paths[path]

    def remove(self, path):
        status, _ = media_call(settings(self.config)['webrtc_port'], 'DELETE', path[len('/media'):])
        if status in (200, 204, 404, 410):
            with self.lock: self.paths.pop(path, None)
        else: raise OSError('WHEP revoke failed')

    def _run(self):
        while not self.stop.is_set():
            try:
                if not self.ready:
                    self.ready = kick_orphans(self.config)
                else:
                    try: active = self.auth.active_keys()
                    except AuthError: active = set()
                    if self.client_keys:
                        try: active = active | self.client_keys()
                        except (OSError, ValueError, AuthError): pass  # unreadable client file: revoke client sessions
                    with self.lock: revoked = [path for path, value in self.paths.items() if value['owner'] not in active]
                    for path in revoked[:8]: self.remove(path)
                    self.prune_ended()
            except (OSError, ValueError, http.client.HTTPException):
                pass
            self.stop.wait(.5)

    def close(self):
        self.stop.set(); self.thread.join(timeout=1)
        deadline = time.monotonic() + 3
        with self.lock: paths = list(self.paths)
        for path in paths:
            if time.monotonic() >= deadline: break
            try: self.remove(path)
            except (OSError, ValueError, http.client.HTTPException): break
