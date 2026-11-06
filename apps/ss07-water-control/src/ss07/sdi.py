"""SDI channel observations: encoder input signal and the real relayed frame rate.

EncoderStatus reads the AVMatrix SE2017 input status through the encoder's own
web API (the same GET its web page uses). Credentials come from a root-managed
file; nothing is written to the encoder. FrameMeter is a read-only RTSP/TCP
client of the loopback MediaMTX that counts H.264 access units (RTP marker bit)
without decoding. Both run in background threads and never block the media loop.
"""
import base64
import http.client
import json
import math
import os
import re
import socket
import stat
import struct
import threading
import time
from urllib.parse import urlsplit

from .config import decode_json

SIGNALS = ('present', 'no_signal', 'unknown')
MODELS = ('avmatrix-se2017',)
STALE_S = 10.0


def load_encoder(path):
    """Return credentials or None. Mode must keep the password away from other users."""
    if path is None: return None
    try: fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError: return None
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o027
                or info.st_uid not in (0, os.geteuid())):
            raise ValueError('SDI encoder credentials must be owner-controlled and not world-readable')
        value = decode_json(stream.read(4097), max_bytes=4096)
    if (type(value) is not dict or set(value) != {'schema_version', 'model', 'username', 'password'}
            or value['schema_version'] != 1 or value['model'] not in MODELS
            or not all(isinstance(value[k], str) and 0 < len(value[k]) <= 64 for k in ('username', 'password'))):
        raise ValueError('invalid SDI encoder credentials')
    return value


def classify(text):
    """SE2017 reports e.g. 'SDI NoSignal N/A'. Anything else non-empty is an input it locked onto."""
    if not isinstance(text, str) or not text.strip(): return 'unknown', None
    shown = re.sub(r'[^\x20-\x7e]', '', text)[:64]
    if re.search(r'no\s*signal', text, re.I): return 'no_signal', shown
    return 'present', shown


class EncoderStatus:
    def __init__(self, host, credentials, stop, interval=3.0, port=80):
        self.host, self.port, self.credentials = host, port, credentials
        self.stop, self.interval = stop, interval
        self.token = None
        self.lock = threading.Lock()
        self.value = dict(input_signal='unknown', input_info=None, encoder_error='encoder_not_polled')
        self.updated = 0.0
        self.thread = threading.Thread(target=self._run, daemon=True, name='sdi-encoder')
        self.thread.start()

    def _call(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=3)
        try:
            conn.request(method, '/cgi-bin/entry.cgi' + path, body=body, headers=headers or {})
            response = conn.getresponse()
            raw = response.read(65537)
            if len(raw) > 65536: raise ValueError('encoder response too large')
            return response, json.loads(raw.decode('utf-8', 'replace') or 'null')
        finally: conn.close()

    def _login(self):
        c = self.credentials
        body = json.dumps({'sUserName': c['username'],
                           'sPassword': base64.b64encode(c['password'].encode()).decode()}).encode()
        response, value = self._call('PUT', '/system/login', body,
                                     {'Content-Type': 'application/json', 'Authorization': 'Bearer undefined'})
        token = None
        for header in response.getheaders():
            if header[0].lower() == 'set-cookie':
                match = re.match(r'\s*token=([^;\s]+)', header[1])
                if match: token = match[1]
        if not isinstance(value, dict) or value.get('status', -1) < 0 or not token:
            raise PermissionError('encoder_auth_failed')
        self.token = token

    def _status(self):
        path = '/system/device-status?timestamp=%d' % int(time.time() * 1000)
        _, value = self._call('GET', path, headers={'Authorization': 'Bearer ' + self.token,
                                                 'Cookie': 'token=' + self.token})
        if not isinstance(value, list): return None  # e.g. {"error":{"code":401}} after the session expired
        for item in value:
            if isinstance(item, dict) and item.get('name') == 'inputVideoInfo':
                return item.get('value')
        raise ValueError('encoder_response_invalid')

    def poll(self):
        try:
            if self.token is None: self._login()
            text = self._status()
            if text is None:
                self._login(); text = self._status()
                if text is None: raise PermissionError('encoder_auth_failed')
            signal, shown = classify(text)
            value = dict(input_signal=signal, input_info=shown, encoder_error=None)
        except PermissionError as exc:
            self.token, value = None, dict(input_signal='unknown', input_info=None, encoder_error=str(exc))
        except (OSError, ValueError, http.client.HTTPException):
            self.token, value = None, dict(input_signal='unknown', input_info=None, encoder_error='encoder_unreachable')
        with self.lock: self.value, self.updated = value, time.monotonic()

    def _run(self):
        while not self.stop.is_set():
            self.poll()
            self.stop.wait(self.interval)

    def snapshot(self):
        with self.lock:
            if time.monotonic() - self.updated > STALE_S:
                return dict(input_signal='unknown', input_info=None, encoder_error='encoder_status_stale')
            return dict(self.value)


