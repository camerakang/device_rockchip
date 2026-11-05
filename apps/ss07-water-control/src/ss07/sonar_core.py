"""Vendor-independent, bounded single-bus polling; no wire format or serial I/O."""
from dataclasses import dataclass
from datetime import datetime, timezone
import re
import uuid

from .snapshots import DIRECTIONS, u64
from .timecode_model import IDENTITY, validate_tag

MAX_U64 = 2**64 - 1
REASON = re.compile(r'[a-z0-9_]{1,96}')
KINDS = ('MEASUREMENT', 'POLL_TIMEOUT', 'SEND_FAILED', 'BUS_ERROR', 'HISTORY_GAP',
         'COLLECTOR_STARTED', 'COLLECTOR_STOPPED', 'BUS_CONNECTED', 'BUS_DISCONNECTED')


@dataclass(frozen=True)
class Timing:
    # All values must come from a reviewed driver/adapter profile. There are no
    # guessed production defaults for absent hardware.
    response_timeout_us: int
    max_response_age_us: int
    quiet_us: int
    min_cycle_us: int
    stale_us: int
    correlation: str

    def __post_init__(self):
        for key in ('response_timeout_us', 'max_response_age_us', 'quiet_us', 'min_cycle_us', 'stale_us'):
            value = getattr(self, key)
            if type(value) is not int or not 0 <= value <= 60000000: raise ValueError('invalid_sonar_timing')
        if (not 1000 <= self.response_timeout_us <= self.max_response_age_us <= 5000000
                or self.stale_us < self.response_timeout_us or self.min_cycle_us < 1000
                or self.correlation not in ('wire_id', 'bounded_response_window')):
            raise ValueError('invalid_sonar_timing')


@dataclass(frozen=True)
class Request:
    sensor_id: str
    address: str
    request_id: str


@dataclass(frozen=True)
class Reply:
    address: str
    request_id: str  # None only for a reviewed bounded-response-window protocol.
    valid: bool
    distance_mm: int
    reason: str
    quality: str


def valid_reply(value):
    if not isinstance(value, Reply) or not isinstance(value.address, str) or not 1 <= len(value.address) <= 256:
        raise ValueError('invalid_sonar_reply')
    if value.request_id is not None: u64(value.request_id)
    if type(value.valid) is not bool: raise ValueError('invalid_sonar_validity')
    if value.valid:
        if type(value.distance_mm) is not int or not 0 <= value.distance_mm <= 2**32-1 or value.reason is not None:
            raise ValueError('invalid_sonar_distance')
    elif value.distance_mm is not None or not isinstance(value.reason, str) or REASON.fullmatch(value.reason) is None:
        raise ValueError('invalid_sonar_invalid_value')
    if value.quality is not None and (not isinstance(value.quality, str) or REASON.fullmatch(value.quality) is None):
        raise ValueError('invalid_sonar_quality')
    return value


EVENT_FIELDS = {'schema_version', 'sample_id', 'kind', 'rk_boot_id', 'collector_instance_id', 'seq',
    'sensor_id', 'direction', 'address', 'request_id', 'event_monotonic_us', 'received_monotonic_us',
    'observed_at_utc', 'timestamp_basis', 'measurement_time_us', 'valid', 'distance_mm', 'reason',
    'quality', 'correlation_basis', 'evidence_kind', 'lost_events'}


