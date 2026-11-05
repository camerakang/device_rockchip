"""Pinned MediaMTX configuration and observations. No video crosses Python."""
import json
import os
import selectors
import subprocess
import tempfile
import time
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

from .config import ConfigError, decode_json
from .snapshots import DIRECTIONS
from .worker import Worker, monotonic_us, terminate

VERSION = 'v1.21.2'
DEFAULTS = dict(rtsp_port=8554, webrtc_port=8889, ice_port=8189, api_port=9997,
                rtp_base_port=19000)
CHANNELS = DIRECTIONS + ('sdi',)


def settings(config):
    return dict(DEFAULTS, **config.get('media', {}))


def rtp_port(config, direction):
    return settings(config)['rtp_base_port'] + DIRECTIONS.index(direction) * 2


def render(config, gateway=None):
    """JSON is YAML-compatible; avoids a YAML dependency and string interpolation."""
    ports = settings(config)
    paths = {}
    for direction in DIRECTIONS:
        port = rtp_port(config, direction)
        paths[direction] = {
            'source': 'udp+rtp://127.0.0.1:' + str(port),
            'rtpSDP': ('v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\ns=SS07\r\n'
                       'c=IN IP4 127.0.0.1\r\nt=0 0\r\nm=video %d RTP/AVP 96\r\n'
                       'a=rtpmap:96 H264/90000\r\na=fmtp:96 packetization-mode=1\r\n') % port,
        }
    paths['sdi'] = {'source': config['sdi']['rtsp_url'] or 'publisher',
                    'rtspTransport': 'tcp'}
    return {
        'logLevel': 'warn', 'logDestinations': ['stdout'],
        'readTimeout': '3s', 'writeTimeout': '3s', 'writeQueueSize': 256,
        'udpMaxPayloadSize': 1200, 'udpReadBufferSize': 0,
        'authMethod': 'internal',
        'authInternalUsers': [{'user': 'any', 'pass': '', 'ips': ['127.0.0.1'],
                               'permissions': [{'action': 'read', 'path': ''},
                                               {'action': 'api', 'path': ''}]}],
        'api': True, 'apiAddress': '127.0.0.1:' + str(ports['api_port']),
        'metrics': False, 'pprof': False, 'playback': False,
        'rtsp': True, 'rtspAddress': '127.0.0.1:' + str(ports['rtsp_port']),
        'rtspTransports': ['tcp'], 'rtmp': False, 'hls': False, 'srt': False,
        'webrtc': True, 'webrtcAddress': '127.0.0.1:' + str(ports['webrtc_port']),
        'webrtcAllowOrigins': [gateway['public_origin'] if gateway else 'http://127.0.0.1:' + str(config['api']['port'])],
        'webrtcLocalUDPAddress': (gateway['ice_ip'] if gateway else '127.0.0.1') + ':' + str(ports['ice_port']),
        'webrtcLocalTCPAddress': '', 'webrtcIPsFromInterfaces': False,
        'webrtcAdditionalHosts': [gateway['ice_ip'] if gateway else '127.0.0.1'], 'webrtcICEServers2': [],
        'moq': False,
        'pathDefaults': {'record': False, 'recordDeleteAfter': '0s',
                         'maxReaders': 12, 'sourceOnDemand': False},
        'paths': paths,
    }


def get_paths(config):
    # Loopback only, ignores HTTP_PROXY; no user-supplied URL or redirects.
    url = 'http://127.0.0.1:%d/v3/paths/list?itemsPerPage=16' % settings(config)['api_port']
    with build_opener(ProxyHandler({})).open(url, timeout=0.7) as response:
        if response.geturl() != url:
            raise ValueError('unexpected media API redirect')
        result = decode_json(response.read(262145), max_bytes=262144)
    if not isinstance(result, dict) or not isinstance(result.get('items'), list) or len(result['items']) > 16:
        raise ValueError('invalid media API response')
    items = {}
    for item in result['items']:
        if not isinstance(item, dict) or item.get('name') not in CHANNELS:
            continue
        name = item['name']
        if name in items or type(item.get('ready')) is not bool:
            raise ValueError('invalid media path')
        size, tracks = item.get('bytesReceived'), item.get('tracks') or []
        if type(size) is not int or size < 0 or not isinstance(tracks, list):
            raise ValueError('invalid media counters')
        items[name] = {'ready': item['ready'], 'bytes_received': str(size),
                       'codec': 'H264' if 'H264' in tracks else None,
                       'ready_time': item.get('readyTime')}
    return items


