"""USB camera auto-fallback resolution and binding helpers."""
import re
from pathlib import Path

from .snapshots import DIRECTIONS

BY_PATH_DIR = Path('/dev/v4l/by-path')
USB_INDEX0 = re.compile(r'^.+?-usb-.+-video-index0$')


def short_label(binding):
    """Turn a v4l by-path into a concise human-readable port label."""
    name = Path(binding).name
    if name.endswith('-video-index0'):
        name = name[:-len('-video-index0')]
    if name.startswith('platform-'):
        name = name[len('platform-'):]
    if '-usb-' in name:
        host, port = name.split('-usb-', 1)
        if port.endswith(':1.0'):
            port = port[:-4]
        return f'{host} · {port}'
    return name


def _resolve_video_node(path, require_char=True):
    try:
        target = Path(path).resolve(strict=True)
    except OSError:
        return None
    if target.parent != Path('/dev') or not target.name.startswith('video'):
        return None
    if require_char and not target.is_char_device():
        return None
    return str(target)


def discover_usb_cameras(by_path_dir=BY_PATH_DIR, require_char=True):
    """List currently attached USB capture cameras sorted by physical by-path."""
    directory = Path(by_path_dir)
    if not directory.is_dir():
        return []
    seen_nodes = set()
    devices = []
    for entry in sorted(directory.iterdir(), key=lambda p: p.name):
        if not USB_INDEX0.fullmatch(entry.name):
            continue
        node = _resolve_video_node(entry, require_char=require_char)
        if node is None or node in seen_nodes:
            continue
        seen_nodes.add(node)
        binding = str(entry)
        devices.append({
            'binding': binding,
            'label': short_label(binding),
            'device_node': node,
        })
    return devices


def resolve_all(config, devices=None, by_path_dir=BY_PATH_DIR, require_char=True):
    """Deterministically resolve effective bindings across all 5 camera channels.

    Pass 1 keeps every channel whose configured binding currently exists.
    Pass 2 deterministically pairs remaining configured channels with free USB
    cameras in sorted order so moving a camera to another USB port still
    produces live video without cross-process races.
    """
    if devices is None:
        devices = discover_usb_cameras(by_path_dir=by_path_dir, require_char=require_char)
    by_binding = {d['binding']: d for d in devices}
    used_bindings = set()
    used_nodes = set()
    resolved = {}

    for channel in DIRECTIONS:
        cam = config['cameras'][channel]
        configured = cam.get('binding')
        if configured is None:
            continue
        if configured in by_binding:
            node = by_binding[configured]['device_node']
        elif devices is not None and by_path_dir == BY_PATH_DIR:
            node = _resolve_video_node(configured, require_char=require_char)
        else:
            node = None
        if node is not None and node not in used_nodes:
            used_bindings.add(configured)
            used_nodes.add(node)
            resolved[channel] = {
                'channel_id': channel,
                'configured_binding': configured,
                'effective_binding': configured,
                'fallback': False,
                'device_node': node,
            }

    missing = [
        ch for ch in DIRECTIONS
        if ch not in resolved and config['cameras'][ch].get('mode') is not None
    ]
    free = [
        d for d in devices
        if d['binding'] not in used_bindings and d['device_node'] not in used_nodes
    ]
    for channel, dev in zip(missing, free):
        used_bindings.add(dev['binding'])
        used_nodes.add(dev['device_node'])
        resolved[channel] = {
            'channel_id': channel,
            'configured_binding': config['cameras'][channel].get('binding'),
            'effective_binding': dev['binding'],
            'fallback': True,
            'device_node': dev['device_node'],
        }

    for channel in DIRECTIONS:
        if channel not in resolved:
            resolved[channel] = {
                'channel_id': channel,
                'configured_binding': config['cameras'][channel].get('binding'),
                'effective_binding': None,
                'fallback': False,
                'device_node': None,
            }
    return resolved


def resolve_channel(config, channel, devices=None, by_path_dir=BY_PATH_DIR, require_char=True):
    entry = resolve_all(config, devices=devices, by_path_dir=by_path_dir, require_char=require_char)[channel]
    return entry['effective_binding'], entry['fallback']


def validate_bindings_patch(current_cameras, patch):
    """Validate PUT /api/v1/cameras/bindings payload and return merged binding map."""
    if type(patch) is not dict or not patch:
        return None, ['bindings must be a non-empty object']
    unknown = set(patch) - set(DIRECTIONS)
    if unknown:
        return None, ['unknown channels: ' + ', '.join(sorted(unknown))]
    merged = {ch: current_cameras[ch].get('binding') for ch in DIRECTIONS}
    for ch, value in patch.items():
        if value is not None:
            if (not isinstance(value, str)
                    or not value.startswith('/dev/v4l/by-path/')
                    or '..' in value or len(value) > 256):
                return None, [f'invalid binding for {ch}']
        merged[ch] = value
    non_null = [v for v in merged.values() if v is not None]
    if len(non_null) != len(set(non_null)):
        return None, ['duplicate binding across channels']
    return merged, []
