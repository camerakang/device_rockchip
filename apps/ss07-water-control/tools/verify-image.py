#!/usr/bin/env python3
"""Unpack and verify a built SS-07 RKFW image; no mount, device access, or deployment."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sdk', required=True, type=Path)
    parser.add_argument('--image-build', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    built, sdk, dest = args.image_build.resolve(), args.sdk.resolve(), args.output.absolute()
    if dest.exists() or dest.is_symlink():
        parser.error('output must not exist')
    manifest = json.loads((built / 'manifest.json').read_text())
    if manifest.get('artifact') != 'ss07-full-sdk-image' or manifest.get('build_complete') is not True:
        parser.error('a completed image assembly is required')
    for name, expected in manifest['recipe_files'].items():
        if digest(ROOT / name) != expected:
            parser.error('image recipe changed: ' + name)
    image = (built / manifest['image']).resolve()
    image.relative_to(built)
    if digest(image) != manifest['image_sha256']:
        parser.error('image checksum mismatch')
    if digest(built / 'rootfs-tree.json') != manifest['rootfs_tree_sha256']:
        parser.error('rootfs tree manifest changed')
    with image.open('rb') as stream:
        if stream.read(4) != b'RKFW':
            parser.error('not an RKFW image')
    # Private parent contains any extracted setuid files and provisioning public key.
    dest.mkdir(mode=0o700, parents=True)
    result = {'evidence_kind': 'offline_full_image_readback', 'complete': False,
              'image': str(image), 'image_sha256': manifest['image_sha256'],
              'image_build_manifest_sha256': digest(built / 'manifest.json'),
              'verifier_sha256': digest(Path(__file__)), 'hardware_validated': False,
              'boot_tested': False, 'deployed': False, 'commands': []}

    def save():
        (dest / 'result.json').write_text(json.dumps(result, indent=2) + '\n')

    def run(argv, name):
        command = {'argv': list(map(str, argv)), 'log': name + '.log'}
        result['commands'].append(command); save()
        print(name, flush=True)
        started = time.monotonic()
        with (dest / command['log']).open('w') as log:
            process = subprocess.run(command['argv'], stdout=log, stderr=subprocess.STDOUT)
        command.update(returncode=process.returncode, elapsed_seconds=round(time.monotonic() - started, 3)); save()
        process.check_returncode()
        return (dest / command['log']).read_text(errors='replace')

    pack = sdk / 'tools/linux/Linux_Pack_Firmware/rockdev'
    for name, expected in manifest['pack_tools'].items():
        if digest(pack / name) != expected:
            raise ValueError('pack tool changed: ' + name)
    outer, payload = dest / 'rkfw', dest / 'payload'
    outer.mkdir(); payload.mkdir()
    run([pack / 'rkImageMaker', '-unpack', image, outer], 'unpack-rkfw')
    unpack_log = run([pack / 'afptool', '-unpack', outer / 'firmware.img', payload], 'unpack-rkaf')
    for name, expected in manifest['payload_files'].items():
        if name == 'package-file':
            # afptool 2.29 regenerates this file on unpack (spacing/CRLF and
            # no comments). Verify its mappings AND the embedded original bytes.
            source = built / 'firmware/package-file'
            if digest(source) != expected:
                raise ValueError('package-file input changed')
            def mappings(path):
                return [line.split() for line in path.read_text().splitlines()
                        if line.strip() and not line.lstrip().startswith('#')]
            if mappings(source) != mappings(payload / name):
                raise ValueError('unpacked package mappings differ')
            location = re.search(re.escape(str(payload / name)) + r'\s+offset=(0x[0-9a-fA-F]+)\s+size=(0x[0-9a-fA-F]+)', unpack_log)
            if not location or not 0 < int(location.group(2), 16) <= 65536:
                raise ValueError('cannot locate embedded package-file')
            with (outer / 'firmware.img').open('rb') as stream:
                stream.seek(int(location.group(1), 16))
                embedded = stream.read(int(location.group(2), 16))
            if hashlib.sha256(embedded).hexdigest() != expected:
                raise ValueError('embedded package-file differs')
            result['package_file'] = 'embedded bytes and regenerated mapping verified'
        elif digest(payload / name) != expected:
            raise ValueError('unpacked payload differs: ' + name)
    result['payload_files_verified'] = len(manifest['payload_files'])
    root_image, boot_image = payload / 'rootfs.img', payload / 'boot.img'
    run(['e2fsck', '-fn', root_image], 'rootfs-fsck')
    run(['e2fsck', '-fn', boot_image], 'boot-fsck')

    # Read back metadata from the filesystem inodes, independently of the build tree.
    # fakeroot retains each UID/GID written by debugfs without host root privileges.
    trees = {}
    for label, fs_image in (('rootfs', root_image), ('boot', boot_image)):
        readback = dest / (label + '-readback')
        readback.mkdir(mode=0o700)
        state = dest / (label + '-ownership.state')
        log = run(['fakeroot', '-s', state, '--', 'debugfs', '-R',
                   'rdump / "' + str(readback).replace('\\', '\\\\').replace('"', '\\"') + '"', fs_image],
                  label + '-readback')
        if len(log.strip().splitlines()) != 1 or not log.startswith('debugfs '):
            raise ValueError('debugfs reported a readback error; see ' + label + '-readback.log')
        tree_file = dest / (label + '-readback.json')
        run(['fakeroot', '-i', state, '--', sys.executable, ROOT / 'packaging/image-tree.py',
             '--root', readback, '--output', tree_file], label + '-inventory')
        trees[label] = json.loads(tree_file.read_text())
    expected_tree = json.loads((built / 'rootfs-tree.json').read_text())
    actual_tree = trees['rootfs']
    if set(actual_tree) - set(expected_tree) != {'/lost+found'}:
        raise ValueError('unexpected filesystem entries after readback')
    # rdump's chown can clear setgid/setuid on the extracted host files. Read
    # authoritative ownership/mode directly from every inode, not that copy.
    names = sorted(expected_tree)
    def quote(name):
        if any(c in name for c in ('\n', '\r', '\x00', '"')):
            raise ValueError('unsupported character in debugfs path')
        # libss/debugfs preserves backslashes inside double quotes; escaping
        # them again breaks systemd filenames containing literal "\\x2d".
        return '"' + name + '"'
    commands = dest / 'rootfs-stat.commands'
    commands.write_text(''.join('stat ' + quote(name) + '\n' for name in names))
    metadata = run(['debugfs', '-f', commands, root_image], 'rootfs-inodes')
    blocks = metadata.split('debugfs: stat ')[1:]
    if len(blocks) != len(names):
        raise ValueError('incomplete filesystem metadata readback')
    for name, block in zip(names, blocks):
        mode = re.search(r'Type:\s+.+?\s+Mode:\s+([0-7]+)', block)
        owner = re.search(r'User:\s+(\d+)\s+Group:\s+(\d+)', block)
        if block.splitlines()[0] != quote(name) or not mode or not owner or name not in actual_tree:
            raise ValueError('cannot read inode metadata: ' + name)
        actual_tree[name].update(mode=int(mode.group(1), 8), uid=int(owner.group(1)), gid=int(owner.group(2)))
    for name, expected in expected_tree.items():
        actual = actual_tree.get(name)
        if actual != expected:
            raise ValueError('filesystem content/ownership mismatch: ' + name + '\n' +
                             repr(expected) + '\n' + repr(actual))
    result['rootfs_entries_verified'] = len(expected_tree)
    result['rootfs_inode_metadata_sha256'] = digest(dest / 'rootfs-inodes.log')
    result['rootfs_readback_sha256'] = digest(dest / 'rootfs-readback.json')
    root, boot_tree = dest / 'rootfs-readback', dest / 'boot-readback'
    root_manifest = json.loads((Path(manifest['rootfs_build']) / 'manifest.json').read_text())
    if digest(Path(manifest['rootfs_build']) / 'manifest.json') != manifest['rootfs_manifest_sha256']:
        raise ValueError('rootfs build manifest changed')
    boot = Path(root_manifest['boot_build'])
    if digest(boot / 'manifest.json') != manifest['boot_manifest_sha256']:
        raise ValueError('boot build manifest changed')
    kernel = boot / 'sources/kernel-6.1'
    release = manifest['kernel_release']
    for target, source in (
            ('Image-' + release, kernel / 'arch/arm64/boot/Image'),
            ('initrd-' + release, Path(manifest['rootfs_build']) / 'state' / ('initrd-' + release)),
            ('rk-kernel.dtb', kernel / 'arch/arm64/boot/dts/rockchip' / manifest['dtb']),
            ('boot.cmd', kernel / 'arch/arm64/boot/dts/rockchip/uEnv/boot.cmd')):
        if digest(boot_tree / target) != digest(source):
            raise ValueError('boot component mismatch: ' + target)
    for alias, target in (('Image', 'Image-' + release), ('initrd', 'initrd-' + release)):
        if os.readlink(str(boot_tree / alias)) != target:
            raise ValueError('boot symlink mismatch: ' + alias)
    initrd_files = run(['lsinitramfs', boot_tree / ('initrd-' + release)], 'initrd-contents').splitlines()
    module_versions = {m.group(1) for line in initrd_files for m in
                       [re.search(r'(?:^|/)lib/modules/([^/]+)', line)] if m}
    if module_versions != {release}:
        raise ValueError('initrd module versions differ: ' + repr(module_versions))
    modules = {name for name in expected_tree if '/lib/modules/' in name and name.endswith('.ko')}
    built_modules = list((boot / 'modules/lib/modules' / release).rglob('*.ko'))
    if not built_modules or len(modules) != len(built_modules) or set(p.name for p in (root / 'lib/modules').iterdir()) != {release}:
        raise ValueError('unexpected kernel module installation')
    for source in built_modules:
        if digest(source) != digest(root / source.relative_to(boot / 'modules')):
            raise ValueError('installed module differs from current build: ' + str(source))
    result['kernel_release'] = release
    result['module_count'] = len(modules)
    result['boot_components_match_current_build'] = True

    # Independent checks prevent a matching manifest from hiding invalid image defaults.
    for name in ('/etc/shadow', '/etc/gshadow'):
        if actual_tree[name]['uid'] != 0 or actual_tree[name]['mode'] & 0o007:
            raise ValueError('account database permissions are unsafe: ' + name)
    if (root / 'etc/machine-id').read_bytes() or list((root / 'etc/ssh').glob('ssh_host_*')):
        raise ValueError('image contains machine identifiers or SSH host keys')
    if (root / 'etc/ss07-provisioned').exists() or (root / 'data/ss07').exists():
        raise ValueError('unprovisioned image unexpectedly enables services or recording storage')
    shadow = {line.split(':')[0]: line.split(':')[1] for line in (root / 'etc/shadow').read_text().splitlines()}
    if not all(shadow[name].startswith(('!', '*')) for name in ('root', 'ss07-admin')):
        raise ValueError('image contains an unlocked default account')
    uid = {line.split(':')[0]: int(line.split(':')[2]) for line in (root / 'etc/passwd').read_text().splitlines()}
    gid = {line.split(':')[0]: int(line.split(':')[2]) for line in (root / 'etc/group').read_text().splitlines()}
    for name, owner, group, mode in (
            ('/var/lib/ss07', 'ss07-storage', 'ss07-recordings', 0o2750),
            ('/var/lib/ss07/timecode', 'ss07-timecode', 'ss07-recordings', 0o2750),
            ('/var/lib/ss07/sonar', 'ss07-sonar', 'ss07-recordings', 0o2750),
            ('/var/lib/ss07-api', 'ss07-api', 'ss07-web', 0o750),
            ('/etc/ss07-credentials', 'root', 'ss07-web', 0o2750),
            ('/etc/ss07-tls', 'root', 'ss07-gateway', 0o750)):
        item = actual_tree[name]
        if (item['uid'], item['gid'], item['mode']) != (uid[owner], gid[group], mode):
            raise ValueError('service directory permissions differ: ' + name)
    result['services_provisioned'] = False
    result['recording_storage_created'] = False
    result['machine_identity_cleared'] = True
    result['default_passwords_locked'] = True
    result['service_ownership_verified'] = True
    result['complete'] = True
    save()
    print(dest / 'result.json')


if __name__ == '__main__':
    main()
