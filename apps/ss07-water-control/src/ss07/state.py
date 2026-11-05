"""API-side aggregation; this process never owns a media device or a serial port."""
import copy
import math
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .config import ConfigError, load, revision
from .probe import read
from .snapshots import DIRECTIONS, SERVICES, SnapshotReader, u64
from .storage import inspect_storage
from .volume import MountLease


def storage_status(config, worker):
    result = dict(inspect_storage(config))
    result.update(state=worker['state'], recording_active=False, channels_recording=[],
                  observer_writable=result['writable'], recording_volume_verified=False)
    details = worker.get('payload', {})
    if worker['state'] in ('RUNNING', 'ERROR', 'UNAVAILABLE'):
        reason = details.get('reason')
        result['reason'] = reason if isinstance(reason, str) and len(reason) <= 256 else None
        result['recorder_gate'] = details.get('gate') if details.get('gate') in ('RECORDING', 'DISABLED', 'BLOCKED') else 'UNKNOWN'
    if (worker['state'] == 'RUNNING' and config.get('recording_enabled') is True
            and details.get('volume_uuid') == config.get('filesystem_uuid')
            and details.get('gate') == 'RECORDING' and details.get('recording_enabled') is True):
        channels = details.get('channels_recording')
        if (not isinstance(channels, list) or len(channels) > 6
                or any(ch not in DIRECTIONS + ('sdi',) for ch in channels)
                or len(set(channels)) != len(channels)
                or type(details.get('recording_active')) is not bool
                or details['recording_active'] != bool(channels)):
            result.update(state='ERROR', reason='invalid_storage_payload')
            return result
        try:
            # The HTTP service deliberately has read-only access. Its own access
            # check cannot decide whether the separate recorder can write.
            with MountLease.acquire(config) as volume:
                free, capacity = volume.space()
                result.update(gate='ELIGIBLE', writable=True, recording_volume_verified=True,
                              free_bytes=str(free), capacity_bytes=str(capacity), recording_enabled=True,
                              recording_active=bool(channels), channels_recording=channels)
        except (OSError, ValueError):
            result.update(gate='VOLUME_UNVERIFIED', reason='recording_volume_not_verified')
    return result


def empty_camera(channel):
    return dict(channel_id=channel, capture_state='NOT_IMPLEMENTED',
                publish_state='NOT_IMPLEMENTED', availability='unavailable',
                format=None, width=None, height=None, fps_observed=None,
                bitrate_observed=None, last_frame_age_ms=None,
                restart_count=None, live_url=None, native_url=None, rtsp_url=None, error='worker_not_implemented')


def native_access(worker):
    if worker['state'] != 'RUNNING': return dict(state='UNAVAILABLE', base_url=None)
    value = worker.get('payload', {}).get('native_rtsp')
    if not isinstance(value, dict): return dict(state='UNAVAILABLE', base_url=None)
    if value.get('enabled') is False: return dict(state='DISABLED', base_url=None)
    if value.get('state') != 'READY': return dict(state='UNAVAILABLE', base_url=None)
    try:
        from .auth import public_origin
        base = value['base_url']
        if not isinstance(base, str) or not base.startswith('rtsps://'): raise ValueError('native URL')
        if public_origin('https://' + base[8:]) != 'https://' + base[8:]: raise ValueError('native URL')
    except (KeyError, ValueError): return dict(state='UNAVAILABLE', base_url=None)
    plain = value.get('plain_base_url')
    if plain is not None:
        try:
            from .auth import public_origin
            if (not isinstance(plain, str) or not plain.startswith('rtsp://')
                    or public_origin('https://' + plain[7:]) != 'https://' + plain[7:]): raise ValueError('plain URL')
        except ValueError: plain = None
    return dict(state='READY', base_url=base, plain_base_url=plain, transport='tcp', authentication='account_required')