def validate_event(value, tagged=False):
    fields = EVENT_FIELDS | ({'time_tag'} if tagged else set())
    if (type(value) is not dict or set(value) != fields or type(value['schema_version']) is not int
            or value['schema_version'] != 1 or value['kind'] not in KINDS
            or value['evidence_kind'] not in ('device_observation', 'synthetic_test')):
        raise ValueError('invalid_sonar_event')
    if not isinstance(value['sample_id'], str) or str(uuid.UUID(value['sample_id'])) != value['sample_id']:
        raise ValueError('invalid_sample_id')
    for key in ('rk_boot_id', 'collector_instance_id'):
        if not isinstance(value[key], str) or IDENTITY.fullmatch(value[key]) is None: raise ValueError('invalid_sonar_identity')
    for key in ('seq', 'event_monotonic_us', 'lost_events'): u64(value[key])
    if not isinstance(value['observed_at_utc'], str) or len(value['observed_at_utc']) > 40:
        raise ValueError('invalid_sample_utc')
    utc = datetime.fromisoformat(value['observed_at_utc'])
    if utc.tzinfo is None or utc.utcoffset().total_seconds() != 0: raise ValueError('sample_utc_required')
    if value['measurement_time_us'] is not None: raise ValueError('vendor_measurement_clock_not_verified')
    if value['kind'] in ('HISTORY_GAP', 'COLLECTOR_STARTED', 'COLLECTOR_STOPPED', 'BUS_CONNECTED', 'BUS_DISCONNECTED'):
        if (any(value[k] is not None for k in ('sensor_id', 'direction', 'address', 'request_id', 'received_monotonic_us',
                                             'distance_mm', 'quality', 'correlation_basis'))
                or value['valid'] is not False
                or value['timestamp_basis'] != 'status_observation'):
            raise ValueError('invalid_sonar_gap')
        if value['kind'] == 'HISTORY_GAP':
            if value['reason'] != 'history_gap' or not u64(value['lost_events']): raise ValueError('invalid_history_gap')
        elif value['lost_events'] != '0' or value['reason'] not in {
                'COLLECTOR_STARTED': ('collector_started', 'unclean_previous_session'),
                'COLLECTOR_STOPPED': ('collector_stopped',), 'BUS_CONNECTED': ('bus_connected',),
                'BUS_DISCONNECTED': ('bus_disconnected',)}[value['kind']]:
            raise ValueError('invalid_collector_lifecycle')
    else:
        if value['sensor_id'] not in DIRECTIONS or value['direction'] != value['sensor_id'] or value['lost_events'] != '0':
            raise ValueError('invalid_sonar_sensor')
        valid_reply(Reply(value['address'], value['request_id'], value['valid'], value['distance_mm'], value['reason'], value['quality']))
        if value['request_id'] is None or value['correlation_basis'] not in ('wire_id', 'bounded_response_window'):
            raise ValueError('missing_sonar_request_identity')
        if value['kind'] == 'MEASUREMENT':
            if value['timestamp_basis'] != 'sonar_receive' or value['received_monotonic_us'] != value['event_monotonic_us']:
                raise ValueError('invalid_sonar_receive_time')
        elif (value['timestamp_basis'] != 'status_observation' or value['received_monotonic_us'] is not None
                or value['valid'] or value['quality'] is not None or value['reason'] != {
                    'POLL_TIMEOUT': 'poll_timeout', 'SEND_FAILED': 'send_failed', 'BUS_ERROR': 'bus_disconnected'}[value['kind']]):
            raise ValueError('timeout_is_not_a_measurement')
    if tagged:
        tag = validate_tag(value['time_tag'])
        if (tag['rk_boot_id'] != value['rk_boot_id'] or tag['local_monotonic_us'] != value['event_monotonic_us']
                or tag['timestamp_basis'] != value['timestamp_basis'] or tag['evidence_kind'] != value['evidence_kind']):
            raise ValueError('sonar_tag_identity_mismatch')
    return value


def event(boot, instance, seq, kind, now, evidence, request=None, reply=None, correlation=None, lost=0):
    received = kind == 'MEASUREMENT'
    value = dict(schema_version=1, sample_id=str(uuid.uuid4()), kind=kind, rk_boot_id=boot,
        collector_instance_id=instance, seq=str(seq), sensor_id=request.sensor_id if request else None,
        direction=request.sensor_id if request else None, address=request.address if request else None,
        request_id=request.request_id if request else None, event_monotonic_us=str(now),
        received_monotonic_us=str(now) if received else None, observed_at_utc=datetime.now(timezone.utc).isoformat(),
        timestamp_basis='sonar_receive' if received else 'status_observation', measurement_time_us=None,
        valid=reply.valid if reply else False, distance_mm=reply.distance_mm if reply else None,
        reason=reply.reason if reply else {'POLL_TIMEOUT': 'poll_timeout', 'SEND_FAILED': 'send_failed', 'HISTORY_GAP': 'history_gap',
            'COLLECTOR_STARTED': 'collector_started', 'COLLECTOR_STOPPED': 'collector_stopped',
            'BUS_ERROR': 'bus_disconnected', 'BUS_CONNECTED': 'bus_connected', 'BUS_DISCONNECTED': 'bus_disconnected'}[kind],
        quality=reply.quality if reply else None, correlation_basis=correlation,
        evidence_kind=evidence, lost_events=str(lost))
    return validate_event(value)


