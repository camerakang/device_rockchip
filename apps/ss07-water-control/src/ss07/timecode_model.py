"""Record-host mappings, isolated by boot/receiver/epoch; no playback clock changes."""
import re
import uuid
from collections import deque

from .snapshots import u64
from .timecode_wire import LOSS_US, Packet

HOLDOVER_US = 5000000
HISTORY_US = 60000000
BASES = ('segment_create_notification', 'segment_complete_notification', 'sonar_receive', 'status_observation')
IDENTITY = re.compile(r'[A-Za-z0-9_-]{1,64}')
TAG_KEYS = {'schema_version', 'rk_boot_id', 'receiver_instance_id', 'mapping_id',
    'local_monotonic_us', 'timestamp_basis', 'quality', 'reason', 'source_epoch',
    'source_sequence', 'source_time_us', 'source_received_monotonic_us', 'source_age_us',
    'estimated_time_us', 'uncertainty_us', 'origin_monotonic_us', 'origin_basis',
    'source_valid', 'mapping_started_monotonic_us', 'valid_until_monotonic_us', 'evidence_kind'}


def unavailable(boot_id, event_us, basis, reason, evidence_kind='device_observation', instance=None):
    result = dict(schema_version=1, rk_boot_id=boot_id, receiver_instance_id=instance, mapping_id=None,
        local_monotonic_us=str(event_us) if event_us is not None else None,
        timestamp_basis=basis, quality='UNAVAILABLE', reason=reason,
        source_epoch=None, source_sequence=None, source_time_us=None,
        source_received_monotonic_us=None, source_age_us=None, estimated_time_us=None,
        uncertainty_us=None, origin_monotonic_us=None, origin_basis='usb_receive_uncalibrated',
        source_valid=False, mapping_started_monotonic_us=None, valid_until_monotonic_us=None,
        evidence_kind=evidence_kind)
    return validate_tag(result)


def validate_tag(tag):
    if (not isinstance(tag, dict) or set(tag) != TAG_KEYS or type(tag['schema_version']) is not int
            or tag['schema_version'] != 1 or tag['timestamp_basis'] not in BASES
            or tag['evidence_kind'] not in ('device_observation', 'synthetic_test')
            or tag['quality'] not in ('ESTIMATED', 'UNAVAILABLE') or type(tag['source_valid']) is not bool):
        raise ValueError('invalid_time_tag')
    if not isinstance(tag['rk_boot_id'], str) or IDENTITY.fullmatch(tag['rk_boot_id']) is None:
        raise ValueError('invalid_time_tag_boot')
    for key in ('receiver_instance_id', 'mapping_id'):
        if tag[key] is not None and (not isinstance(tag[key], str) or IDENTITY.fullmatch(tag[key]) is None):
            raise ValueError('invalid_time_tag_identity')
    for key in ('local_monotonic_us', 'source_epoch', 'source_sequence', 'source_time_us',
                'source_received_monotonic_us', 'source_age_us', 'estimated_time_us',
                'mapping_started_monotonic_us', 'valid_until_monotonic_us'):
        if tag[key] is not None:
            u64(tag[key])
    if (tag['uncertainty_us'] is not None or tag['origin_monotonic_us'] is not None
            or tag['origin_basis'] != 'usb_receive_uncalibrated'):
        raise ValueError('unverified_sof_calibration')
    if not isinstance(tag['reason'], str) or re.fullmatch(r'[a-z0-9_]{1,96}', tag['reason']) is None:
        raise ValueError('invalid_time_tag_reason')
    if tag['quality'] == 'UNAVAILABLE':
        if tag['source_valid'] or any(tag[key] is not None for key in ('mapping_id', 'source_epoch',
                'source_sequence', 'source_time_us', 'source_received_monotonic_us', 'source_age_us',
                'estimated_time_us', 'mapping_started_monotonic_us', 'valid_until_monotonic_us')):
            raise ValueError('unavailable_tag_has_mapping')
    else:
        if any(tag[key] is None for key in ('receiver_instance_id', 'mapping_id', 'local_monotonic_us',
                'source_epoch', 'source_sequence', 'source_time_us', 'source_received_monotonic_us',
                'source_age_us', 'estimated_time_us', 'mapping_started_monotonic_us', 'valid_until_monotonic_us')):
            raise ValueError('incomplete_time_tag_mapping')
        event, received = u64(tag['local_monotonic_us']), u64(tag['source_received_monotonic_us'])
        age = event - received
        if (not u64(tag['source_epoch']) or u64(tag['source_sequence']) > 2**32-1
                or not 0 <= age < HOLDOVER_US or age != u64(tag['source_age_us'])
                or u64(tag['estimated_time_us']) != u64(tag['source_time_us']) + age
                or u64(tag['mapping_started_monotonic_us']) > received
                or not received <= u64(tag['valid_until_monotonic_us']) <= received + LOSS_US
                or tag['source_valid'] != (event < u64(tag['valid_until_monotonic_us']))):
            raise ValueError('inconsistent_time_tag_mapping')
    return tag


