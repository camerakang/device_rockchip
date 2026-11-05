"""Fail closed on stale, synthetic, mismatched or unreviewed sonar observations."""
from dataclasses import asdict

from .config import ConfigError
from .config_runtime import fingerprint
from .snapshots import DIRECTIONS, u64
from .sonar_core import Poller, REASON, validate_event
from .sonar_driver import resolve
from .sonar_archive_worker import disabled_status


def empty(config, reason):
    return [dict(sensor_id=d, direction=d, address=config['sonar']['nodes'][d]['address'],
                 seq=None, sample_id=None, distance_mm=None, valid=False, reason=reason, quality=None,
                 age_ms=None, received_monotonic_us=None, timestamp_basis='sonar_receive', measurement_time_us=None,
                 evidence_kind='device_observation') for d in DIRECTIONS]


def history_status(value):
    if not isinstance(value, dict): return None
    try:
        if set(value) != {'error', 'queued_events', 'dropped_events', 'last_committed_monotonic_us', 'data_disk_archive'}:
            raise ValueError('invalid_sonar_history_status')
        if value['error'] is not None and (not isinstance(value['error'], str) or REASON.fullmatch(value['error']) is None):
            raise ValueError('invalid_history_reason')
        if type(value['queued_events']) is not int or not 0 <= value['queued_events'] <= 256: raise ValueError('invalid_queue')
        u64(value['dropped_events'])
        if value['last_committed_monotonic_us'] is not None: u64(value['last_committed_monotonic_us'])
        if value['data_disk_archive'] is not False: raise ValueError('archive_not_implemented')
        return dict(value)
    except (KeyError, ValueError, TypeError): return None


def archive_status(config, value, now):
    enabled=config['storage'].get('sonar_recording_enabled',False)
    fallback=disabled_status(enabled,'archive_status_unavailable' if enabled else 'archive_disabled')
    if not isinstance(value,dict):return fallback
    try:
        if set(value)!=set(fallback) or type(value['enabled']) is not bool or value['enabled']!=enabled:
            raise ValueError('invalid_archive_status')
        if (value['data_disk_archive'] is not True or value['state'] not in ('DISABLED','STARTING','RECORDING','BLOCKED','ERROR','STOPPED')
                or type(value['queued_events']) is not int or not 0<=value['queued_events']<=256
                or type(value['recovery_failures']) is not int or not 0<=value['recovery_failures']<=200000):
            raise ValueError('invalid_archive_status')
        for k in ('dropped_events','written_events'):u64(value[k])
        if value['reason'] is not None and (not isinstance(value['reason'],str) or REASON.fullmatch(value['reason']) is None):
            raise ValueError('invalid_archive_reason')
        if value['active_archive_id'] is not None:
            from .sonar_archive import valid_id
            valid_id(value['active_archive_id'])
        last=value['last_committed_monotonic_us']
        if last is not None and u64(last)>now:raise ValueError('future_archive_commit')
        if not enabled:
            if value!=disabled_status():raise ValueError('disabled_archive_has_observations')
            return fallback
        if value['state']=='RECORDING':
            if last is None or value['volume_uuid']!=config['storage'].get('filesystem_uuid'):
                raise ValueError('archive_volume_mismatch')
            from .volume import MountLease
            with MountLease.acquire(config['storage']):pass
        return dict(value)
    except (OSError,ValueError,KeyError,TypeError):return fallback


def inspect(config, worker, now, boot):
    state, reason = worker['state'], worker.get('reason') or 'sonar_worker_unavailable'
    result = dict(state=state, reason=reason, serial_connected=False, journal=None, samples=empty(config, reason),
                  archive=disabled_status(config['storage'].get('sonar_recording_enabled',False),'sonar_worker_unavailable'))
    payload = worker.get('payload', {})
    try:
        # Even a forged RUNNING snapshot cannot enable an unknown wire protocol.
        profile, addresses = resolve(config)
    except ConfigError as exc:
        reason = str(exc)
        result.update(state='UNAVAILABLE', reason=reason, samples=empty(config, reason))
        result['archive']=disabled_status(config['storage'].get('sonar_recording_enabled',False),
            reason if config['storage'].get('sonar_recording_enabled',False) else 'archive_disabled')
        if state == 'UNAVAILABLE' and worker.get('service_revision') == fingerprint(config, 'sonar'):
            result['journal'] = history_status(payload.get('journal'))
        return result
    if state not in ('RUNNING', 'ERROR'): return result
    try:
        if worker.get('service_revision') != fingerprint(config, 'sonar'): raise ValueError('sonar_configuration_mismatch')
        if (set(payload) != {'reason', 'protocol_id', 'timing', 'serial_connected', 'connections', 'samples',
                             'last_received', 'invalidated', 'counters', 'journal', 'archive'}
                or payload['protocol_id'] != profile.protocol_id or payload['timing'] != asdict(profile.timing)
                or type(payload['serial_connected']) is not bool or (state == 'RUNNING') != payload['serial_connected']
                or type(payload['connections']) is not int or payload['connections'] < 0):
            raise ValueError('invalid_sonar_payload')
        observed = u64(worker['observed_monotonic_us'])
        if observed > now: raise ValueError('future_sonar_status')
        if type(payload['samples']) is not dict or set(payload['samples']) - set(DIRECTIONS): raise ValueError('invalid_sonar_samples')
        if type(payload['last_received']) is not dict or set(payload['last_received']) - set(payload['samples']):
            raise ValueError('invalid_receive_ages')
        invalidated = payload['invalidated']
        if type(invalidated) is not list or len(invalidated) > 5 or any(d not in DIRECTIONS for d in invalidated):
            raise ValueError('invalid_sonar_reset')
        last = {d: u64(t) for d, t in payload['last_received'].items()}
        for d, sample in payload['samples'].items():
            validate_event(sample)
            if (sample['sensor_id'] != d or sample['address'] != addresses[d] or sample['rk_boot_id'] != boot
                    or sample['collector_instance_id'] != worker['instance_id'] or sample['evidence_kind'] != 'device_observation'
                    or u64(sample['event_monotonic_us']) > observed or last.get(d, 0) > u64(sample['event_monotonic_us'])):
                raise ValueError('sonar_sample_identity_mismatch')
            if sample['kind'] == 'MEASUREMENT' and last.get(d) != u64(sample['received_monotonic_us']):
                raise ValueError('sonar_receive_time_mismatch')
        poller = Poller(addresses, profile.timing, boot, worker['instance_id'], lambda sample: None)
        poller.latest, poller.last_received = payload['samples'], last
        poller.invalidated = set(invalidated) if state == 'RUNNING' else set(DIRECTIONS)
        readings = poller.status(now)
        if state != 'RUNNING':
            for reading in readings: reading['reason'] = 'sonar_transport_unavailable'
        result.update(reason=None if state == 'RUNNING' else 'sonar_transport_unavailable',
                      serial_connected=payload['serial_connected'], journal=history_status(payload['journal']), samples=readings,
                      archive=archive_status(config,payload['archive'],now))
    except (KeyError, ValueError, TypeError, AttributeError):
        result.update(state='ERROR', reason='invalid_sonar_payload', samples=empty(config, 'invalid_sonar_payload'))
    return result