def camera_status(channel, worker, now_us):
    result = empty_camera(channel)
    if worker['state'] == 'NOT_IMPLEMENTED':
        return result
    result.update(capture_state=worker['state'], publish_state='UNAVAILABLE',
                  error=worker['reason'] or 'no_media_observation')
    if worker['state'] != 'RUNNING':
        reason = worker.get('payload', {}).get('reason')
        if isinstance(reason, str) and len(reason) <= 256:
            result['error'] = reason
        return result
    payload = worker.get('payload', {})
    required = {'capture_state', 'publish_state', 'format', 'width', 'height',
                'fps_observed', 'bitrate_observed', 'last_frame_monotonic_us',
                'restart_count', 'error'}
    try:
        if set(payload) != required:
            raise ValueError('camera payload fields')
        if payload['capture_state'] not in ('ABSENT', 'OPENING', 'STREAMING', 'RECOVERING', 'CONFIG_ERROR'):
            raise ValueError('capture state')
        if payload['publish_state'] not in ('UNAVAILABLE', 'CONNECTING', 'PUBLISHING', 'ERROR'):
            raise ValueError('publish state')
        for key in ('width', 'height', 'restart_count'):
            if payload[key] is not None and (type(payload[key]) is not int or not 0 <= payload[key] <= 2**32 - 1):
                raise ValueError('camera integer')
        for key in ('fps_observed', 'bitrate_observed'):
            value = payload[key]
            if value is not None and (type(value) not in (int, float) or not 0 <= value <= 2**64 - 1 or not math.isfinite(value)):
                raise ValueError('camera measurement')
        if payload['format'] not in (None, 'H264', 'HEVC', 'MJPG', 'YUYV', 'NV12'):
            raise ValueError('camera format')
        if payload['error'] is not None and (not isinstance(payload['error'], str) or len(payload['error']) > 256):
            raise ValueError('camera error')
        observed = payload['last_frame_monotonic_us']
        age = None if observed is None else (now_us - u64(observed)) // 1000
        if age is not None and age < 0:
            raise ValueError('future frame')
        if payload['capture_state'] == 'STREAMING' and age is None:
            raise ValueError('streaming requires a real frame timestamp')
        result.update({key: value for key, value in payload.items() if key != 'last_frame_monotonic_us'})
        result['last_frame_age_ms'] = age
        if age is not None and age >= 3000:
            result.update(capture_state='STALE', error='frame_timeout')
        elif payload['capture_state'] == 'STREAMING' and payload['publish_state'] == 'PUBLISHING':
            result['availability'] = 'available'
    except (ValueError, TypeError, ConfigError):
        result = empty_camera(channel)
        result.update(capture_state='ERROR', error='invalid_camera_payload')
    return result


def host_health():
    memory = {}
    for line in (read('/proc/meminfo') or '').splitlines():
        parts = line.split()
        if parts and parts[0] in ('MemTotal:', 'MemAvailable:'):
            memory[parts[0][:-1]] = str(int(parts[1]) * 1024)
    temperatures = []
    for path in sorted(Path('/sys/class/thermal').glob('thermal_zone*/temp'))[:16]:
        value = read(path)
        try:
            temperatures.append({'sensor': path.parent.name, 'celsius': int(value) / 1000})
        except (ValueError, TypeError):
            pass
    return {'memory_bytes': memory, 'temperatures': temperatures}


def attach_media(streams, worker, now_us):
    """Require both a fresh worker and advancing media counters for live URLs."""
    for stream in streams:
        stream.update(availability='unavailable', live_url=None)
        if stream['publish_state'] == 'PUBLISHING':
            stream['publish_state'] = 'UNAVAILABLE'
    if worker['state'] != 'RUNNING':
        return
    payload = worker.get('payload', {}).get('streams', {})
    if not isinstance(payload, dict):
        return
    for stream in streams:
        channel = stream['channel_id']
        item = payload.get(channel, {})
        try:
            last = u64(item.get('last_data_monotonic_us'))
            ready = item.get('ready') is True and item.get('codec') == 'H264' and 0 <= now_us - last < 3000000
        except (ConfigError, TypeError, AttributeError):
            ready = False
        if not ready:
            stream.update(availability='unavailable', publish_state='UNAVAILABLE', live_url=None)
            continue
        if channel == 'sdi':
            # Receive age is media ingress activity, not a decoded image/exposure timestamp.
            stream.update(capture_state='STREAMING', format='H264',
                          last_frame_age_ms=None, media_activity_age_ms=(now_us - last) // 1000,
                          timestamp_basis='media_ingress_activity', error=None)
        if stream['capture_state'] == 'STREAMING':
            stream.update(availability='available', publish_state='PUBLISHING',
                          live_url='/media/' + channel + '/whep')