class Mapper:
    def __init__(self, boot_id, instance, evidence_kind='device_observation', emit=None):
        unavailable(boot_id, 0, 'status_observation', 'no_sample', evidence_kind, instance)
        self.boot_id, self.instance, self.evidence_kind = boot_id, instance, evidence_kind
        self.emit = emit or (lambda *_: None)
        self.history = deque(maxlen=6500)
        self.epochs = set()
        self.latest = self.active = self.last_local = None
        self.clock_fault = False
        self.accepted = self.rejected = 0
        self.last_rejection = None

    def _event(self, kind, now, **data):
        self.emit(dict(schema_version=1, kind=kind, rk_boot_id=self.boot_id,
            receiver_instance_id=self.instance, observed_monotonic_us=str(now),
            evidence_kind=self.evidence_kind, **data))

    def _close(self, at_us, reason, observed):
        if self.active:
            self.active.update(end_monotonic_us=str(at_us), end_reason=reason)
            self._event('MAPPING_CLOSED', observed, mapping=dict(self.active))
            self.active = None

    def tick(self, now):
        if type(now) is not int or not 0 <= now <= 2**64 - 1:
            raise ValueError('invalid_local_clock')
        if self.last_local is not None and now < self.last_local:
            if not self.clock_fault:
                self._close(self.last_local, 'local_clock_regressed', self.last_local)
                self._event('LOCAL_CLOCK_ERROR', self.last_local)
            self.clock_fault = True
            return
        self.last_local = now
        if self.latest and self.active and now - self.latest['received_us'] >= LOSS_US:
            self._close(self.latest['received_us'] + LOSS_US, 'source_timeout', now)
        while self.history and now - self.history[0]['received_us'] > HISTORY_US:
            self.history.popleft()

    def reject(self, reason):
        self.rejected += 1
        self.last_rejection = reason
        return False

    def observe(self, packet, received_us, observed_us):
        self.tick(observed_us)
        if self.clock_fault:
            return self.reject('local_clock_regressed')
        if (not isinstance(packet, Packet) or type(received_us) is not int
                or not 0 <= received_us <= observed_us or observed_us - received_us >= LOSS_US):
            return self.reject('processing_delay_or_timestamp')
        if (not 0 < packet.epoch < 2**64 or not 0 <= packet.sequence < 2**32
                or not 0 <= packet.time_us < 2**64):
            return self.reject('invalid_source_value')
        if self.latest:
            previous = self.latest['packet']
            if received_us < self.latest['received_us']:
                return self.reject('receive_time_regressed')
            if packet.epoch == previous.epoch:
                step = (packet.sequence - previous.sequence) & 0xffffffff
                delta = packet.time_us - previous.time_us
                if not 0 < step < 0x80000000:
                    return self.reject('duplicate_or_old_sequence')
                if not 9950 * step <= delta <= 10050 * step:
                    return self.reject('source_time_step')
            elif packet.epoch in self.epochs:
                return self.reject('retired_epoch')
        if packet.epoch not in self.epochs and len(self.epochs) >= 4096:
            return self.reject('epoch_history_capacity')
        # A queued sample must not open an interval before an already closed one.
        if self.latest and self.active is None and self.latest['mapping']['end_monotonic_us'] is not None:
            if received_us < int(self.latest['mapping']['end_monotonic_us']):
                return self.reject('sample_before_closed_boundary')
        if self.active and packet.epoch != int(self.active['source_epoch']):
            self._close(received_us, 'source_epoch_changed', observed_us)
        if self.active is None:
            self.active = dict(mapping_id=str(uuid.uuid4()), source_epoch=str(packet.epoch),
                start_monotonic_us=str(received_us), end_monotonic_us=None, end_reason=None,
                origin_basis='usb_receive_uncalibrated', uncertainty_us=None)
            self._event('MAPPING_OPENED', observed_us, mapping=dict(self.active))
        self.epochs.add(packet.epoch)
        self.latest = dict(packet=packet, received_us=received_us, observed_us=observed_us, mapping=self.active)
        self.history.append(self.latest)
        self.accepted += 1
        return True

    def disconnect(self, now, reason='serial_disconnected'):
        self.tick(now)
        if not self.clock_fault:
            self._close(now, reason, now)

    def tag(self, event_us, now, basis='status_observation', boot_id=None):
        self.tick(now)
        def missing(reason):
            return unavailable(self.boot_id, event_us, basis, reason, self.evidence_kind, self.instance)
        if boot_id is not None and boot_id != self.boot_id:
            return missing('boot_mismatch')
        if event_us is None:
            return missing('event_time_missing')
        if type(event_us) is not int or not 0 <= event_us <= 2**64-1:
            raise ValueError('invalid_event_time')
        if self.clock_fault:
            return missing('local_clock_regressed')
        if event_us > now:
            return missing('future_event')
        if now - event_us > HISTORY_US:
            return missing('history_expired')
        anchor = next((a for a in reversed(self.history) if a['received_us'] <= event_us), None)
        if anchor is None:
            return missing('no_prior_sample')
        age = event_us - anchor['received_us']
        packet, mapping = anchor['packet'], anchor['mapping']
        if age >= HOLDOVER_US:
            return missing('holdover_expired')
        if packet.time_us + age > 2**64-1:
            return missing('external_time_overflow')
        until = min(2**64-1, anchor['received_us'] + LOSS_US)
        if mapping['end_monotonic_us'] is not None:
            until = min(until, int(mapping['end_monotonic_us']))
        fresh = event_us < until
        result = missing('usb_receive_uncalibrated' if fresh else 'source_lost_holdover')
        result.update(mapping_id=mapping['mapping_id'], quality='ESTIMATED', source_valid=fresh,
            source_epoch=str(packet.epoch), source_sequence=str(packet.sequence), source_time_us=str(packet.time_us),
            source_received_monotonic_us=str(anchor['received_us']), source_age_us=str(age),
            estimated_time_us=str(packet.time_us + age), mapping_started_monotonic_us=mapping['start_monotonic_us'],
            valid_until_monotonic_us=str(until))
        return validate_tag(result)

    def status(self, now):
        return dict(tag=self.tag(now, now), accepted_samples=str(self.accepted), rejected_samples=str(self.rejected),
                    last_rejection=self.last_rejection, retained_anchors=len(self.history),
                    retained_epoch_ids=len(self.epochs), clock_fault=self.clock_fault)
