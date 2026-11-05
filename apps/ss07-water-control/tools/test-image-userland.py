#!/usr/bin/env python3
"""Run the installed ARM64 image API in an isolated namespace, without hardware or deployment."""
import argparse
import hashlib
import json
from pathlib import Path
import signal
import sqlite3
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request


def guest():
    dest = Path('/tmp/test-output')
    config = json.loads(Path('/etc/ss07/system.json').read_text())
    # Namespace-private loopback and temporary state; image remains read-only.
    config['api'] = {'host': '127.0.0.1', 'port': 18077}
    Path('/tmp/system.json').write_text(json.dumps(config))
    state = Path('/tmp/ss07-state'); state.mkdir()
    log = (dest / 'api.log').open('w')
    process = subprocess.Popen(['/opt/ss07/current/tools/ss07', 'serve', '--config', '/tmp/system.json',
                                '--state-dir', str(state)], stdout=log, stderr=subprocess.STDOUT)
    observations = {}
    try:
        for attempt in range(150):
            if process.poll() is not None:
                raise AssertionError('image API exited before readiness')
            try:
                with urllib.request.urlopen('http://127.0.0.1:18077/healthz', timeout=2) as response:
                    observations['health'] = json.load(response)
                break
            except (urllib.error.URLError, ConnectionError):
                time.sleep(.1)
        else:
            raise AssertionError('image API readiness timed out')
        for endpoint in ('status', 'streams', 'sonars', 'timecode'):
            with urllib.request.urlopen('http://127.0.0.1:18077/api/v1/' + endpoint, timeout=3) as response:
                observations[endpoint] = json.load(response)
        with urllib.request.urlopen('http://127.0.0.1:18077/', timeout=3) as response:
            if response.status != 200 or len(response.read()) < 100:
                raise AssertionError('image web UI is missing')
        if observations['health']['data']['hardware_ready'] is not False:
            raise AssertionError('image API incorrectly claims hardware readiness')
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill(); process.wait()
            raise AssertionError('image API did not stop gracefully')
        log.close()
    if process.returncode != 0:
        raise AssertionError('image API shutdown failed')
    camera = subprocess.check_output(['/opt/ss07/current/bin/ss07-camera', '--version'], text=True).strip()
    result = {'evidence_kind': 'arm64_image_userland_qemu', 'complete': True,
              'python_version': sys.version, 'sqlite_version': sqlite3.sqlite_version,
              'openssl_version': ssl.OPENSSL_VERSION, 'camera_version': camera,
              'api_sigterm_exit': process.returncode, 'http_observations': observations,
              'web_ui_available': True, 'hardware_validated': False, 'boot_tested': False,
              'systemd_services_started': False, 'deployed': False}
    (dest / 'guest-result.json').write_text(json.dumps(result, indent=2) + '\n')


def main():
    if sys.argv[1:] == ['--guest']:
        guest()
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image-verification', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    verified, dest = args.image_verification.resolve(), args.output.absolute()
    verification = json.loads((verified / 'result.json').read_text())
    if verification.get('evidence_kind') != 'offline_full_image_readback' or verification.get('complete') is not True:
        parser.error('completed image readback verification is required')
    if dest.exists() or dest.is_symlink():
        parser.error('output must not exist')
    dest.mkdir(mode=0o700, parents=True)
    script = Path(__file__).resolve()
    cmd = ['bwrap', '--unshare-user', '--uid', '0', '--gid', '0', '--unshare-pid', '--unshare-ipc', '--unshare-net',
           '--die-with-parent', '--ro-bind', str(verified / 'rootfs-readback'), '/', '--proc', '/proc', '--dev', '/dev',
           '--tmpfs', '/run', '--tmpfs', '/tmp', '--ro-bind', str(script.parent), '/tmp/test-inputs',
           '--bind', str(dest), '/tmp/test-output', '--clearenv', '--setenv', 'PATH', '/usr/sbin:/usr/bin:/sbin:/bin',
           '--setenv', 'LC_ALL', 'C.UTF-8', '--setenv', 'PYTHONDONTWRITEBYTECODE', '1',
           '/usr/bin/python3', '/tmp/test-inputs/' + script.name, '--guest']
    with (dest / 'userland.log').open('w') as log:
        completed = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT, timeout=90)
    completed.check_returncode()
    result = json.loads((dest / 'guest-result.json').read_text())
    result['image_sha256'] = verification['image_sha256']
    result['verification_result_sha256'] = hashlib.sha256((verified / 'result.json').read_bytes()).hexdigest()
    result['qemu_version'] = subprocess.check_output(['qemu-aarch64-static', '--version'], text=True).splitlines()[0]
    (dest / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
    print(dest / 'result.json')


if __name__ == '__main__':
    main()