class State:
    def __init__(self, config, state_dir=None, config_path=None, catalog_path=None, timecode_journal_path=None, active_path=None, sonar_journal_path=None, sonar_catalog_path=None):
        self.config = copy.deepcopy(config)
        self.config_path = config_path
        self.active_path = active_path
        from .catalog import Catalog
        self.catalog = Catalog(catalog_path) if catalog_path else None
        self.boot_id = read('/proc/sys/kernel/random/boot_id')
        from .timecode_ipc import Client
        from .timecode_journal import Journal
        self.timecode_client = Client(state_dir, self.boot_id)
        self.timecode_journal = Journal(timecode_journal_path) if timecode_journal_path else None
        from .sonar_journal import Journal as SonarJournal
        self.sonar_journal = SonarJournal(sonar_journal_path) if sonar_journal_path else None
        from .sonar_archive import Catalog as SonarCatalog
        self.sonar_catalog=SonarCatalog(sonar_catalog_path) if sonar_catalog_path else None
        self.instance_id = str(uuid.uuid4())
        self.started = time.monotonic()
        self.seq = 0
        self.lock = threading.Lock()
        self.workers = SnapshotReader(state_dir, self.boot_id)

    def configuration(self):
        persisted, error = self.config, None
        if self.config_path:
            try:
                persisted = load(self.config_path)
            except (OSError, ConfigError, RuntimeError):
                persisted, error = None, 'persisted_config_unreadable_or_invalid'
        disk_revision = revision(persisted) if persisted is not None else None
        loaded_revision = revision(self.config)
        active = self.config
        if self.active_path:
            try: active = load(self.active_path)
            except (OSError, ConfigError, RuntimeError): active, error = None, 'active_config_unreadable_or_invalid'
        active_revision = revision(active) if active is not None else None
        from .config_runtime import ORDER, acknowledgement
        consumers = {}
        for service in ORDER:
            observed = self.workers.read(service)
            ok, reason = acknowledgement(active, service, observed) if active is not None else (False, 'active_config_unavailable')
            consumers[service] = dict(state='APPLIED' if ok else 'UNVERIFIED', reason=reason,
                                      service_revision=observed.get('service_revision'), worker_state=observed['state'])
        coordinator = self.workers.read('configuration')
        return dict(persisted=persisted, revision=disk_revision, loaded_revision=loaded_revision,
                    active_revision=active_revision, pending_apply=disk_revision != active_revision,
                    runtime={'api': self.config['api']}, mutable=False,
                    pending_restart=disk_revision != loaded_revision,
                    error=error, apply_result={'services': consumers, 'hardware_verified': False},
                    application={'state': coordinator['state'], 'status': coordinator.get('payload')})

    def snapshot(self):
        with self.lock:
            self.seq += 1
            seq = str(self.seq)
        c = self.config
        workers = {name: self.workers.read(name) for name in SERVICES}
        now_us = time.monotonic_ns() // 1000
        streams = [camera_status(d, workers['camera.' + d], now_us) for d in DIRECTIONS]
        streams.append(empty_camera('sdi'))
        attach_media(streams, workers['media'], now_us)
        native = native_access(workers['media'])
        for stream in streams:
            if native['state'] == 'READY' and stream['availability'] == 'available':
                stream['native_url'] = native['base_url'] + '/' + stream['channel_id']
                if native.get('plain_base_url'): stream['rtsp_url'] = native['plain_base_url'] + '/' + stream['channel_id']
        from .sonar_status import inspect as inspect_sonar
        sonar = inspect_sonar(c, workers['sonar'], now_us, self.boot_id)
        sonars = sonar['samples']
        from .timecode_status import inspect as inspect_timecode
        tc = inspect_timecode(self.timecode_client, workers['timecode'])
        storage_worker = workers['storage']
        storage = storage_status(c['storage'], storage_worker)
        alerts = ['HARDWARE_INTEGRATION_INCOMPLETE']
        if workers['media'].get('payload', {}).get('native_rtsp', {}).get('enabled') is True and native['state'] != 'READY':
            alerts.append('NATIVE_RTSP_UNAVAILABLE')
        if storage['gate'] != 'ELIGIBLE':
            alerts.append('RECORDING_' + storage['gate'])
        journal = tc.get('journal')
        if isinstance(journal, dict) and (journal.get('error') or journal.get('dropped_events') != '0'):
            alerts.append('TIMECODE_HISTORY_GAP')
        journal = sonar['journal']
        if isinstance(journal, dict) and (journal.get('error') or journal.get('dropped_events') != '0'):
            alerts.append('SONAR_HISTORY_GAP')
        archive=sonar['archive']
        if archive['enabled'] and archive['state']!='RECORDING':alerts.append('SONAR_ARCHIVE_UNAVAILABLE')
        if archive['dropped_events']!='0' or archive['recovery_failures']:alerts.append('SONAR_ARCHIVE_GAP')
        camera_states = {s['capture_state'] for s in streams[:-1]}
        camera_service = ('NOT_IMPLEMENTED' if camera_states == {'NOT_IMPLEMENTED'}
                          else 'RUNNING' if camera_states == {'STREAMING'} else 'DEGRADED')
        return dict(schema_version=1, app_version=__version__, device_id=c['device_id'],
                    boot_id=self.boot_id, service_instance_id=self.instance_id, seq=seq,
                    observed_at_utc=datetime.now(timezone.utc).isoformat(),
                    local_monotonic_us=str(now_us),
                    uptime_ms=int((time.monotonic() - self.started) * 1000),
                    evidence_kind='service_observation', phase=3, streams=streams, sonars=sonars,
                    timecode=tc, storage=storage, system=host_health(), native_rtsp=native,
                    sonar_service={k: v for k, v in sonar.items() if k != 'samples'},
                    service_states={'api': 'RUNNING', 'camera': camera_service,
                                    'media': workers['media']['state'], 'sonar': sonar['state'],
                                    'timecode': workers['timecode']['state'], 'storage': storage_worker['state']},
                    workers={name: {k: v for k, v in value.items() if k != 'payload'}
                             for name, value in workers.items()}, alerts=alerts)

    def data(self, path):
        if path == '/api/v1/config':
            return self.configuration()
        if path == '/healthz':
            return {'api': 'RUNNING', 'app_version': __version__, 'hardware_ready': False}
        if path not in ('/api/v1/status', '/api/v1/streams', '/api/v1/sonars', '/api/v1/timecode'):
            return None
        status = self.snapshot()
        if path == '/api/v1/status':
            return status
        key = path[len('/api/v1/'):]
        return {**{k: status[k] for k in ('schema_version', 'device_id', 'boot_id',
                                        'service_instance_id', 'seq', 'observed_at_utc')},
                key: status[key]}
