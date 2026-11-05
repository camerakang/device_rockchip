"""Camera supervisor: configuration, child lifecycle and 1 Hz metadata only."""
import math
import os
import re
import selectors
import subprocess
import time
from pathlib import Path

from .config import ConfigError, decode_json
from .media import rtp_port
from .snapshots import DIRECTIONS, SnapshotReader, u64
from .worker import Worker, monotonic_us, terminate


def command(config, channel, binary):
    if channel not in DIRECTIONS:
        raise ConfigError('unknown camera channel')
    camera = config['cameras'][channel]
    mode, binding = camera['mode'], camera['binding']
    if mode is None or binding is None:
        raise ConfigError('camera_unconfigured')
    if mode['fourcc'] != 'H264':
        raise ConfigError('only_native_h264_is_implemented')
    return [str(binary), '--device', binding, '--width', str(mode['width']),
            '--height', str(mode['height']), '--fps-num', str(mode['fps_num']),
            '--fps-den', str(mode['fps_den']), '--port', str(rtp_port(config, channel))]


def check_identity(binding):
    """Reject ambiguous serials at every open, even if enumeration changed."""
    target = Path(binding).resolve(strict=True)
    if target.parent != Path('/dev') or not target.name.startswith('video') or not target.is_char_device():
        raise ConfigError('binding_is_not_a_video_device')
    if '/by-id/' in binding:
        def serial(node):
            path = (Path('/sys/class/video4linux') / node / 'device').resolve()
            for parent in (path,) + tuple(path.parents):
                value = parent / 'serial'
                if value.is_file():
                    return parent, value.read_text()[:256].strip()
            return None, None
        parent, value = serial(target.name)
        if not value or set(value) == {'0'}:
            raise ConfigError('nonunique_serial_use_by_path')
        matches = {serial(p.name)[0] for p in Path('/sys/class/video4linux').glob('video*')
                   if serial(p.name)[1] == value}
        if matches != {parent}:
            raise ConfigError('duplicate_serial_use_by_path')


def telemetry(raw, previous=None, now=None):
    data = decode_json(raw, max_bytes=4096)
    fields = {'evidence_kind', 'observed_monotonic_us', 'last_frame_monotonic_us',
              'frames', 'bytes', 'fps_observed', 'bitrate_observed'}
    if not isinstance(data, dict) or set(data) != fields or data['evidence_kind'] != 'device_observation':
        raise ConfigError('invalid_native_telemetry')
    now = monotonic_us() if now is None else now
    observed = u64(data['observed_monotonic_us'])
    last = data['last_frame_monotonic_us']
    if observed > now or now - observed >= 3000000 or (last is not None and u64(last) > observed):
        raise ConfigError('invalid_native_timestamp')
    for key in ('frames', 'bytes'):
        value = u64(data[key])
        if previous and value < u64(previous[key]):
            raise ConfigError('native_counter_regressed')
    if previous and observed <= u64(previous['observed_monotonic_us']):
        raise ConfigError('native_observation_regressed')
    for key in ('fps_observed', 'bitrate_observed'):
        value = data[key]
        if type(value) not in (float, int) or not math.isfinite(value) or not 0 <= value <= 2**64 - 1:
            raise ConfigError('invalid_native_measurement')
    return data


def run(config, channel, state_dir, binary, stop):
    with Worker(state_dir, 'camera.' + channel, stop, config=config) as worker:
        try:
            argv = command(config, channel, binary)
        except ConfigError as exc:
            while not stop.is_set():
                worker.emit('UNAVAILABLE', {'reason': str(exc)})
                stop.wait(1)
            return
        media = SnapshotReader(state_dir, worker.boot)
        retries, delay = 0, 1
        while not stop.is_set():
            worker.emit('STARTING', {'reason': 'opening_camera'})
            started = time.monotonic()
            try:
                check_identity(config['cameras'][channel]['binding'])
                proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        start_new_session=True, bufsize=0)
                previous, pending, last_output = None, b'', monotonic_us()
                stderr, native_reason = b'', 'capture_process_exited'
                try:
                    with selectors.DefaultSelector() as selector:
                        selector.register(proc.stdout, selectors.EVENT_READ, 'stdout')
                        selector.register(proc.stderr, selectors.EVENT_READ, 'stderr')
                        while not stop.is_set() and proc.poll() is None:
                            for key, _ in selector.select(timeout=0.2):
                                chunk = os.read(key.fd, 4096)
                                if not chunk:
                                    selector.unregister(key.fileobj)
                                    continue
                                if key.data == 'stderr':
                                    stderr = (stderr + chunk)[-4096:]
                                    while b'\n' in stderr:
                                        line, stderr = stderr.split(b'\n', 1)
                                        if re.fullmatch(b'[a-z][a-z0-9_]{1,100}', line):
                                            native_reason = line.decode('ascii')
                                    continue
                                pending += chunk
                                if len(pending) > 8192:
                                    raise ConfigError('native_telemetry_too_large')
                                while b'\n' in pending:
                                    line, pending = pending.split(b'\n', 1)
                                    previous = telemetry(line, previous)
                                    last_output = monotonic_us()
                                    frame = previous['last_frame_monotonic_us']
                                    fresh = frame is not None and last_output - u64(frame) < 3000000
                                    current = media.read('media')
                                    sources = current.get('payload', {}).get('streams', {})
                                    source = sources.get(channel, {}) if isinstance(sources, dict) else {}
                                    try:
                                        published = (current['state'] == 'RUNNING' and isinstance(source, dict)
                                            and source.get('ready') is True and source.get('codec') == 'H264'
                                            and 0 <= last_output - u64(source.get('last_data_monotonic_us')) < 3000000)
                                    except ConfigError:
                                        published = False
                                    mode = config['cameras'][channel]['mode']
                                    worker.emit('RUNNING', dict(capture_state='STREAMING' if fresh else 'OPENING',
                                        publish_state='PUBLISHING' if fresh and published else 'CONNECTING',
                                        format='H264', width=mode['width'], height=mode['height'],
                                        fps_observed=previous['fps_observed'], bitrate_observed=previous['bitrate_observed'],
                                        last_frame_monotonic_us=frame, restart_count=retries,
                                        error=None if fresh and published else 'waiting_for_media'))
                            if monotonic_us() - last_output >= 3500000:
                                raise ConfigError('native_telemetry_timeout')
                finally:
                    terminate(proc)
                reason = native_reason
            except (OSError, ConfigError) as exc:
                reason = str(exc) if isinstance(exc, ConfigError) else 'camera_device_unavailable'
            if not stop.is_set():
                worker.emit('ERROR', {'reason': reason, 'restart_count': retries})
                if time.monotonic() - started > 60:
                    delay = 1
                stop.wait(delay)
                retries += 1
                delay = min(delay * 2, 30)
