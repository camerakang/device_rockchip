#!/usr/bin/env python3
"""Run the existing management tests with ARM64 Python in an isolated QEMU root; no device access."""
import argparse
import json
from pathlib import Path
import re
import shutil
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sysroot', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    dest = args.output.absolute()
    if dest.exists() or dest.is_symlink():
        parser.error('output must not exist')
    qemu = shutil.which('qemu-aarch64-static')
    if not qemu or not shutil.which('bwrap'):
        parser.error('qemu-aarch64-static and bwrap are required')
    dest.mkdir(parents=True)
    # A real mount root is essential: qemu -L alone redirects absolute paths but
    # not subsequent openat(dir_fd, name), which would invalidate volume tests.
    base = ['bwrap', '--unshare-user', '--uid', '0', '--gid', '0', '--die-with-parent', '--tmpfs', '/']
    for name in ('usr', 'lib', 'bin', 'sbin', 'etc'):
        base += ['--ro-bind', str(args.sysroot.resolve() / name), '/' + name]
    base += ['--proc', '/proc', '--dev', '/dev',
            '--tmpfs', '/tmp', '--tmpfs', '/run', '--ro-bind', str(ROOT), '/app',
            '--ro-bind', qemu, '/qemu-aarch64-static', '--clearenv',
            '--setenv', 'PATH', '/usr/bin:/bin', '--setenv', 'LC_ALL', 'C.UTF-8',
            '--setenv', 'PYTHONHOME', '/usr', '--setenv', 'PYTHONPATH', '/app/src',
            '--setenv', 'PYTHONDONTWRITEBYTECODE', '1', '--chdir', '/app',
            '/qemu-aarch64-static', '/usr/bin/python3.8']
    runtime = subprocess.check_output(base + ['-c', 'import json,platform,sqlite3,ssl; print(json.dumps(dict(python=platform.python_version(),machine=platform.machine(),sqlite=sqlite3.sqlite_version,openssl=ssl.OPENSSL_VERSION)))'], text=True, timeout=30)
    start = time.monotonic()
    with (dest / 'unittest.log').open('w') as log:
        result = subprocess.run(base + ['-m', 'unittest', 'discover', '-s', '/app/tests', '-v'],
                                stdout=log, stderr=subprocess.STDOUT, timeout=300)
    log_text = (dest / 'unittest.log').read_text()
    match = re.search(r'Ran (\d+) tests', log_text)
    report = {'evidence_kind': 'arm64_qemu_management_tests', 'runtime': json.loads(runtime),
              'tests': int(match.group(1)) if match else None, 'returncode': result.returncode,
              'elapsed_seconds': round(time.monotonic() - start, 3),
              'isolation': 'bwrap user/mount namespace, read-only sysroot/app, private tmp/dev',
              'hardware_validated': False, 'deployed': False}
    (dest / 'result.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))
    result.check_returncode()


if __name__ == '__main__':
    main()