class MediaObserver:
    def __init__(self):
        self.previous = {}

    def observe(self, items, now=None):
        now = monotonic_us() if now is None else now
        streams = {}
        for channel in CHANNELS:
            item = items.get(channel)
            previous = self.previous.get(channel)
            last = None
            if item and item['ready']:
                counter = int(item['bytes_received'])
                identity = item['ready_time']
                if previous and previous[0] == identity and counter > previous[1]:
                    last = now
                elif previous and previous[0] == identity and counter == previous[1]:
                    last = previous[2]
                # The first ready response does not prove bytes are still arriving.
                self.previous[channel] = (identity, counter, last)
            else:
                self.previous.pop(channel, None)
            live = bool(item and item['ready'] and item['codec'] == 'H264'
                        and last is not None and 0 <= now - last < 3000000)
            streams[channel] = dict(ready=live, codec=item['codec'] if item else None,
                                    last_data_monotonic_us=str(last) if last is not None else None,
                                    bytes_received=item['bytes_received'] if item else '0',
                                    reason=None if live else 'media_not_receiving_h264')
        return {'version': VERSION, 'streams': streams, 'recording_enabled': False}


def run(config, state_dir, binary, stop, gateway=None, rtsp_profile=None, accounts=None, rtsp_audit=None, api_clients=None):
    check = subprocess.run([str(binary), '--version'], stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, timeout=5, check=True)
    if check.stdout.decode().strip() != VERSION:
        raise ConfigError('MediaMTX version must be ' + VERSION)
    with Worker(state_dir, 'media', stop, config=config) as worker:
        delay = 1
        while not stop.is_set():
            with tempfile.TemporaryDirectory(prefix='ss07-media-') as tmp:
                from .rtsp_access import DEFAULT, Relay
                relay = Relay(config, gateway, rtsp_profile or dict(DEFAULT), accounts, rtsp_audit, binary, tmp, api_clients)
                path = os.path.join(tmp, 'mediamtx.json')
                with open(path, 'w') as out:
                    json.dump(render(config, gateway), out)
                worker.emit('STARTING', {'reason': 'media_starting'})
                proc = subprocess.Popen([str(binary), path], stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, start_new_session=True)
                observer = MediaObserver()
                started, next_poll = time.monotonic(), 0.0
                try:
                    with selectors.DefaultSelector() as selector:
                        selector.register(proc.stdout, selectors.EVENT_READ)
                        while not stop.is_set() and proc.poll() is None:
                            relay.tick()
                            for key, _ in selector.select(timeout=0.1):
                                # Drain bounded chunks; do not log remote URLs or credentials.
                                os.read(key.fd, 4096)
                            if time.monotonic() >= next_poll:
                                try:
                                    payload = observer.observe(get_paths(config))
                                    payload['native_rtsp'] = relay.snapshot()
                                    worker.emit('RUNNING', payload)
                                except (OSError, ValueError, URLError):
                                    observer = MediaObserver()
                                    worker.emit('UNAVAILABLE', {'reason': 'media_api_unavailable'})
                                next_poll = time.monotonic() + 1
                    if time.monotonic() - started > 60:
                        delay = 1
                finally:
                    try: relay.close()
                    finally: terminate(proc)
            if not stop.is_set():
                worker.emit('ERROR', {'reason': 'media_process_exited'})
                stop.wait(delay)
                delay = min(delay * 2, 30)
