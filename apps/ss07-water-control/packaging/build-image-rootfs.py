#!/usr/bin/env python3
"""Build a pinned ARM64 rootfs and matching initrd in a NEW directory, without host installs or device access."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / 'packaging/image.lock.json'
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT / 'src'))
from ss07.release_package import unpack


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


def fetch(item, cache, sdk):
    if 'sdk_path' in item:
        path = sdk / item['sdk_path']
    else:
        path = cache / item['filename']
        if not path.exists():
            partial = path.with_name(path.name + '.partial')
            with urllib.request.urlopen(item['url'], timeout=45) as response, partial.open('xb') as output:
                total = 0
                while True:
                    block = response.read(1024 * 1024)
                    if not block:
                        break
                    total += len(block)
                    if total > item.get('size', item.get('bytes')):
                        raise ValueError('download exceeds pinned size')
                    output.write(block)
            if digest(partial) != item['sha256']:
                raise ValueError('download checksum mismatch: ' + partial.name)
            partial.rename(path)
    if digest(path) != item['sha256']:
        raise ValueError('input checksum mismatch: ' + str(path))
    return path


def check_boot(sdk, boot):
    manifest = json.loads((boot / 'manifest.json').read_text())
    if manifest.get('artifact') != 'ss07-sdk-boot-components' or manifest.get('complete') is not True:
        raise ValueError('a completed build-sdk-boot.py result is required')
    for name, source in manifest['sources'].items():
        head = subprocess.check_output(['git', '-C', str(sdk / name), 'rev-parse', 'HEAD'], text=True).strip()
        dirty = subprocess.check_output(['git', '-C', str(sdk / name), 'status', '--porcelain', '--untracked-files=normal'], text=True)
        if head != source['commit'] or dirty:
            raise ValueError('boot components no longer match current clean SDK source: ' + name)
    for name, expected in manifest['files'].items():
        path = (boot / name).resolve()
        path.relative_to(boot)
        if digest(path) != expected:
            raise ValueError('boot artifact changed: ' + name)
    release = manifest['kernel_release']
    if not re.fullmatch(r'[a-zA-Z0-9._+-]{1,80}', release):
        raise ValueError('invalid kernel release')
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sdk', required=True, type=Path)
    parser.add_argument('--boot-build', required=True, type=Path)
    parser.add_argument('--application', required=True, type=Path)
    parser.add_argument('--cache', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--admin-key', type=Path, help='optional explicit SSH public key; otherwise SSH remains conditional/inactive')
    args = parser.parse_args()
    os.umask(0o022)
    sdk, boot, dest = args.sdk.resolve(), args.boot_build.resolve(), args.output.absolute()
    if dest.exists() or dest.is_symlink():
        parser.error('output must not exist')
    for tool in ('bwrap', 'fakeroot', 'dpkg-deb', 'tar', 'qemu-aarch64-static', 'systemd-tmpfiles'):
        if not shutil.which(tool):
            parser.error('missing host build tool: ' + tool)
    lock = json.loads(LOCK.read_text())
    boot_manifest = check_boot(sdk, boot)
    if digest(args.application) != lock['application']['sha256']:
        parser.error('application archive differs from image lock')
    admin_key = None
    if args.admin_key:
        admin_key = args.admin_key.read_text().strip()
        if len(admin_key) > 16384 or '\n' in admin_key or not admin_key.split() or admin_key.split()[0] not in (
                'ssh-ed25519', 'ssh-rsa', 'ecdsa-sha2-nistp256', 'ecdsa-sha2-nistp384', 'ecdsa-sha2-nistp521'):
            parser.error('admin-key must be one OpenSSH public key, not a private key or authorized_keys options')
        subprocess.run(['ssh-keygen', '-lf', str(args.admin_key)], check=True, stdout=subprocess.DEVNULL)
    args.cache.mkdir(parents=True, exist_ok=True)
    base = fetch(lock['base'], args.cache, sdk)
    packages = [(item, fetch(item, args.cache, sdk)) for item in lock['packages']]
    dest.mkdir(parents=True)
    root = dest / 'rootfs'
    root.mkdir()
    state = dest / 'state'
    state.mkdir()
    inputs, app = dest / 'inputs', dest / 'app-stage'
    inputs.mkdir()
    repo = inputs / 'debs'
    repo.mkdir()
    manifest = {'artifact': 'ss07-image-rootfs', 'complete': False, 'firmware_image': False,
                'hardware_validated': False, 'deployed': False, 'image_lock_sha256': digest(LOCK),
                'application_sha256': digest(args.application), 'boot_build': str(boot),
                'boot_manifest_sha256': digest(boot / 'manifest.json'),
                'kernel_release': boot_manifest['kernel_release'], 'sources': boot_manifest['sources'],
                'ssh_key_provided': admin_key is not None, 'services_provisioned': False,
                'recipe_files': {str(p.relative_to(ROOT)): digest(p) for p in sorted((ROOT / 'packaging/image').glob('*')) if p.is_file()},
                'commands': []}
    manifest['recipe_files']['packaging/build-image-rootfs.py'] = digest(Path(__file__))
    manifest['host_tools'] = {tool: subprocess.check_output([tool, '--version'], text=True).splitlines()[0]
                              for tool in ('fakeroot', 'systemd-tmpfiles', 'qemu-aarch64-static')}

    def save():
        (dest / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')

    def run(argv, name):
        record = {'argv': list(map(str, argv)), 'log': name + '.log'}
        manifest['commands'].append(record); save()
        print(name, flush=True)
        started = time.monotonic()
        with (dest / record['log']).open('w') as output:
            result = subprocess.run(record['argv'], stdout=output, stderr=subprocess.STDOUT)
        record.update(returncode=result.returncode, elapsed_seconds=round(time.monotonic() - started, 3)); save()
        result.check_returncode()

    # Preserve package UID/GID metadata across target installs and host filesystem image creation.
    fake_state = state / 'fakeroot.state'
    run(['fakeroot', '-s', fake_state, '--', 'tar', '-xzf', base, '-C', root], 'extract-base')
    fake = ['fakeroot', '-i', str(fake_state), '-s', str(fake_state), '--']
    for item, path in packages:
        if item['package'] in ('fakeroot', 'libfakeroot'):
            run(fake + ['dpkg-deb', '-x', str(path), str(root)], 'bootstrap-' + item['package'])
        target = repo / item['filename']
        try:
            os.link(path, target)
        except OSError:
            shutil.copyfile(path, target)
    paragraphs = []
    for item, path in packages:
        control = subprocess.check_output(['dpkg-deb', '-f', str(path)], text=True).strip()
        paragraphs.append(control + '\nFilename: ./' + item['filename'] + '\nSize: ' + str(item['size']) +
                          '\nSHA256: ' + item['sha256'] + '\n')
    (repo / 'Packages').write_text('\n'.join(paragraphs))
    (inputs / 'install-args.txt').write_text(''.join(item['package'] + '=' + item['version'] + '\n' for item, _ in packages))
    shutil.copyfile(LOCK, inputs / 'image.lock.json')
    shutil.copyfile(boot / 'sources/kernel-6.1/.config', inputs / 'kernel.config')
    (inputs / 'fstab').write_text('UUID=' + lock['rootfs_uuid'] + ' / ext4 defaults,x-systemd.growfs 0 1\n' +
                                 'UUID=' + lock['boot_uuid'] + ' /boot ext2 defaults 0 2\n')
    if admin_key is not None:
        (inputs / 'admin-authorized-key').write_text(admin_key + '\n')
    imported = unpack(args.application, lock['application']['sha256'], app)
    if imported['version'] != lock['application']['version']: raise ValueError('application version differs from image lock')
    manifest['application_import'] = imported; save()
    # This temporary source contains only packages whose bytes were checked above.
    (root / 'etc/apt/sources.list').write_text('deb [trusted=yes] file:/build-inputs/debs ./\n')
    (root / 'etc/apt/apt.conf.d/99-ss07-build').write_text('APT::Sandbox::User "root";\nAPT::Install-Recommends "false";\n')
    policy = root / 'usr/sbin/policy-rc.d'
    policy.write_text('#!/bin/sh\nexit 101\n'); policy.chmod(0o755)
    cache = root / 'var/cache/apt/archives'
    cache.mkdir(parents=True, exist_ok=True)
    for item, path in packages:
        target = cache / item['filename']
        if not target.exists():
            try:
                os.link(path, target)
            except OSError:
                shutil.copyfile(path, target)
    # The package install is offline, has no host devices, and cannot start host services.
    namespace = ['bwrap', '--unshare-user', '--uid', '0', '--gid', '0', '--unshare-pid', '--unshare-ipc', '--unshare-net',
                 '--die-with-parent', '--bind', str(root), '/', '--proc', '/proc', '--dev', '/dev', '--tmpfs', '/run', '--tmpfs', '/tmp',
                 '--tmpfs', '/sys', '--ro-bind', '/sys/fs/cgroup', '/sys/fs/cgroup',
                 '--ro-bind', str(inputs), '/build-inputs', '--ro-bind', str(app), '/build-app',
                 '--ro-bind', str(ROOT / 'packaging/image'), '/build-scripts',
                 '--ro-bind', str(boot / 'modules'), '/build-modules', '--bind', str(state), '/build-state',
                 '--clearenv', '--setenv', 'PATH', '/usr/sbin:/usr/bin:/sbin:/bin', '--setenv', 'LC_ALL', 'C.UTF-8',
                 '--setenv', 'FAKEROOTDONTTRYCHOWN', '1',
                 '/usr/bin/fakeroot-sysv', '-i', '/build-state/fakeroot.state', '-s', '/build-state/fakeroot.state',
                 '--', '/bin/sh', '/build-scripts/install-rootfs.sh', boot_manifest['kernel_release']]
    run(namespace, 'install-rootfs')
    run(fake + ['systemd-tmpfiles', '--root=' + str(root), '--create', str(root / 'usr/lib/tmpfiles.d/ss07.conf')],
        'initialize-service-directories')
    for name in ('build-inputs', 'build-app', 'build-scripts', 'build-modules', 'build-state'):
        run(fake + ['rmdir', str(root / name)], 'remove-' + name)
    initrd = state / ('initrd-' + boot_manifest['kernel_release'])
    manifest['initrd_sha256'] = digest(initrd)
    manifest['packages_sha256'] = digest(state / 'packages.tsv')
    manifest['fakeroot_state_sha256'] = digest(fake_state)
    manifest['complete'] = True
    save()
    print(dest / 'manifest.json')


if __name__ == '__main__':
    main()
