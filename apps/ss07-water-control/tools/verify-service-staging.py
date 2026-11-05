#!/usr/bin/env python3
"""Parse staged units with the target ARM64 systemd in isolation; never start services."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage', required=True, type=Path)
    parser.add_argument('--system-rootfs', required=True, type=Path, help='complete ARM64 rootfs with systemd and account definitions')
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    stage, system, out = args.stage.resolve(), args.system_rootfs.resolve(), args.output.absolute()
    if out.exists() or out.is_symlink(): parser.error('output must be new')
    qemu = shutil.which('qemu-aarch64-static')
    if not qemu or not shutil.which('bwrap'): parser.error('QEMU and bwrap required')
    units = sorted((stage / 'usr/lib/systemd/system').glob('ss07*'))
    if not units or not (stage / 'etc/ss07-active/system.json').is_file(): parser.error('new SS-07 staging tree required')
    out.mkdir(parents=True)
    # Copy only small configuration/unit directories. All original rootfs and
    # package files remain read-only, and no host account or unit is modified.
    shutil.copytree(system / 'etc', out / 'etc', symlinks=True)
    shutil.copytree(stage / 'etc', out / 'etc', symlinks=True, dirs_exist_ok=True)
    shutil.copytree(system / 'usr/lib/systemd/system', out / 'units', symlinks=True)
    for source in units: shutil.copyfile(source, out / 'units' / source.name)
    command = ['bwrap', '--unshare-user', '--uid', '0', '--gid', '0', '--unshare-pid', '--unshare-net', '--unshare-ipc',
               '--die-with-parent', '--ro-bind', str(system), '/', '--ro-bind', str(out / 'etc'), '/etc',
               '--ro-bind', str(out / 'units'), '/usr/lib/systemd/system',
               '--ro-bind', str(stage / 'opt/ss07'), '/opt/ss07',
               '--proc', '/proc', '--dev', '/dev', '--tmpfs', '/run', '--tmpfs', '/tmp', '--tmpfs', '/sys',
               '--ro-bind', qemu, '/run/qemu-aarch64-static',
               '--ro-bind', '/sys/fs/cgroup', '/sys/fs/cgroup', '--clearenv',
               '--setenv', 'PATH', '/usr/sbin:/usr/bin:/sbin:/bin', '--setenv', 'LC_ALL', 'C.UTF-8',
               '/run/qemu-aarch64-static', '/usr/bin/systemd-analyze']
    version = subprocess.check_output(command + ['--version'], text=True, timeout=30).splitlines()[0]
    with (out / 'verify.log').open('w') as log:
        result = subprocess.run(command + ['verify'] + ['/usr/lib/systemd/system/' + p.name for p in units],
                                stdout=log, stderr=subprocess.STDOUT, timeout=60)
    report = dict(evidence_kind='arm64_systemd_static_validation', result='passed' if result.returncode == 0 else 'failed',
                  systemd_version=version, returncode=result.returncode,
                  units={p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in units},
                  dropins={str(p.relative_to(stage)): hashlib.sha256(p.read_bytes()).hexdigest()
                           for p in sorted((stage / 'etc/systemd/system').glob('ss07*.d/*.conf'))},
                  recovery_helper_sha256=hashlib.sha256((stage / 'opt/ss07/recovery/restore.py').read_bytes()).hexdigest()
                      if (stage / 'opt/ss07/recovery/restore.py').is_file() else None,
                  executable_paths_rewritten=False, services_started=False, host_users_changed=False,
                  live_service_permissions_verified=False, hardware_verified=False)
    (out / 'result.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))
    result.check_returncode()


if __name__ == '__main__': main()
