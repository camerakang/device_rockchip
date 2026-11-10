"""Remote settings of the SDI encoder (AVMatrix SE2017) for maintainers and mobocon.

RK logs in to the encoder with the root-managed credentials and exposes a narrow,
validated subset: video source, H.264 encoding, and audio level/bitrate. Network,
stream protocol, firmware and factory-reset functions stay on the encoder's own page
because changing them can cut the RK pull path. The encoder accepts out-of-range
values without complaint, so every value is checked here against both a fixed
allow-list and the encoder's own published option table, then read back.
"""
import base64
import hashlib
import http.client
import json
import re
import threading
import time

from .sdi import classify

PROFILES = {'highP': 'high', 'main': 'main', 'baseline': 'baseline'}
RESOLUTIONS = ('1920x1080', '1280x720', '720x480')
FRAME_RATES = (24, 25, 30, 50, 60)
RATE_CONTROLS = ('CBR', 'VBR')
MAX_BITRATES_KBPS = (256, 512, 1024, 2048, 4096, 8192, 12288, 16384, 24576, 32768)
GOP_RANGE = (5, 300)
AUDIO_BITRATES = (64000, 128000, 256000, 320000)
VOLUME_RANGE = (0, 100)
SOURCES = ('SDI', 'HDMI', 'LINEIN')
FIELDS = ('video_source', 'resolution', 'fps', 'codec', 'profile', 'rate_control', 'max_bitrate_kbps', 'gop',
          'audio_source', 'audio_bitrate', 'audio_volume')
# Keys the encoder's own page strips before PUT; sending them back is rejected or ignored.
VIDEO_DROP = ('iTargetRate', 'sVideoEncoderConfigurationName', 'sVideoEncoderConfigurationToken', 'sVideoSourceToken')
AUDIO_DROP = ('iSampleRate', 'id', 'sBitWidth', 'sSoundChannel')


class EncoderError(Exception):
    """code is a stable API error code; status the HTTP status RK returns."""
    def __init__(self, code, status=503, details=()):
        super().__init__(code)
        self.code, self.status, self.details = code, status, list(details)


