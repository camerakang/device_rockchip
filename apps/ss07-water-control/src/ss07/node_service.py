"""Install/remove the independent ss07-mobocom-node unit and print node identity.

The unit lives in /etc/systemd/system so the release installer, ss07.target and
the media/API services neither start nor depend on it.
"""
import json
import os
from pathlib import Path
import subprocess
import tempfile

UNIT = 'ss07-mobocom-node.service'
UNIT_PATH = Path('/etc/systemd/system') / UNIT
AVAHI_PATH = Path('/etc/avahi/services/ss07-mobocom.service')
IDENTITY = Path('/run/ss07-mobocom-node/identity.json')


def binary(release=Path('/opt/ss07/current')):
    return release / 'bin/ss07-mobocom-node'


def info(as_json=False):
    command = [str(binary()), 'info'] + (['--json'] if as_json else [])
    return subprocess.run(command, check=False).returncode


def _write(path, text, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(text)
            stream.flush(); os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, str(path))
    except BaseException:
        if os.path.exists(temporary): os.unlink(temporary)
        raise


def avahi_service(uid):
    """Display-only mDNS record; discovery never depends on it."""
    return ('<?xml version="1.0" standalone="no"?>\n<!DOCTYPE service-group SYSTEM "avahi-service.dtd">\n'
            '<service-group>\n  <name>SS07-%08X</name>\n  <service>\n    <type>_mobocom._udp</type>\n'
            '    <port>47821</port>\n    <txt-record>uid=%d</txt-record>\n    <txt-record>type=0x9009</txt-record>\n'
            '  </service>\n</service-group>\n') % (uid & 0xffffffff, uid)


def systemctl(*args):
    subprocess.run(('systemctl',) + args, check=True, timeout=60)


def install(release=Path('/opt/ss07/current')):
    source = release / 'share' / UNIT
    if not binary(release).is_file() or not source.is_file():
        raise ValueError('current release has no ss07-mobocom-node; install a release that contains it first')
    _write(UNIT_PATH, source.read_text())
    systemctl('daemon-reload')
    systemctl('enable', UNIT)
    systemctl('restart', UNIT)  # also replaces an already running older node
    mdns = False
    if Path('/usr/sbin/avahi-daemon').exists() and AVAHI_PATH.parent.is_dir():
        for _ in range(50):
            if IDENTITY.is_file(): break
            subprocess.run(('sleep', '0.1'), check=False)
        uid = int(json.loads(IDENTITY.read_text())['uid'])
        _write(AVAHI_PATH, avahi_service(uid)); mdns = True
    return dict(unit=str(UNIT_PATH), enabled=True, mdns=mdns)


def uninstall():
    """Rollback: stop/disable the node and drop the mDNS record; media and API are untouched."""
    subprocess.run(('systemctl', 'disable', '--now', UNIT), check=False, timeout=60)
    removed = []
    for path in (UNIT_PATH, AVAHI_PATH):
        if path.exists(): path.unlink(); removed.append(str(path))
    systemctl('daemon-reload')
    return dict(removed=removed, enabled=False)


def status():
    state = subprocess.run(('systemctl', 'is-active', UNIT), capture_output=True, text=True, check=False).stdout.strip()
    enabled = subprocess.run(('systemctl', 'is-enabled', UNIT), capture_output=True, text=True, check=False).stdout.strip()
    return dict(unit=UNIT, installed=UNIT_PATH.exists(), active=state, enabled=enabled, mdns=AVAHI_PATH.exists())
