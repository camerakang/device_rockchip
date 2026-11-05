#!/usr/bin/env python3
"""Assemble a Rockchip update IMG from checked new SS-07 rootfs and current SDK boot components."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
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
    parser.add_argument('--rootfs-build', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--rootfs-mib', type=int, default=3072)
    args = parser.parse_args()
    os.umask(0o022)
    dest, sdk, built = args.output.absolute(), args.sdk.resolve(), args.rootfs_build.resolve()
    if dest.exists() or dest.is_symlink():
        parser.error('output must not exist')
    if not 2048 <= args.rootfs_mib <= 3584:
        parser.error('rootfs-mib must be 2048..3584; keep this SDK RKAF payload below 4 GiB')
    lock = json.loads((ROOT / 'packaging/image.lock.json').read_text())
    root_manifest = json.loads((built / 'manifest.json').read_text())
    if root_manifest.get('artifact') != 'ss07-image-rootfs' or root_manifest.get('complete') is not True:
        parser.error('rootfs build is incomplete')
    if root_manifest['image_lock_sha256'] != digest(ROOT / 'packaging/image.lock.json'):
        parser.error('image lock changed after rootfs build')
    for name, expected in root_manifest['recipe_files'].items():
        if digest(ROOT / name) != expected:
            parser.error('rootfs recipe changed after build: ' + name)
    boot = Path(root_manifest['boot_build']).resolve()
    if digest(boot / 'manifest.json') != root_manifest['boot_manifest_sha256']:
        parser.error('boot manifest changed after rootfs build')
    spec = importlib.util.spec_from_file_location('ss07_rootfs_builder', ROOT / 'packaging/build-image-rootfs.py')
    helper = importlib.util.module_from_spec(spec); spec.loader.exec_module(helper)
    boot_manifest = helper.check_boot(sdk, boot)
    release = boot_manifest['kernel_release']
    initrd = built / 'state' / ('initrd-' + release)
    if digest(initrd) != root_manifest['initrd_sha256'] or digest(built / 'state/fakeroot.state') != root_manifest['fakeroot_state_sha256']:
        parser.error('rootfs ownership state or initrd changed')
    root, state = built / 'rootfs', built / 'state/fakeroot.state'
    kernel = boot / 'sources/kernel-6.1'
    dtb = kernel / 'arch/arm64/boot/dts/rockchip' / lock['dtb']
    compatible = subprocess.check_output(['fdtget', str(dtb), '/', 'compatible'], text=True).split()
    if lock['board_compatible'] not in compatible:
        parser.error('DTB does not match the observed board')
    dest.mkdir(parents=True)
    firmware, boot_tree = dest / 'firmware', dest / 'boot-tree'
    firmware.mkdir(); boot_tree.mkdir()
    manifest = {'artifact': 'ss07-full-sdk-image', 'build_complete': False, 'whole_project_complete': False,
                'format': 'Rockchip RKFW update IMG (SDK afptool/rkImageMaker)',
                'hardware_validated': False, 'boot_tested': False, 'deployed': False,
                'rootfs_build': str(built), 'rootfs_manifest_sha256': digest(built / 'manifest.json'),
                'image_lock_sha256': digest(ROOT / 'packaging/image.lock.json'),
                'boot_manifest_sha256': digest(boot / 'manifest.json'), 'sources': boot_manifest['sources'],
                'kernel_release': release, 'dtb': lock['dtb'], 'board_compatible': lock['board_compatible'],
                'rootfs_mib': args.rootfs_mib, 'application_version': lock['application']['version'],
                'application_sha256': lock['application']['sha256'], 'services_provisioned': False,
                'verification_complete': False,
                'recipe_files': {str(p.relative_to(ROOT)): digest(p) for p in
                                 (Path(__file__), ROOT / 'packaging/image-tree.py')},
                'commands': []}

    def save():
        (dest / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')

    def run(argv, name, cwd=None):
        command = {'argv': list(map(str, argv)), 'cwd': str(cwd or dest), 'log': name + '.log'}
        manifest['commands'].append(command); save()
        print(name, flush=True)
        started = time.monotonic()
        with (dest / command['log']).open('w') as log:
            result = subprocess.run(command['argv'], cwd=command['cwd'], stdout=log, stderr=subprocess.STDOUT)
        command.update(returncode=result.returncode, elapsed_seconds=round(time.monotonic() - started, 3)); save()
        result.check_returncode()

    fake = ['fakeroot', '-i', str(state), '--']
    run(fake + [sys.executable, str(ROOT / 'packaging/image-tree.py'), '--root', str(root), '--output', str(dest / 'rootfs-tree.json')], 'rootfs-tree')
    tree = json.loads((dest / 'rootfs-tree.json').read_text())
    total = sum(item.get('size', 0) for item in tree.values())
    if total > args.rootfs_mib * 1024 * 1024 * .75:
        raise ValueError('rootfs exceeds 75 percent of requested filesystem capacity')
    run(fake + ['mke2fs', '-F', '-t', 'ext4', '-b', '4096', '-m', '0', '-L', 'rootfs', '-U', lock['rootfs_uuid'],
                '-d', str(root), str(firmware / 'rootfs.img'), str(args.rootfs_mib * 256)], 'rootfs-image')
    run(['e2fsck', '-fn', str(firmware / 'rootfs.img')], 'rootfs-fsck')
    shutil.copyfile(kernel / 'arch/arm64/boot/Image', boot_tree / ('Image-' + release))
    (boot_tree / 'Image').symlink_to('Image-' + release)
    shutil.copyfile(initrd, boot_tree / ('initrd-' + release))
    (boot_tree / 'initrd').symlink_to('initrd-' + release)
    shutil.copyfile(dtb, boot_tree / 'rk-kernel.dtb')
    shutil.copyfile(kernel / '.config', boot_tree / ('config-' + release))
    shutil.copyfile(kernel / 'System.map', boot_tree / ('System.map-' + release))
    for source, name in (('logo_boot.bmp', 'logo.bmp'), ('logo_kernel.bmp', 'logo_kernel.bmp')):
        shutil.copyfile(kernel / source, boot_tree / name)
    (boot_tree / 'uEnv').mkdir()
    (boot_tree / 'uEnv/uEnv.txt').write_text((ROOT / 'packaging/image/uEnv.txt').read_text().replace('@KERNEL_RELEASE@', release))
    shutil.copyfile(kernel / 'arch/arm64/boot/dts/rockchip/uEnv/boot.cmd', boot_tree / 'boot.cmd')
    mkimage = boot / 'sources/u-boot/tools/mkimage'
    run([str(mkimage), '-A', 'arm64', '-T', 'script', '-C', 'none', '-d', str(boot_tree / 'boot.cmd'), str(boot_tree / 'boot.scr')], 'boot-script')
    (boot_tree / 'extlinux').mkdir()
    (boot_tree / 'extlinux/extlinux.conf').write_text('label ss07-' + release + '\n' +
        '  kernel /Image-' + release + '\n  initrd /initrd-' + release + '\n  fdt /rk-kernel.dtb\n' +
        '  append root=PARTUUID=' + lock['rootfs_uuid'] + ' console=ttyFIQ0 console=tty1 rootwait rw rootfstype=ext4\n')
    run(['fakeroot', '--', 'mke2fs', '-F', '-t', 'ext2', '-b', '4096', '-m', '0', '-L', 'boot', '-U', lock['boot_uuid'],
         '-O', '^64bit,^metadata_csum', '-d', str(boot_tree), str(firmware / 'boot.img'), '32768'], 'boot-image')
    run(['e2fsck', '-fn', str(firmware / 'boot.img')], 'boot-fsck')
    shutil.copyfile(boot / 'sources/u-boot/uboot.img', firmware / 'uboot.img')
    loaders = list((boot / 'sources/u-boot').glob('rk3588_spl_loader_*.bin'))
    if len(loaders) != 1:
        raise ValueError('expected one current-build loader')
    shutil.copyfile(loaders[0], firmware / 'MiniLoaderAll.bin')
    for source, name in (('parameter-extboot.txt', 'parameter.txt'), ('package-file-extboot', 'package-file')):
        shutil.copyfile(sdk / 'device/rockchip/.chips/rk3588' / source, firmware / name)
    parameter = (firmware / 'parameter.txt').read_text()
    expected_layout = '0x00004000@0x00004000(uboot),0x00040000@0x00008000(boot:bootable),-@0x00048000(rootfs:grow)'
    if 'CMDLINE: mtdparts=:' + expected_layout not in parameter:
        raise ValueError('SDK partition layout changed; review partition sizes before packaging')
    partition_uuids = dict(re.findall(r'^uuid:([^=]+)=(.+)$', parameter, re.MULTILINE))
    if any(partition_uuids.get(name, '').strip().lower() != lock[name + '_uuid'] for name in ('rootfs', 'boot')):
        raise ValueError('SDK partition UUIDs differ from rootfs fstab')
    if (firmware / 'uboot.img').stat().st_size > 8 * 1024 * 1024:
        raise ValueError('U-Boot exceeds its partition')
    pack = sdk / 'tools/linux/Linux_Pack_Firmware/rockdev'
    manifest['pack_tools'] = {name: digest(pack / name) for name in ('afptool', 'rkImageMaker')}
    run([str(pack / 'afptool'), '-pack', '.', 'update.raw.img'], 'pack-rkaf', firmware)
    image = firmware / ('SS07-' + lock['application']['version'] + '-LubanCat4-V1-' + release + '.img')
    run([str(pack / 'rkImageMaker'), '-RK3588', 'MiniLoaderAll.bin', 'update.raw.img', image.name, '-os_type:androidos'], 'pack-rkfw', firmware)
    manifest['image'] = str(image.relative_to(dest))
    manifest['image_sha256'] = digest(image)
    manifest['rootfs_tree_sha256'] = digest(dest / 'rootfs-tree.json')
    manifest['payload_files'] = {name: digest(firmware / name) for name in ('MiniLoaderAll.bin', 'uboot.img', 'boot.img', 'rootfs.img', 'parameter.txt', 'package-file')}
    (firmware / 'SHA256SUMS').write_text(manifest['image_sha256'] + '  ' + image.name + '\n')
    manifest['build_complete'] = True
    save()
    print(image)


if __name__ == '__main__':
    main()