def revision(settings):
    return hashlib.sha256(json.dumps(settings, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _int(text):
    try: return int(str(text).strip())
    except (TypeError, ValueError): return None


def _list(info, name):
    value = info.get(name)
    return [v for v in value.split(',') if v in SOURCES] if isinstance(value, str) else []


class EncoderClient:
    def __init__(self, host, credentials, port=80, timeout=5.0):
        self.host, self.port, self.credentials, self.timeout = host, port, credentials, timeout
        self.token = None
        self.lock = threading.Lock()  # One read-modify-write at a time; also guards the token.

    # ---- transport -------------------------------------------------------------------------
    def _call(self, method, path, body=None, auth=True):
        headers = {'Content-Type': 'application/json'}
        if auth:
            headers.update(Authorization='Bearer ' + self.token, Cookie='token=' + self.token)
        else:
            headers['Authorization'] = 'Bearer undefined'
        if method == 'GET':
            path += ('&' if '?' in path else '?') + 'timestamp=%d' % int(time.time() * 1000)
        conn = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
        try:
            conn.request(method, '/cgi-bin/entry.cgi' + path,
                         body=None if body is None else json.dumps(body).encode(), headers=headers)
            response = conn.getresponse()
            raw = response.read(262145)
            if len(raw) > 262144: raise ValueError('encoder response too large')
            return response, json.loads(raw.decode('utf-8', 'replace') or 'null')
        finally: conn.close()

    def _login(self):
        c = self.credentials
        body = {'sUserName': c['username'], 'sPassword': base64.b64encode(c['password'].encode()).decode()}
        response, value = self._call('PUT', '/system/login', body, auth=False)
        token = None
        for name, text in response.getheaders():
            if name.lower() == 'set-cookie':
                match = re.match(r'\s*token=([^;\s]+)', text)
                if match: token = match[1]
        if not isinstance(value, dict) or value.get('status', -1) < 0 or not token:
            raise EncoderError('SDI_ENCODER_AUTH_FAILED')
        self.token = token

    def _request(self, method, path, body=None):
        """Authenticated call; one re-login when the encoder session has expired."""
        try:
            for attempt in (0, 1):
                if self.token is None: self._login()
                _, value = self._call(method, path, body)
                if isinstance(value, dict) and isinstance(value.get('error'), dict):
                    message = str(value['error'].get('message', ''))
                    if attempt == 0 and (value['error'].get('code') == 401 or 'token' in message):
                        self.token = None; continue
                    raise EncoderError('SDI_ENCODER_REJECTED', 502, [message[:120]])
                return value
        except EncoderError: raise
        except (OSError, ValueError, http.client.HTTPException):
            self.token = None
            raise EncoderError('SDI_ENCODER_UNAVAILABLE')
        raise EncoderError('SDI_ENCODER_AUTH_FAILED')

    # ---- model -----------------------------------------------------------------------------
    def _raw(self):
        video = self._request('GET', '/video/0')
        audio = self._request('GET', '/audio/0')
        info = self._request('GET', '/system/device-info')
        status = self._request('GET', '/system/device-status')
        table = self._request('GET', '/system/para/video-encoder')
        if isinstance(table, str):
            try: table = json.loads(table)
            except ValueError: table = None
        if not (isinstance(video, dict) and isinstance(audio, dict) and isinstance(info, list) and isinstance(status, list)):
            raise EncoderError('SDI_ENCODER_RESPONSE_INVALID', 502)
        items = {i['name']: i for i in info if isinstance(i, dict) and isinstance(i.get('name'), str)}
        values = {i['name']: i.get('value') for i in status if isinstance(i, dict) and isinstance(i.get('name'), str)}
        return video, audio, items, values, table if isinstance(table, dict) else {}

    @staticmethod
    def _options(video, items, table):
        def table_options(*path):
            node = table
            for key in path: node = node.get(key, {}) if isinstance(node, dict) else {}
            return node.get('options') if isinstance(node, dict) and isinstance(node.get('options'), list) else None
        fps_in = _int(video.get('sFrameRateIn'))
        res = table_options('dynamic', 'sStreamType', 'mainStream', 'sResolution')
        rates = table_options('dynamic', 'sStreamType', 'mainStream', 'iMaxRate')
        fps = table_options('static', 'sFrameRate')
        profiles = table_options('static', 'sH264Profile')
        rc = table_options('static', 'sRCMode')
        gop = (table.get('static', {}).get('iGOP', {}).get('range') if isinstance(table.get('static'), dict) else None) or {}
        gop_min = max(GOP_RANGE[0], _int(gop.get('min')) or GOP_RANGE[0])
        gop_max = min(GOP_RANGE[1], _int(gop.get('max')) or GOP_RANGE[1])
        info = {k: v.get('value') for k, v in items.items()}
        return dict(
            video_source=_list(info, 'videoSourceList'),
            resolution=[r for r in RESOLUTIONS if res is None or r.replace('x', '*') in res],
            fps=[f for f in FRAME_RATES if (fps is None or str(f) in fps) and (fps_in is None or f <= fps_in)],
            codec=['H264'],
            profile=[PROFILES[p] for p in PROFILES if profiles is None or p in profiles],
            rate_control=[r for r in RATE_CONTROLS if rc is None or r in rc],
            max_bitrate_kbps=[r for r in MAX_BITRATES_KBPS if rates is None or r in rates],
            gop=dict(min=gop_min, max=gop_max),
            audio_source=_list(info, 'audioSourceList'),
            audio_bitrate=list(AUDIO_BITRATES),
            audio_volume=dict(min=VOLUME_RANGE[0], max=VOLUME_RANGE[1]))

    @staticmethod
    def _settings(video, audio, items):
        codec = {'H.264': 'H264', 'H.265': 'H265'}.get(video.get('sOutputDataType'))
        res = video.get('sResolution')
        return dict(
            video_source=items.get('videoSource', {}).get('value'),
            resolution=res.replace('*', 'x') if isinstance(res, str) else None,
            fps=_int(video.get('sFrameRate')),
            codec=codec,
            profile=PROFILES.get(video.get('sH264Profile')) if codec == 'H264' else None,
            rate_control=video.get('sRCMode') if video.get('sRCMode') in RATE_CONTROLS else None,
            max_bitrate_kbps=_int(video.get('iMaxRate')),
            gop=_int(video.get('iGOP')),
            audio_source=items.get('audioSource', {}).get('value'),
            audio_bitrate=_int(audio.get('iBitRate')),
            audio_volume=_int(audio.get('iVolume')))

    def _snapshot(self):
        video, audio, items, status, table = self._raw()
        settings = self._settings(video, audio, items)
        signal, shown = classify(status.get('inputVideoInfo'))
        info = {k: v.get('value') for k, v in items.items()}
        public = dict(model=info.get('model'), firmware=info.get('firmwareVersion'), host=self.host,
                      input=dict(signal=signal, info=shown, fps_in=_int(video.get('sFrameRateIn'))),
                      video_enabled=video.get('sVideoSwitch') == 'open',
                      settings=settings, options=self._options(video, items, table), revision=revision(settings),
                      applies_to='SE2017 main stream; the SDI stream reconnects for a few seconds after a change')
        return public, (video, audio, items)

    def read(self):
        with self.lock:
            return self._snapshot()[0]

    # ---- write -----------------------------------------------------------------------------
    @staticmethod
    def validate(change, options):
        if not isinstance(change, dict) or not change:
            raise EncoderError('INVALID_SDI_SETTINGS', 422, ['settings must be a non-empty object'])
        errors = ['%s: unknown field' % k for k in change if k not in FIELDS]
        for key, value in change.items():
            if key not in FIELDS: continue
            allowed = options[key]
            if isinstance(allowed, dict):
                if type(value) is not int or not allowed['min'] <= value <= allowed['max']:
                    errors.append('%s: integer %d..%d' % (key, allowed['min'], allowed['max']))
            elif type(value) is bool or value not in allowed:
                errors.append('%s: one of %s' % (key, json.dumps(allowed)))
        if errors: raise EncoderError('INVALID_SDI_SETTINGS', 422, errors)

    def update(self, expected_revision, change):
        with self.lock:
            public, (video, audio, items) = self._snapshot()
            if expected_revision != public['revision']:
                raise EncoderError('REVISION_CONFLICT', 409, ['settings changed; read them again'])
            self.validate(change, public['options'])
            desired = dict(public['settings'], **change)
            current = public['settings']
            if desired == current: return dict(public, changed=[])
            changed = sorted(k for k in change if change[k] != current[k])
            video_keys = {'resolution', 'fps', 'codec', 'profile', 'rate_control', 'max_bitrate_kbps', 'gop'}
            if video_keys & set(changed):
                body = {k: v for k, v in video.items() if k not in VIDEO_DROP}
                body.update(sResolution=desired['resolution'].replace('x', '*'), sFrameRate=str(desired['fps']),
                            sOutputDataType='H.264', sRCMode=desired['rate_control'],
                            iMaxRate=desired['max_bitrate_kbps'], iGOP=desired['gop'],
                            sH264Profile={v: k for k, v in PROFILES.items()}[desired['profile'] or 'high'])
                self._request('PUT', '/video/' + str(video.get('id', 0)), body)
            if 'video_source' in changed:
                self._request('PUT', '/system/device-info', dict(items['videoSource'], value=desired['video_source']))
            if {'audio_bitrate', 'audio_volume'} & set(changed):
                body = {k: v for k, v in audio.items() if k not in AUDIO_DROP}
                body.update(iBitRate=desired['audio_bitrate'], iVolume=desired['audio_volume'])
                self._request('PUT', '/audio/0', body)
            if 'audio_source' in changed:
                self._request('PUT', '/system/device-info', dict(items['audioSource'], value=desired['audio_source']))
            after = self._snapshot()[0]
            wrong = [k for k in change if after['settings'][k] != change[k]]
            if wrong:
                raise EncoderError('SDI_ENCODER_APPLY_MISMATCH', 502, ['%s not applied' % k for k in wrong])
            return dict(after, changed=changed)


class Provider:
    """Creates the client from the current SDI URL and credentials file on each use."""
    def __init__(self, credentials_path, config_source):
        self.path, self.config_source = credentials_path, config_source
        self.client, self.key = None, None
        self.lock = threading.Lock()

    def get(self):
        from urllib.parse import urlsplit
        from .sdi import load_encoder
        url = self.config_source()['sdi']['rtsp_url']
        if url is None: raise EncoderError('SDI_ENCODER_NOT_CONFIGURED')
        try: credentials = load_encoder(self.path)
        except (OSError, ValueError): raise EncoderError('SDI_ENCODER_CREDENTIALS_INVALID')
        if credentials is None: raise EncoderError('SDI_ENCODER_NOT_CONFIGURED')
        host = urlsplit(url).hostname
        key = (host, credentials['username'], credentials['password'])
        with self.lock:
            if self.key != key: self.client, self.key = EncoderClient(host, credentials), key
            return self.client
