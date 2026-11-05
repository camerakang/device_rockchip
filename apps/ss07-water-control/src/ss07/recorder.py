"""Independent compressed-stream recorder with a pinned volume and durable catalog."""
import json
import os
import re
import selectors
import shlex
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from decimal import Decimal, ROUND_HALF_UP
from collections import deque
from pathlib import Path

from .catalog import CHANNELS, RELATIVE, Catalog, CatalogError, RecordStore, recording_id, utc
from .config import ConfigError, ROOT, decode_json
from .media import VERSION, settings as media_settings
from .snapshots import u64
from .volume import MountLease, VolumeError
from .worker import Worker, monotonic_us, terminate
from .timecode_ipc import Client as TimecodeClient

DEFAULTS = dict(recording_enabled=False, filesystem_uuid=None, segment_seconds=300,
                reserve_bytes='50000000000', retention_days=None)


def settings(config):
    return dict(DEFAULTS, **config['storage'])


def duration_ms(text):
    # MediaMTX v1.21.2 formats float64 Seconds() with 'f', -1. Binary
    # rounding can produce more than nine decimal places (e.g. 1.14...).
    if not isinstance(text, str) or not re.fullmatch(r'[0-9]{1,6}(?:\.[0-9]{1,18})?', text):
        raise CatalogError('invalid_record_duration')
    seconds = Decimal(text)
    if not Decimal('0.001') <= seconds <= Decimal('86400'):
        raise CatalogError('invalid_record_duration')
    return int((seconds * 1000).to_integral_value(rounding=ROUND_HALF_UP))


def render(config, fd, session_id):
    if type(fd) is not int or fd < 0 or not re.fullmatch(r'[a-f0-9]{32}', session_id):
        raise ValueError('invalid recording session')
    config_storage = settings(config)
    hook = shlex.quote(sys.executable) + ' ' + shlex.quote(str(ROOT / 'tools/ss07-segment-hook.py'))
    return dict(logLevel='warn', logDestinations=['stdout'],
        api=False, metrics=False, pprof=False, playback=False,
        rtsp=False, rtmp=False, hls=False, webrtc=False, srt=False, moq=False,
        readTimeout='3s', writeTimeout='3s', writeQueueSize=256,
        pathDefaults=dict(record=True,
            recordPath='/proc/self/fd/%d/%s/%%path/%%Y-%%m-%%d_%%H-%%M-%%S-%%f' % (fd, session_id),
            recordFormat='fmp4', recordPartDuration='1s', recordMaxPartSize='16M',
            recordSegmentDuration=str(config_storage['segment_seconds']) + 's', recordDeleteAfter='0s',
            sourceOnDemand=False, rtspTransport='tcp',
            runOnRecordSegmentCreate=hook + ' create', runOnRecordSegmentComplete=hook + ' complete'),
        paths={channel: {'source': 'rtsp://127.0.0.1:%d/%s' % (media_settings(config)['rtsp_port'], channel)}
               for channel in CHANNELS})


def verify_binary(binary):
    try:
        result = subprocess.run([str(binary), '--version'], stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, check=True, timeout=5)
    except subprocess.SubprocessError as exc:
        raise ConfigError('MediaMTX version check failed') from exc
    if result.stdout.decode().strip() != VERSION:
        raise ConfigError('MediaMTX version must be ' + VERSION)


