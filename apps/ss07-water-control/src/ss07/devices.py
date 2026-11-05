"""Normalize real probe evidence and check requested camera modes without opening devices."""
import re
from decimal import Decimal

from .snapshots import DIRECTIONS


def discrete_modes(output):
    modes = []
    fourcc = size = None
    for line in output.splitlines():
        match = re.search(r"\[\d+\]: '([^']{4})'", line)
        if match:
            fourcc, size = match.group(1), None
        match = re.search(r'Size: Discrete (\d+)x(\d+)', line)
        if match:
            size = (int(match.group(1)), int(match.group(2)))
        if 'Size:' in line and 'Discrete' not in line:
            size = None  # Continuous/stepwise ranges require separate negotiation.
        match = re.search(r'Interval: Discrete [0-9.]+s \(([0-9.]+) fps\)', line)
        if match and fourcc and size:
            mode = {'fourcc': fourcc, 'width': size[0], 'height': size[1],
                    'fps_reported': match.group(1)}
            if mode not in modes:
                modes.append(mode)
    return modes


def inventory(report):
    cameras = []
    serials = {}
    camera_paths = set()
    for entry in report.get('video_devices', []):
        props = entry.get('properties', {})
        if ':capture:' not in props.get('ID_V4L_CAPABILITIES', ''):
            continue
        serial = props.get('ID_SERIAL_SHORT')
        camera_paths.add(props.get('ID_PATH', '').rsplit(':', 1)[0])
        serials[serial] = serials.get(serial, 0) + 1
        cameras.append(dict(node=entry['node'], by_id=entry.get('by_id', []),
                            by_path=entry.get('by_path', []), serial=serial,
                            query_state=entry.get('formats', {}).get('state'),
                            modes=discrete_modes(entry.get('formats', {}).get('output', ''))))
    for camera in cameras:
        serial = camera['serial']
        unique = bool(serial and serials[serial] == 1 and set(serial) != {'0'})
        camera['identity_strategy'] = 'by-id' if unique and camera['by_id'] else 'by-path'
        camera['recommended_bindings'] = camera['by_id'] if camera['identity_strategy'] == 'by-id' else camera['by_path']
        camera['direction'] = None
    serial_devices = []
    for entry in report.get('serial_devices', []):
        path = entry.get('properties', {}).get('ID_PATH', '').rsplit(':', 1)[0]
        serial_devices.append({'node': entry['node'], 'by_id': entry.get('by_id', []),
                               'by_path': entry.get('by_path', []),
                               'role': 'camera_interface' if path and path in camera_paths else 'unassigned'})
    return {'observed_at_utc': report.get('observed_at_utc'), 'cameras': cameras,
            'serial_devices': serial_devices, 'hardware_verified': False}


def check_cameras(config, report):
    devices = inventory(report)['cameras']
    channels = []
    for direction in DIRECTIONS:
        requested = config['cameras'][direction]
        binding, mode = requested['binding'], requested['mode']
        result = {'channel_id': direction, 'binding': binding, 'state': 'UNCONFIGURED'}
        matches = [d for d in devices if binding in d['by_id'] + d['by_path']]
        if binding is None or mode is None:
            pass
        elif len(matches) != 1:
            result['state'] = 'ABSENT_OR_AMBIGUOUS'
        else:
            device = matches[0]
            if '/by-id/' in binding and device['identity_strategy'] != 'by-id':
                result['state'] = 'AMBIGUOUS_IDENTITY'
            elif device['query_state'] != 'ok':
                result['state'] = 'CAPABILITIES_UNAVAILABLE'
            else:
                fps = Decimal(mode['fps_num']) / Decimal(mode['fps_den'])
                supported = any(m['fourcc'] == mode['fourcc'] and m['width'] == mode['width']
                                and m['height'] == mode['height']
                                and abs(Decimal(m['fps_reported']) - fps) <= Decimal('0.001')
                                for m in device['modes'])
                result['state'] = 'ENUMERATED' if supported else 'UNSUPPORTED_MODE'
        channels.append(result)
    return {'channels': channels, 'hardware_verified': False,
            'evidence_observed_at_utc': report.get('observed_at_utc'),
            'note': 'Saved enumeration only; re-probe and negotiate at device open. No capture has run.'}
