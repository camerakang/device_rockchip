"""Read-only storage gate; no recordings directory is created by this module."""
import os
import re
from pathlib import Path


def _unescape(value):
    return re.sub(r'\\([0-7]{3})', lambda match: chr(int(match.group(1), 8)), value)


def inspect_storage(config, mountinfo=None):
    target = Path(config['mountpoint'])
    recordings = Path(config['recordings_dir'])
    result = dict(mount_present=False, writable=False, free_bytes=None,
                  recording_enabled=False, state='NOT_IMPLEMENTED',
                  gate='STORAGE_UNAVAILABLE')
    try:
        if target.resolve() != target or recordings.resolve() != recordings:
            result['gate'] = 'SYMLINK_PATH'
            return result
        if mountinfo is None:
            mountinfo = Path('/proc/self/mountinfo').read_text()
        entries = []
        for line in mountinfo.splitlines():
            before, after = line.split(' - ', 1)
            fields, fs = before.split(), after.split()
            entries.append({'device': fields[2], 'target': _unescape(fields[4]),
                            'options': fields[5].split(',') + fs[2].split(',')})
        mount = next((e for e in reversed(entries) if e['target'] == str(target)), None)
        root = next((e for e in reversed(entries) if e['target'] == '/'), None)
        if mount is None:
            result['gate'] = 'MOUNT_MISSING'
            return result
        result['mount_present'] = True
        if root is None or root['device'] == mount['device']:
            result['gate'] = 'SYSTEM_FILESYSTEM'
            return result
        if 'ro' in mount['options']:
            result['gate'] = 'READ_ONLY'
            return result
        usage = os.statvfs(str(target))
        result['free_bytes'] = str(usage.f_bavail * usage.f_frsize)
        result['writable'] = os.access(str(target), os.W_OK)
        result['gate'] = 'ELIGIBLE' if result['writable'] else 'PERMISSION_DENIED'
    except (OSError, ValueError, IndexError, RuntimeError):
        result['gate'] = 'INSPECTION_FAILED'
    return result
