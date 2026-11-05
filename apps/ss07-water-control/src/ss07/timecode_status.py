"""API projection of live receive RPC; worker heartbeat is not timecode freshness."""
import time

from .snapshots import u64
from .timecode_model import HOLDOVER_US, validate_tag


def inspect(client, worker):
    result = dict(quality='UNAVAILABLE', reason='receiver_not_connected', source_epoch=None,
        source_sequence=None, source_time_us=None, local_monotonic_us=None, mapping_id=None,
        age_ms=None, uncertainty_us=None, estimated_time_us=None, timestamp_basis=None,
        read_only=True, source_valid=False, serial_connected=False, software_tx_bytes=None,
        origin_basis='usb_receive_uncalibrated', origin_monotonic_us=None, journal=None)
    if worker['state'] not in ('RUNNING', 'ERROR', 'UNAVAILABLE'):
        result['reason'] = 'receiver_stale_or_not_connected'
        return result
    try:
        response = client.request({'op': 'status'})
        data = response['data']
        tag = validate_tag(data['tag'])
        if (tag['evidence_kind'] != client.evidence or tag['rk_boot_id'] != client.boot_id
                or tag['receiver_instance_id'] != response['receiver_instance_id']
                or tag['timestamp_basis'] != 'status_observation' or data['read_only'] is not True
                or data['software_tx_bytes'] != '0' or type(data['serial_connected']) is not bool):
            raise ValueError('timecode_status_identity')
        now = time.monotonic_ns() // 1000
        result.update(read_only=True, serial_connected=data['serial_connected'], software_tx_bytes='0',
                      receiver_instance_id=response['receiver_instance_id'], journal=data['journal'],
                      accepted_samples=str(u64(data['accepted_samples'])), rejected_samples=str(u64(data['rejected_samples'])),
                      last_rejection=data['last_rejection'], wire=data['wire'])
        if tag['quality'] == 'UNAVAILABLE':
            result['reason'] = data['reason'] or tag['reason']
            return result
        age = now - u64(tag['source_received_monotonic_us'])
        if not 0 <= age < HOLDOVER_US or u64(tag['source_time_us']) + age > 2**64-1:
            result['reason'] = 'holdover_expired'
            return result
        fresh = now < u64(tag['valid_until_monotonic_us'])
        result.update(quality='ESTIMATED', reason='usb_receive_uncalibrated' if fresh else 'source_lost_holdover',
            source_epoch=tag['source_epoch'], source_sequence=tag['source_sequence'], source_time_us=tag['source_time_us'],
            local_monotonic_us=str(now), mapping_id=tag['mapping_id'], age_ms=age // 1000,
            source_valid=fresh, estimated_time_us=str(u64(tag['source_time_us']) + age),
            source_received_monotonic_us=tag['source_received_monotonic_us'], timestamp_basis='status_observation')
        return result
    except (OSError, ValueError, KeyError, TypeError):
        result.update(quality='UNAVAILABLE', reason='timecode_service_unavailable', source_valid=False)
        return result