class Poller:
    """One outstanding request, strict round-robin, and no retries before peers.

    Drivers lacking a wire request ID must guarantee max_response_age_us from
    host write completion. Wait out that entire window even after an early
    reply. A protocol unable to supply this bound is not safe to enable.
    """
    def __init__(self, addresses, timing, boot, instance, submit, evidence_kind='device_observation'):
        if set(addresses) != set(DIRECTIONS) or not isinstance(timing, Timing): raise ValueError('invalid_sonar_nodes')
        configured = [v for v in addresses.values() if v is not None]
        if (any(not isinstance(v, str) or not 1 <= len(v) <= 256 for v in configured)
                or len(set(configured)) != len(configured)): raise ValueError('invalid_sonar_addresses')
        if (evidence_kind not in ('device_observation', 'synthetic_test') or not callable(submit)
                or any(not isinstance(v, str) or IDENTITY.fullmatch(v) is None for v in (boot, instance))):
            raise ValueError('invalid_sonar_collector')
        self.addresses, self.timing = dict(addresses), timing
        self.boot, self.instance, self.submit, self.evidence = boot, instance, submit, evidence_kind
        self.index = self.seq = self.request_seq = 0
        self.pending = self.sent_us = self.deadline = self.last_clock = None
        self.ready_at = 0
        self.starts, self.latest, self.last_received = {}, {}, {}
        self.invalidated = set()
        self.counters = dict(polls=0, measurements=0, invalid_measurements=0, timeouts=0, unmatched=0, send_failures=0)

    def clock(self, now):
        if type(now) is not int or not 0 <= now <= MAX_U64 or (self.last_clock is not None and now < self.last_clock):
            raise ValueError('sonar_clock_regressed')
        self.last_clock = now

    def _finish(self, kind, now, reply=None):
        self.seq += 1
        sample = event(self.boot, self.instance, self.seq, kind, now, self.evidence, self.pending, reply, self.timing.correlation)
        self.latest[self.pending.sensor_id] = sample
        if reply is not None:
            self.last_received[self.pending.sensor_id] = now
            self.invalidated.discard(self.pending.sensor_id)
        self.submit(sample)
        if self.sent_us is not None and self.timing.correlation == 'bounded_response_window':
            self.ready_at = max(now + self.timing.quiet_us, self.sent_us + self.timing.max_response_age_us)
        else: self.ready_at = now + self.timing.quiet_us
        self.pending = self.sent_us = self.deadline = None
        return sample

    def tick(self, now):
        self.clock(now)
        if self.deadline is not None and now >= self.deadline:
            self.counters['timeouts'] += 1
            self._finish('POLL_TIMEOUT', now)

    def next_request(self, now):
        self.tick(now)
        if self.pending is not None or now < self.ready_at: return None
        for _ in DIRECTIONS:
            direction = DIRECTIONS[self.index]; self.index = (self.index + 1) % len(DIRECTIONS)
            address = self.addresses[direction]
            if address is None or now < self.starts.get(direction, -self.timing.min_cycle_us) + self.timing.min_cycle_us: continue
            self.request_seq += 1
            if self.request_seq > MAX_U64: raise ValueError('sonar_request_sequence_exhausted')
            self.pending = Request(direction, address, str(self.request_seq))
            return self.pending
        return None

    def sent(self, now):
        self.clock(now)
        if self.pending is None or self.sent_us is not None: raise ValueError('no_pending_sonar_send')
        self.sent_us = now; self.deadline = now + self.timing.response_timeout_us
        self.starts[self.pending.sensor_id] = now
        self.counters['polls'] += 1

    def send_failed(self, now):
        self.clock(now)
        if self.pending is None: raise ValueError('no_pending_sonar_send')
        self.starts[self.pending.sensor_id] = now
        self.counters['send_failures'] += 1
        self._finish('SEND_FAILED', now)
        # Even a partial host write can cause a device response. The caller
        # must close/reset the transport before resuming the bus.
        self.ready_at = max(self.ready_at, now + self.timing.max_response_age_us)

    def connection(self, connected, now):
        self.clock(now)
        if self.pending is not None: self._finish('BUS_ERROR', now)
        self.invalidated.update(DIRECTIONS)
        self.ready_at = max(self.ready_at, now + self.timing.max_response_age_us + self.timing.quiet_us)
        self.seq += 1
        self.submit(event(self.boot, self.instance, self.seq,
                          'BUS_CONNECTED' if connected else 'BUS_DISCONNECTED', now, self.evidence))

    def receive(self, reply, received_us):
        self.clock(received_us); valid_reply(reply)
        good = self.pending is not None and self.sent_us is not None
        good = good and self.sent_us <= received_us < self.deadline and reply.address == self.pending.address
        if good:
            good = (reply.request_id == self.pending.request_id if self.timing.correlation == 'wire_id' else reply.request_id is None)
        if not good:
            self.counters['unmatched'] += 1
            self.tick(received_us)
            return False
        self.counters['measurements'] += 1
        if not reply.valid: self.counters['invalid_measurements'] += 1
        self._finish('MEASUREMENT', received_us, reply)
        return True

    def status(self, now):
        self.tick(now)
        result = []
        for direction in DIRECTIONS:
            sample = self.latest.get(direction)
            received = self.last_received.get(direction)
            age = None if received is None else (now - received) // 1000
            fresh = received is not None and now - received < self.timing.stale_us
            valid = bool(fresh and sample and sample['valid'] and direction not in self.invalidated)
            reason = (sample['reason'] if sample else 'no_measurement')
            if sample and sample['valid'] and not fresh: reason = 'sample_stale'
            if direction in self.invalidated: reason = 'awaiting_measurement_after_reconnect'
            if self.addresses[direction] is None: reason = 'node_unconfigured'
            result.append(dict(sensor_id=direction, direction=direction, address=self.addresses[direction],
                seq=sample['seq'] if sample else None, sample_id=sample['sample_id'] if sample else None,
                distance_mm=sample['distance_mm'] if valid else None, valid=valid, reason=reason,
                quality=sample['quality'] if sample else None, age_ms=age,
                received_monotonic_us=str(received) if received is not None else None,
                timestamp_basis='sonar_receive', measurement_time_us=None, evidence_kind=self.evidence))
        return result