class RecorderSession:
    """One recorder subprocess. The supplied volume determines evidence provenance."""
    def __init__(self, config, catalog, volume, binary, boot_id, timecode_client=None):
        self.config, self.catalog, self.volume = config, catalog, volume
        self.binary, self.boot_id = str(binary), boot_id
        self.session = dict(schema_version=1, session_id=uuid.uuid4().hex, boot_id=boot_id,
                            volume_uuid=volume.volume_uuid, evidence_kind=volume.evidence_kind,
                            created_at_utc=utc(), created_monotonic_us=str(monotonic_us()))
        self.store = RecordStore(catalog, volume)
        self.pending = {}
        self.pending_tags = {}
        self.timecode = timecode_client or TimecodeClient(None, boot_id, volume.evidence_kind)
        self.completed = 0
        self.last_completed_us = None
        self.finished = deque(maxlen=128)
        self.progress = {}
        self.recent_logs = deque(maxlen=8)
        self.process = self.socket = self.selector = self.temporary = None
        self.prefix = '/proc/self/fd/%d/' % volume.fd

    def __enter__(self):
        name = self.session['session_id']
        for channel in CHANNELS:
            with self.volume.directory(name + '/' + channel, create=True):
                pass
        self.volume.write_json(name + '/session.json', self.session)
        self.temporary = tempfile.TemporaryDirectory(prefix='ss07-record-')
        directory = Path(self.temporary.name)
        self.socket_path = directory / 'events.sock'
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.socket.bind(str(self.socket_path))
        self.socket.setblocking(False)
        try:
            path = directory / 'recorder.json'
            path.write_text(json.dumps(render(self.config, self.volume.fd, name)))
            env = dict(os.environ, SS07_RECORD_SOCKET=str(self.socket_path), SS07_RECORD_SESSION=name,
                       TZ='UTC', PYTHONDONTWRITEBYTECODE='1')
            self.process = subprocess.Popen([sys.executable, str(ROOT / 'tools/ss07-record-child.py'),
                str(os.getpid()), self.binary, str(path)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                pass_fds=(self.volume.fd,), start_new_session=True, env=env)
            self.selector = selectors.DefaultSelector()
            self.selector.register(self.socket, selectors.EVENT_READ, 'events')
            self.selector.register(self.process.stdout, selectors.EVENT_READ, 'logs')
            return self
        except Exception:
            self.__exit__(None, None, None)
            raise

    def _event(self, raw):
        data = decode_json(raw, max_bytes=2048)
        keys = {'schema_version', 'session_id', 'event', 'channel', 'path', 'duration', 'observed_monotonic_us'}
        if not isinstance(data, dict) or set(data) != keys:
            raise CatalogError('invalid_record_notification')
        if data['session_id'] != self.session['session_id']:
            return  # Late datagram from a different process lifetime.
        if type(data['schema_version']) is not int or data['schema_version'] != 1 or data['event'] not in ('create', 'complete'):
            raise CatalogError('invalid_record_notification')
        observed = u64(data['observed_monotonic_us'])
        if observed > monotonic_us() or observed < int(self.session['created_monotonic_us']):
            raise CatalogError('future_record_notification')
        path = data['path']
        if not isinstance(path, str) or not path.startswith(self.prefix):
            raise CatalogError('record_notification_path_outside_volume')
        relative = path[len(self.prefix):]
        match = RELATIVE.fullmatch(relative)
        if not match or match[1] != self.session['session_id'] or match[2] != data['channel']:
            raise CatalogError('record_notification_identity_mismatch')
        if data['event'] == 'create':
            if relative in self.finished:
                return
            try:
                self.catalog.get(recording_id(self.volume.volume_uuid, relative))
                return  # A delayed duplicate cannot resurrect an already indexed file.
            except KeyError:
                pass
            if len(self.pending) >= 128 and relative not in self.pending:
                raise CatalogError('record_notification_capacity')
            if relative not in self.pending:
                self.pending[relative] = data['observed_monotonic_us']
                self.pending_tags[relative] = self.timecode.tag(observed, 'segment_create_notification')
        else:
            if relative in self.finished:
                return
            milliseconds = duration_ms(data['duration'])
            tags = dict(start=self.pending_tags.get(relative) or self.timecode.tag(None, 'segment_create_notification'),
                        end=self.timecode.tag(observed, 'segment_complete_notification'))
            self.store.finalize(relative, self.session, milliseconds, self.pending.get(relative),
                                data['observed_monotonic_us'], tags)
            self.pending.pop(relative, None)
            self.pending_tags.pop(relative, None)
            self.finished.append(relative)
            self.completed += 1
            self.last_completed_us = data['observed_monotonic_us']

    def tick(self, timeout=0.1, stopping=False):
        if not stopping:
            self.volume.require_valid()
        if (Path(self.temporary.name) / 'hook-failure.json').exists():
            raise CatalogError('record_notification_lost')
        for key, _ in self.selector.select(timeout):
            if key.data == 'logs':
                raw = os.read(key.fd, 4096)
                if not raw:
                    self.selector.unregister(key.fileobj)
                else:
                    self.recent_logs.append(raw.decode('utf-8', errors='replace'))
            else:
                for _ in range(32):
                    try:
                        raw = self.socket.recv(2049)
                    except BlockingIOError:
                        break
                    self._event(raw)
        if not stopping and self.process.poll() is not None:
            raise CatalogError('record_process_exited')

    def status(self):
        # A file-open notification is not proof that compressed data is advancing.
        now = monotonic_us()
        active = set()
        for path in list(self.progress):
            if path not in self.pending:
                del self.progress[path]
        for path in self.pending:
            try:
                fd = self.volume.open(path)
                try:
                    size = os.fstat(fd).st_size
                finally:
                    os.close(fd)
                previous, advanced = self.progress.get(path, (0, None))
                if size > previous:
                    advanced = now
                self.progress[path] = (size, advanced)
                if advanced is not None and now - advanced < 3000000:
                    active.add(RELATIVE.fullmatch(path)[2])
            except OSError:
                self.progress.pop(path, None)
        return dict(recording_enabled=True, recording_active=bool(active), gate='RECORDING',
                    volume_uuid=self.volume.volume_uuid, session_id=self.session['session_id'],
                    channels_recording=sorted(active),
                    completed_segments=self.completed, last_completed_monotonic_us=self.last_completed_us,
                    free_bytes=str(self.volume.space()[0]), reason=None)

    def __exit__(self, *args):
        error = None
        if self.process:
            if self.process.poll() is None:
                self.process.terminate()
            deadline = time.monotonic() + 3
            try:
                while self.selector and time.monotonic() < deadline:
                    self.tick(timeout=0.05, stopping=True)
                    if self.process.poll() is not None:
                        # Hooks can already be queued when the child exits.
                        self.tick(timeout=0, stopping=True)
                        break
            except (OSError, ValueError) as exc:
                error = exc
            finally:
                terminate(self.process)
        if self.selector:
            self.selector.close()
        if self.socket:
            self.socket.close()
        if self.temporary:
            self.temporary.cleanup()
        if error is not None and args[0] is None:
            raise error


def run(config, state_dir, catalog_path, binary, stop):
    verify_binary(binary)
    catalog = Catalog(catalog_path)
    storage = settings(config)
    reserve = int(storage['reserve_bytes'])
    with Worker(state_dir, 'storage', stop, config=config) as worker:
        catalog.initialize()
        delay, last_error = 1, None
        while not stop.is_set():
            if not storage['recording_enabled']:
                worker.emit('UNAVAILABLE', dict(recording_enabled=False, recording_active=False,
                            gate='DISABLED', reason='recording_disabled'))
                stop.wait(1)
                continue
            try:
                with MountLease.acquire(storage, writable=True) as volume:
                    store = RecordStore(catalog, volume)
                    recovery = store.recover()
                    if recovery['rejected']:
                        catalog.event('RECOVERY_WARNING', 'metadata_or_container_rejected')
                    store.cleanup(reserve + 134217728, storage['retention_days'])
                    if volume.space()[0] <= reserve:
                        raise VolumeError('insufficient_recording_space')
                    with RecorderSession(config, catalog, volume, binary, worker.boot,
                                         TimecodeClient(state_dir, worker.boot)) as session:
                        next_check = 0.0
                        while not stop.is_set():
                            session.tick()
                            if time.monotonic() >= next_check:
                                store.cleanup(reserve + 134217728, storage['retention_days'])
                                if volume.space()[0] <= reserve:
                                    raise VolumeError('insufficient_recording_space')
                                worker.emit('RUNNING', session.status())
                                delay, last_error = 1, None
                                next_check = time.monotonic() + 1
                        delay = 1
            except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
                reason = str(exc) if isinstance(exc, (CatalogError, VolumeError, ConfigError)) else 'recording_io_or_catalog_error'
                reason = reason[:128]
                worker.emit('ERROR', dict(recording_enabled=False, recording_active=False, gate='BLOCKED', reason=reason))
                if reason != last_error:
                    try:
                        catalog.event('RECORDING_GAP', reason)
                    except (OSError, ValueError, sqlite3.Error):
                        pass
                    last_error = reason
                deadline = time.monotonic() + delay
                while not stop.is_set() and time.monotonic() < deadline:
                    worker.emit('ERROR', dict(recording_enabled=False, recording_active=False, gate='BLOCKED', reason=reason))
                    stop.wait(min(1, max(0, deadline - time.monotonic())))
                delay = min(delay * 2, 30)
