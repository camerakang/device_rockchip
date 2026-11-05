"""Loopback API behind an HTTPS gateway; optional read-only local development."""
import json
import http.client
import re
import socket
import sqlite3
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from . import __version__
from .config import ROOT, decode_json
from .auth import AuthError, COOKIE, PERMISSIONS, TOKEN
from .state import State  # Public compatibility import for 0.1 consumers.

ASSETS = {'/': ('index.html', 'text/html; charset=utf-8'),
          '/player.js': ('player.js', 'text/javascript; charset=utf-8'),
          '/recordings.js': ('recordings.js', 'text/javascript; charset=utf-8'),
          '/sonar-archives.js': ('sonar-archives.js', 'text/javascript; charset=utf-8'),
          '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
          '/session.js': ('session.js', 'text/javascript; charset=utf-8'),
          '/maintenance.js': ('maintenance.js', 'text/javascript; charset=utf-8'),
          '/style.css': ('style.css', 'text/css; charset=utf-8')}



def client_key(name, client):
    # The digest prefix changes on rotation, so sessions opened with an old key are revoked.
    return 'api-client:%s:%s' % (name, client['sha256'][:16])

class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 16

    def __init__(self, address, state, auth=None, maintenance=None, maintenance_audit=None, cleanup_path=None, application_audit=None, rtsp_audit=None, api_clients=None):
        if address[0] != '127.0.0.1':
            raise ValueError('foundation API binds to loopback only')
        self.state = state
        self.auth, self.maintenance, self.maintenance_audit = auth, maintenance, maintenance_audit
        self.application_audit = application_audit
        self.rtsp_audit = rtsp_audit
        self.api_clients = api_clients
        self.grants = None
        self.slots = threading.BoundedSemaphore(16)
        self.event_slots = threading.BoundedSemaphore(4)
        self.download_slots = threading.BoundedSemaphore(4)
        self.stopping = threading.Event()
        self.event_interval = 1.0
        super().__init__(address, Handler)
        if auth:
            from .media_sessions import Grants, save_cleanup_config
            if cleanup_path:
                try: save_cleanup_config(cleanup_path, state.config)
                except Exception:
                    super().server_close(); raise
            self.grants = Grants(state.config, auth, self.client_keys)

    def client_keys(self):
        """Grant owners for enabled API clients; a rotated, disabled or removed key loses its WebRTC sessions."""
        if self.api_clients is None: return set()
        return {client_key(name, client) for name, client in self.api_clients.read()['clients'].items() if client['enabled']}

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def shutdown(self):
        self.stopping.set()
        super().shutdown()

    def server_close(self):
        self.stopping.set()
        if self.grants:
            self.grants.close()
        super().server_close()


