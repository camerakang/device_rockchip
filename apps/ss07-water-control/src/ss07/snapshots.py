"""One bounded atomic JSON file per local worker, freshness tied to its boot clock."""
import copy
import os
import re
import stat
import threading
import time
from pathlib import Path

from .config import ConfigError, MAX_CONFIG_BYTES, decode_json
from .persistence import atomic_json

DIRECTIONS = ('front', 'back', 'left', 'right', 'down')
SERVICES = tuple('camera.' + d for d in DIRECTIONS) + ('media', 'sonar', 'timecode', 'storage', 'maintenance', 'api', 'gateway', 'configuration')
STATES = ('STARTING', 'RUNNING', 'UNAVAILABLE', 'ERROR', 'STOPPED')


def u64(value):
    if not isinstance(value, str) or re.fullmatch(r'0|[1-9][0-9]{0,19}', value) is None:
        raise ConfigError('u64 must be a decimal string')
    result = int(value)
    if result > 2**64 - 1:
        raise ConfigError('u64 out of range')
    return result


def validate_snapshot(data, service):
    required = {'schema_version', 'service', 'instance_id', 'boot_id', 'seq',
                'observed_monotonic_us', 'state', 'evidence_kind', 'payload'}
    optional = {'config_revision', 'service_revision'}
    if type(data) is not dict or set(data) not in (required, required | optional):
        raise ConfigError('snapshot fields do not match contract')
    for key in optional & data.keys():
        if not isinstance(data[key], str) or re.fullmatch('[0-9a-f]{64}', data[key]) is None:
            raise ConfigError('snapshot configuration revision')
    if type(data['schema_version']) is not int or data['schema_version'] != 1:
        raise ConfigError('snapshot schema version')
    if service not in SERVICES or data['service'] != service:
        raise ConfigError('snapshot service mismatch')
    for key in ('instance_id', 'boot_id'):
        if not isinstance(data[key], str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', data[key]):
            raise ConfigError('snapshot identity')
    u64(data['seq'])
    u64(data['observed_monotonic_us'])
    if data['state'] not in STATES or data['evidence_kind'] != 'device_observation':
        raise ConfigError('snapshot state or evidence kind')
    if type(data['payload']) is not dict:
        raise ConfigError('snapshot payload must be an object')
    return data


def publish(directory, data):
    """For future hardware workers: caller supplies actual observation timestamps."""
    validate_snapshot(data, data.get('service'))
    path = Path(directory)
    path.mkdir(parents=True, exist_ok=True)
    atomic_json(path / (data['service'] + '.json'), data, mode=0o640)


class SnapshotReader:
    def __init__(self, directory, boot_id, stale_ms=3000, clock=None):
        self.directory = Path(directory) if directory else None
        self.boot_id = boot_id
        self.stale_ms = stale_ms
        self.clock = clock or (lambda: time.monotonic_ns() // 1000)
        self.accepted = {}
        self.lock = threading.Lock()

    def read(self, service):
        if service not in SERVICES:
            raise ValueError('unknown service')
        if self.directory is None:
            return {'state': 'NOT_IMPLEMENTED', 'age_ms': None, 'reason': 'worker_not_connected'}
        with self.lock:
            return self._read(service)

    def _read(self, service):
        path = self.directory / (service + '.json')
        try:
            fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, 'rb') as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise ConfigError('snapshot is not a regular file')
                data = validate_snapshot(decode_json(stream.read(MAX_CONFIG_BYTES + 1)), service)
            observed, seq = u64(data['observed_monotonic_us']), u64(data['seq'])
            now = self.clock()
            if self.boot_id is None or data['boot_id'] != self.boot_id:
                return {'state': 'UNAVAILABLE', 'age_ms': None, 'reason': 'boot_mismatch'}
            if observed > now:
                raise ConfigError('future snapshot timestamp')
            previous = self.accepted.get(service)
            if previous:
                old_time, old_seq = u64(previous['observed_monotonic_us']), u64(previous['seq'])
                if observed < old_time:
                    raise ConfigError('snapshot timestamp regressed')
                if data['instance_id'] == previous['instance_id']:
                    if seq < old_seq or (seq == old_seq and data != previous):
                        raise ConfigError('snapshot replay or changed duplicate')
                elif observed <= old_time:
                    raise ConfigError('restarted worker needs a new timestamp')
            self.accepted[service] = copy.deepcopy(data)
            age = (now - observed) // 1000
            if age >= self.stale_ms:
                return {'state': 'STALE', 'age_ms': age, 'reason': 'worker_timeout'}
            return {'state': data['state'], 'age_ms': age, 'reason': None,
                    'instance_id': data['instance_id'], 'seq': data['seq'],
                    'observed_monotonic_us': data['observed_monotonic_us'],
                    'config_revision': data.get('config_revision'), 'service_revision': data.get('service_revision'),
                    'payload': data['payload']}
        except FileNotFoundError:
            return {'state': 'UNAVAILABLE', 'age_ms': None, 'reason': 'snapshot_missing'}
        except (OSError, ConfigError, TypeError, ValueError, RuntimeError):
            return {'state': 'ERROR', 'age_ms': None, 'reason': 'invalid_snapshot'}