class FrameMeter:
    """Counts access units of the loopback 'sdi' path; one RTSP reader, no decoding."""

    def __init__(self, port, stop, path='sdi', window=2.0):
        self.url = 'rtsp://127.0.0.1:%d/%s' % (port, path)
        self.port, self.stop, self.window = port, stop, window
        self.lock = threading.Lock()
        self.fps, self.updated = None, 0.0
        self.thread = threading.Thread(target=self._run, daemon=True, name='sdi-meter')
        self.thread.start()

    def snapshot(self):
        with self.lock:
            return self.fps if time.monotonic() - self.updated < 3.0 else None

    def _run(self):
        delay = 1
        while not self.stop.is_set():
            try:
                self._session()
                delay = 1
            except (OSError, ValueError, KeyError, IndexError, struct.error):
                pass
            with self.lock: self.fps = None
            self.stop.wait(delay)
            delay = min(delay * 2, 16)

    def _session(self):
        sock = socket.create_connection(('127.0.0.1', self.port), timeout=3)
        reader = _Reader(sock)
        try:
            cseq = [0]

            def request(method, url, extra=None):
                cseq[0] += 1
                headers = dict({'CSeq': str(cseq[0]), 'User-Agent': 'ss07-sdi-meter'}, **(extra or {}))
                sock.sendall((method + ' ' + url + ' RTSP/1.0\r\n'
                              + ''.join('%s: %s\r\n' % kv for kv in headers.items()) + '\r\n').encode())
                while True:
                    item = reader.next()
                    if item[0] == 'response':
                        if not item[1].startswith('RTSP/1.0 200'): raise ValueError('rtsp ' + item[1][:40])
                        return item[2], item[3]

            fields, sdp = request('DESCRIBE', self.url, {'Accept': 'application/sdp'})
            control = video_control(sdp.decode('utf-8', 'replace'))
            base = fields.get('content-base', self.url)
            target = control if control.startswith('rtsp://') else base.rstrip('/') + '/' + control
            fields, _ = request('SETUP', target, {'Transport': 'RTP/AVP/TCP;unicast;interleaved=0-1'})
            session = fields['session'].split(';', 1)[0]
            request('PLAY', self.url, {'Session': session})
            frames, started, keepalive = 0, time.monotonic(), time.monotonic()
            while not self.stop.is_set():
                item = reader.next()
                if item[0] == 'rtp' and item[1] == 0 and len(item[2]) >= 12 and item[2][1] & 0x80:
                    frames += 1
                now = time.monotonic()
                if now - started >= self.window:
                    with self.lock: self.fps, self.updated = round(frames / (now - started), 2), now
                    frames, started = 0, now
                if now - keepalive >= 20:
                    cseq[0] += 1
                    sock.sendall(('OPTIONS %s RTSP/1.0\r\nCSeq: %d\r\nSession: %s\r\n\r\n'
                                  % (self.url, cseq[0], session)).encode())
                    keepalive = now
        finally:
            sock.close()


def video_control(sdp):
    """Control attribute of the first H.264 video section."""
    section, control, h264 = None, None, False
    for line in sdp.splitlines():
        if line.startswith('m='):
            if section == 'video' and h264: break
            section, control, h264 = line[2:].split(' ', 1)[0], None, False
        elif section == 'video' and line.startswith('a=control:'):
            control = line[10:].strip()
        elif section == 'video' and line.startswith('a=rtpmap:') and 'H264/' in line:
            h264 = True
    if section != 'video' or not h264 or not control: raise ValueError('no H.264 video track')
    return control


class _Reader:
    def __init__(self, sock):
        self.sock, self.buffer = sock, bytearray()

    def _need(self, count):
        while len(self.buffer) < count:
            data = self.sock.recv(65536)
            if not data: raise OSError('rtsp closed')
            self.buffer.extend(data)
            if len(self.buffer) > 1 << 20: raise ValueError('rtsp buffer overflow')

    def next(self):
        self._need(1)
        if self.buffer[0] == 0x24:
            self._need(4)
            channel, size = self.buffer[1], struct.unpack('!H', self.buffer[2:4])[0]
            self._need(size + 4)
            payload = bytes(self.buffer[4:size + 4]); del self.buffer[:size + 4]
            return ('rtp', channel, payload)
        while b'\r\n\r\n' not in self.buffer:
            if len(self.buffer) > 16384: raise ValueError('rtsp header too large')
            self._need(len(self.buffer) + 1)
        end = self.buffer.index(b'\r\n\r\n') + 4
        lines = bytes(self.buffer[:end]).decode('utf-8', 'replace').split('\r\n')
        fields = {l.split(':', 1)[0].strip().lower(): l.split(':', 1)[1].strip() for l in lines[1:] if ':' in l}
        size = int(fields.get('content-length', '0'))
        if not 0 <= size <= 65536: raise ValueError('rtsp body size')
        self._need(end + size)
        body = bytes(self.buffer[end:end + size]); del self.buffer[:end + size]
        return ('response', lines[0], fields, body)


class SdiObserver:
    """Owned by the media worker; None-safe when the SDI source is not configured."""

    def __init__(self, config, rtsp_port, encoder_path, stop):
        url = config['sdi']['rtsp_url']
        self.meter = self.encoder = None
        self.error = None
        if url is None: return
        self.meter = FrameMeter(rtsp_port, stop)
        try: credentials = load_encoder(encoder_path)
        except (OSError, ValueError) as exc:
            credentials, self.error = None, 'encoder_credentials_invalid'
            print('sdi: ' + str(exc), flush=True)
        if credentials is not None:
            self.encoder = EncoderStatus(urlsplit(url).hostname, credentials, stop)
        elif self.error is None:
            self.error = 'encoder_status_not_configured'

    def snapshot(self):
        fps = self.meter.snapshot() if self.meter else None
        value = (self.encoder.snapshot() if self.encoder
                 else dict(input_signal='unknown', input_info=None, encoder_error=self.error))
        value['fps_observed'] = fps if fps is None or math.isfinite(fps) else None
        return value