class Handler(BaseHTTPRequestHandler):
    server_version = 'SS07/' + __version__

    def setup(self):
        self.request.settimeout(3)
        super().setup()

    def log_message(self, *args):
        pass  # No client paths, credentials or disconnect spam in logs.

    def handle(self):
        try:
            super().handle()
        except AuthError as exc:
            try: self.reply(exc.status, {'error': {'code': exc.code, 'message': exc.code.lower(), 'details': []}})
            except (ConnectionError, socket.timeout): pass
        except (ConnectionError, socket.timeout):
            pass

    def parse_request(self):
        if not super().parse_request(): return False
        authority = self.server.auth.authority if self.server.auth else '127.0.0.1:' + str(self.server.server_port)
        if (len(self.path) > 4096 or not self.path.startswith('/') or self.path.startswith('//')
                or self.headers.get_all('Host', []) != [authority]
                or sum(len(k) + len(v) for k, v in self.headers.items()) > 16384):
            self.reply(400, {'error': {'code': 'INVALID_REQUEST_OR_HOST', 'message': 'Invalid request or Host', 'details': []}})
            return False
        return True

    def cookie(self):
        headers = self.headers.get_all('Cookie', [])
        if len(headers) != 1 or len(headers[0]) > 4096: raise AuthError('LOGIN_REQUIRED')
        values = [item.strip().split('=', 1)[1] for item in headers[0].split(';')
                  if item.strip().startswith(COOKIE + '=')]
        if len(values) != 1 or TOKEN.fullmatch(values[0]) is None: raise AuthError('LOGIN_REQUIRED')
        return values[0]

    def bearer(self):
        values = self.headers.get_all('Authorization', [])
        if not values: return None
        if len(values) != 1 or not values[0].startswith('Bearer ') or len(values[0]) > 128:
            raise AuthError('INVALID_API_KEY')
        return values[0][7:].strip()

    def client(self, key, permission):
        """Machine clients (mobocon) use a Bearer key. It is never sent implicitly by
        browsers, so CSRF/Origin checks do not apply to it."""
        if self.server.api_clients is None or self.headers.get_all('Cookie', []): raise AuthError('INVALID_API_KEY')
        try: found = self.server.api_clients.bearer(key)
        except (OSError, ValueError): raise AuthError('AUTH_UNAVAILABLE', 503)
        if found is None: raise AuthError('INVALID_API_KEY')
        name, client = found
        if permission and permission not in PERMISSIONS[client['role']]: raise AuthError('FORBIDDEN', 403)
        return dict(username=name, role=client['role'], key=client_key(name, client))

    def authorize(self, permission='monitor', mutate=False):
        if not self.server.auth:
            if mutate: raise AuthError('READ_ONLY_DEVELOPMENT', 405)
            self.principal = None
            return None
        self.api_key = key = self.bearer()
        if key is not None:
            self.principal = self.client(key, permission)
            return self.principal
        self.auth_token = self.cookie()
        self.principal = self.server.auth.session(self.auth_token, permission)
        if mutate:
            self.check_origin()
            if len(self.headers.get_all('X-CSRF-Token', [])) != 1: raise AuthError('CSRF_DENIED', 403)
            self.server.auth.csrf(self.principal, self.headers['X-CSRF-Token'])
        return self.principal

    def check_origin(self):
        expected = self.server.auth.origin if self.server.auth else 'http://127.0.0.1:' + str(self.server.server_port)
        if self.headers.get_all('Origin', []) != [expected]: raise AuthError('ORIGIN_DENIED', 403)

    def stream_allowed(self, permission, touch=False):
        if not self.server.auth: return True
        try:
            if getattr(self, 'api_key', None) is not None:
                self.client(self.api_key, permission); return True
            self.server.auth.session(self.auth_token, permission, touch=touch)
            return True
        except AuthError: return False

    def json_body(self, maximum=65536):
        if (self.headers.get('Transfer-Encoding') or len(self.headers.get_all('Content-Length', [])) != 1
                or self.headers.get_content_type() != 'application/json'):
            raise AuthError('INVALID_JSON_REQUEST', 400)
        try:
            length = int(self.headers['Content-Length'])
            if not 1 <= length <= maximum: raise ValueError('body size')
            raw = self.rfile.read(length)
            if len(raw) != length: raise ValueError('short body')
            return decode_json(raw, max_bytes=maximum)
        except ValueError: raise AuthError('INVALID_JSON_REQUEST', 400)

    def send_headers(self, code, content_type, length=None, extra=None):
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        if length is not None:
            self.send_header('Content-Length', str(length))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; frame-ancestors 'none'")
        self.send_header('Referrer-Policy', 'no-referrer')
        for key, value in (extra or {}).items(): self.send_header(key, value)
        self.end_headers()

    def reply(self, code, payload, extra=None):
        body = json.dumps(dict(request_id=str(uuid.uuid4()), **payload), allow_nan=False).encode()
        self.send_headers(code, 'application/json; charset=utf-8', len(body), extra)
        if self.command != 'HEAD':
            self.wfile.write(body)

    def do_GET(self):
        download = self.path.split('?', 1)[0].endswith('/content')
        if download and not self.server.download_slots.acquire(blocking=False):
            self.reply(503, {'error': {'code': 'DOWNLOAD_LIMIT', 'message': 'Too many downloads', 'details': []}})
            return
        try: self.get()
        finally:
            if download: self.server.download_slots.release()

    def get(self):
        try:
            path = urlsplit(self.path).path
        except ValueError:
            self.reply(400, {'error': {'code': 'BAD_REQUEST', 'message': 'Invalid URL', 'details': []}})
            return
        if path in ASSETS:
            filename, content_type = ASSETS[path]
            body = (ROOT / 'web' / filename).read_bytes()
            self.send_headers(200, content_type, len(body))
            self.wfile.write(body)
            return
        if path == '/api/v1/session':
            headers = {}
            if not self.server.auth:
                data = dict(authenticated=False, authentication_required=False, role='development_read_only',
                            permissions=['monitor', 'recordings.read'], csrf_token=None)
            else:
                try: data = self.server.auth.public(self.server.auth.session(self.cookie()))
                except AuthError as exc:
                    if exc.status != 401: raise
                    if self.server.auth.direct_access:
                        if self.headers.get('Sec-Fetch-Site') == 'cross-site':
                            raise AuthError('ORIGIN_DENIED', 403)
                        token, session = self.server.auth.open_session()
                        data = self.server.auth.public(session)
                        headers['Set-Cookie'] = COOKIE + '=' + token + '; Secure; HttpOnly; SameSite=Strict; Path=/'
                    else:
                        data = dict(authenticated=False, authentication_required=True, role=None, permissions=[], csrf_token=None)
            self.reply(200, {'data': data}, headers); return
        if path != '/healthz':
            permission = 'maintain' if path in ('/api/v1/config', '/api/v1/logs') else (
                'recordings.read' if path.startswith(('/api/v1/recordings','/api/v1/sonars/archives')) or path in ('/api/v1/timecode/history', '/api/v1/sonars/history') else 'monitor')
            self.authorize(permission)
        if path == '/api/v1/logs': self.logs(); return
        from .recording_http import handle as recording_request
        if recording_request(self): return
        from .timecode_http import history
        if history(self): return
        from .sonar_http import history as sonar_history
        if sonar_history(self): return
        from .sonar_archive_http import handle as sonar_archives
        if sonar_archives(self):return
        if path == '/api/v1/events':
            self.events()
            return
        data = self.server.state.data(path)
        if data is None:
            self.reply(404, {'error': {'code': 'NOT_FOUND', 'message': 'Endpoint not implemented', 'details': []}})
        else:
            if path == '/api/v1/config':
                data['mutable'] = bool(self.server.auth and self.server.maintenance)
                data['maintenance_worker'] = self.server.state.workers.read('maintenance')['state']
            self.reply(200, {'data': data})

    def logs(self):
        if not self.server.auth: raise AuthError('READ_ONLY_DEVELOPMENT', 405)
        try:
            query_text = urlsplit(self.path).query
            query = parse_qs(query_text, strict_parsing=True, keep_blank_values=True, max_num_fields=4) if query_text else {}
            if set(query) - {'source', 'limit', 'cursor', 'download'} or any(len(v) != 1 for v in query.values()):
                raise ValueError('query')
            source = query.get('source', ['access'])[0]
            if source not in ('access', 'maintenance', 'application', 'rtsp', 'timecode', 'recording'): raise ValueError('source')
            limit, cursor = int(query.get('limit', ['50'])[0]), query.get('cursor', [None])[0]
            if not 1 <= limit <= 100 or query.get('download', ['0'])[0] not in ('0', '1'): raise ValueError('limit')
            from .snapshots import u64
            if cursor is not None and not 1 <= u64(cursor) <= 2**63-1: raise ValueError('cursor')
        except ValueError: raise AuthError('INVALID_LOG_QUERY', 422)
        try:
            if source == 'recording':
                if cursor is not None: raise AuthError('INVALID_LOG_QUERY', 422)
                if not self.server.state.catalog: raise OSError('catalog missing')
                data = dict(items=self.server.state.catalog.events(limit), next_cursor=None,
                            complete_history=False, retention='latest_recording_events')
            else:
                journal = {'access': self.server.auth.audit, 'maintenance': self.server.maintenance_audit,
                           'application': self.server.application_audit,
                           'rtsp': self.server.rtsp_audit,
                           'timecode': self.server.state.timecode_journal}[source]
                if journal is None: raise OSError('journal unavailable')
                data = journal.history(limit=limit, cursor=cursor)
            extra = {'Content-Disposition': 'attachment; filename="ss07-' + source + '-events.json"'} if query.get('download') == ['1'] else None
            self.reply(200, {'data': dict(data, source=source)}, extra)
        except AuthError: raise
        except (OSError, sqlite3.Error, ValueError): raise AuthError('LOG_UNAVAILABLE', 503)

    def events(self):
        if not self.server.event_slots.acquire(blocking=False):
            self.reply(503, {'error': {'code': 'EVENT_LIMIT', 'message': 'Too many event subscribers', 'details': []}})
            return
        try:
            self.send_headers(200, 'text/event-stream; charset=utf-8')
            # No event backlog: on reconnect send current complete snapshot.
            while not self.server.stopping.is_set():
                if not self.stream_allowed('monitor', touch=True): return
                data = self.server.state.snapshot()
                event_id = data['service_instance_id'] + ':' + data['seq']
                payload = json.dumps({'request_id': str(uuid.uuid4()), 'data': data}, allow_nan=False)
                self.wfile.write(('retry: 2000\nid: ' + event_id + '\nevent: status\ndata: ' + payload + '\n\n').encode())
                self.wfile.flush()
                if self.server.stopping.wait(self.server.event_interval):
                    break
        finally:
            self.server.event_slots.release()

    def deny_write(self):
        self.reply(405, {'error': {'code': 'READ_ONLY_PHASE', 'message': 'HTTP maintenance writes are not enabled', 'details': []}})

    def media_request(self, method):
        """Only WHEP session creation/deletion; never proxy the media admin API."""
        match = re.fullmatch(r'/media/(front|back|left|right|down|sdi)/whep(?:/([a-fA-F0-9-]{36}))?', self.path)
        if not match or (method == 'POST') != (match[2] is None):
            self.reply(404, {'error': {'code': 'NOT_FOUND', 'message': 'Unknown media endpoint', 'details': []}})
            return
        self.authorize('monitor', mutate=bool(self.server.auth))
        if self.server.auth and method == 'DELETE' and not self.server.grants.owns(self.path, self.principal['key']):
            raise AuthError('MEDIA_SESSION_NOT_FOUND', 404)
        expected_origin = self.server.auth.origin if self.server.auth else 'http://127.0.0.1:' + str(self.server.server_address[1])
        # Bearer keys are never attached by browsers implicitly, so a client app's own Origin is acceptable.
        if getattr(self, 'api_key', None) is None and self.headers.get('Origin') not in (None, expected_origin):
            self.reply(403, {'error': {'code': 'ORIGIN_DENIED', 'message': 'Invalid origin', 'details': []}})
            return
        try:
            if self.headers.get('Transfer-Encoding') or len(self.headers.get_all('Content-Length', [])) > 1:
                raise ValueError('ambiguous length')
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 <= length <= 32768 or (method == 'DELETE' and length):
                raise ValueError('invalid length')
            if method == 'POST' and (length == 0 or self.headers.get_content_type() != 'application/sdp'):
                raise ValueError('SDP required')
            body = self.rfile.read(length)
            if len(body) != length:
                raise ValueError('incomplete body')
        except ValueError:
            self.reply(400, {'error': {'code': 'INVALID_MEDIA_REQUEST', 'message': 'Invalid SDP request', 'details': []}})
            return
        from .media import settings
        reserved = bool(self.server.auth and method == 'POST')
        marker = self.server.grants.begin(self.principal['key']) if reserved else None
        conn = http.client.HTTPConnection('127.0.0.1', settings(self.server.state.config)['webrtc_port'], timeout=3)
        try:
            conn.request(method, self.path[len('/media'):], body=body,
                         headers=dict({'Content-Type': 'application/sdp'}, **({'User-Agent': marker} if marker else {})))
            response = conn.getresponse()
            answer = response.read(32769)
            if len(answer) > 32768:
                raise ValueError('media response too large')
            location = response.getheader('Location')
            if location:
                parsed = urlsplit(location)
                # MediaMTX may return a relative or absolute session URL. Only
                # expose the matching channel and opaque ID; never its host.
                if not re.fullmatch('/' + match[1] + r'/whep/[a-fA-F0-9-]{36}', parsed.path):
                    raise ValueError('invalid session location')
                location = '/media' + parsed.path
            if self.server.auth:
                if method == 'POST' and response.status == 201:
                    if location is None: raise ValueError('missing WHEP session')
                    self.server.grants.add(location, self.principal['key'], marker)
                    if self.server.stopping.is_set():
                        self.server.grants.remove(location)
                        raise AuthError('SERVICE_STOPPING', 503)
                    if not self.stream_allowed('monitor'):
                        self.server.grants.remove(location)
                        raise AuthError('LOGIN_REQUIRED')
                elif method == 'DELETE' and response.status in (200, 204, 404, 410):
                    with self.server.grants.lock: self.server.grants.paths.pop(self.path, None)
            self.send_response(response.status)
            self.send_header('Content-Type', 'application/sdp' if response.status == 201 else 'text/plain')
            self.send_header('Content-Length', str(len(answer)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            if location:
                self.send_header('Location', location)
            self.end_headers()
            self.wfile.write(answer)
        except AuthError: raise
        except (OSError, ValueError, http.client.HTTPException):
            self.reply(502, {'error': {'code': 'MEDIA_UNAVAILABLE', 'message': 'Media service unavailable', 'details': []}})
        finally:
            conn.close()
            if reserved: self.server.grants.end(self.principal['key'])

    def session_write(self, login):
        auth = self.server.auth
        if auth is None: raise AuthError('READ_ONLY_DEVELOPMENT', 405)
        try:
            if login:
                self.check_origin()
                body = self.json_body(2048)
                if type(body) is not dict or set(body) != {'username', 'password'}: raise AuthError('INVALID_LOGIN', 422)
                token, session = auth.login(body['username'], body['password'])
                value = COOKIE + '=' + token + '; Secure; HttpOnly; SameSite=Strict; Path=/'
                self.reply(200, {'data': auth.public(session)}, {'Set-Cookie': value})
            else:
                if self.headers.get_all('Authorization', []): raise AuthError('INVALID_LOGOUT', 422)
                self.authorize('monitor', mutate=True)
                if self.json_body(128) != {}: raise AuthError('INVALID_LOGOUT', 422)
                auth.logout(self.auth_token)
                self.reply(200, {'data': {'authenticated': False}},
                           {'Set-Cookie': COOKIE + '=; Secure; HttpOnly; SameSite=Strict; Path=/; Max-Age=0'})
        except AuthError: raise
        except (OSError, sqlite3.Error): raise AuthError('AUTH_UNAVAILABLE', 503)
        except ValueError: raise AuthError('INVALID_LOGIN', 422)

    def maintenance_write(self, operation, record_id=None):
        self.authorize('maintain', mutate=True)
        if self.server.maintenance is None: raise AuthError('MAINTENANCE_UNAVAILABLE', 503)
        body = self.json_body()
        if type(body) is not dict: raise AuthError('INVALID_MAINTENANCE_REQUEST', 422)
        if record_id:
            if 'recording_id' in body: raise AuthError('INVALID_MAINTENANCE_REQUEST', 422)
            body['recording_id'] = record_id
        try:
            result = self.server.maintenance.request({k: self.principal[k] for k in ('username', 'role')}, operation, body)
        except (OSError, ValueError): raise AuthError('MAINTENANCE_RESULT_UNCERTAIN', 503)
        if 'error' in result:
            code, status = result['error']['code'], result['error']['status']
            if status not in (403, 404, 409, 422, 503): raise AuthError('MAINTENANCE_UNAVAILABLE', 503)
            raise AuthError(code, status)
        self.reply(202 if operation in ('config.apply', 'config.recover') else 200,
                   {'data': result['data'], 'operation_id': result['request_id']})

    def do_POST(self):
        if self.path == '/api/v1/login': self.session_write(True)
        elif self.path == '/api/v1/logout': self.session_write(False)
        elif self.path in ('/api/v1/config/plan', '/api/v1/config/apply', '/api/v1/config/recover'):
            self.maintenance_write('config.' + self.path.rsplit('/', 1)[1])
        elif re.fullmatch(r'/api/v1/recordings/[a-f0-9-]{36}/lock', self.path):
            self.maintenance_write('recording.lock', self.path.split('/')[4])
        elif self.path.startswith('/media/'):
            self.media_request('POST')
        else:
            self.deny_write()

    def do_DELETE(self):
        if self.path.startswith('/media/'):
            self.media_request('DELETE')
        else:
            self.deny_write()

    def do_PUT(self):
        if self.path == '/api/v1/config': self.maintenance_write('config.update')
        else: self.deny_write()

    do_PATCH = deny_write

    def do_HEAD(self):
        self.authorize('recordings.read')
        from .recording_http import handle as recording_request
        from .sonar_archive_http import handle as sonar_archives
        if not recording_request(self, head=True) and not sonar_archives(self,head=True):
            self.deny_write()
