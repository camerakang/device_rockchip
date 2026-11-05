#!/usr/bin/env python3
"""Build current SDK boot sources in a NEW directory; never alter SDK output or flash."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo)] + list(args), text=True).strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sdk', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--jobs', type=int, default=4)
    args = parser.parse_args()
    sdk, output = args.sdk.resolve(), args.output.absolute()
    if output.exists() or output.is_symlink():
        parser.error('output must be a new directory')
    if not 1 <= args.jobs <= 16:
        parser.error('jobs must be 1..16')
    prefix = sdk / ('prebuilts/gcc/linux-x86/aarch64/'
                    'gcc-arm-10.3-2021.07-x86_64-aarch64-none-linux-gnu/bin/'
                    'aarch64-none-linux-gnu-')
    compiler = Path(str(prefix) + 'gcc')
    if not compiler.is_file():
        parser.error('expected SDK GCC 10.3 toolchain is missing')
    repos = {}
    for name in ('kernel-6.1', 'u-boot', 'rkbin'):
        repo = sdk / name
        if git(repo, 'status', '--porcelain', '--untracked-files=normal'):
            parser.error(name + ' has local changes; include/review them before snapshotting')
        repos[name] = {'commit': git(repo, 'rev-parse', 'HEAD'),
                       'source': str(repo), 'snapshot': 'git archive HEAD'}
    output.mkdir(parents=True)
    sources = output / 'sources'
    sources.mkdir()
    manifest = {'artifact': 'ss07-sdk-boot-components', 'sources': repos,
                'kernel_defconfig': 'lubancat_linux_rk3588_defconfig',
                'dtb': 'rk3588-lubancat-generic', 'uboot_defconfig': 'rk3588',
                'compiler_sha256': digest(compiler),
                'compiler_version': subprocess.check_output([str(compiler), '--version'], text=True).splitlines()[0],
                'complete': False, 'boot_tested': False, 'firmware_image': False,
                'vendor_blobs': 'DDR/SPL/BL31/BL32 from the pinned rkbin commit; not rebuilt from source',
                'commands': []}
    manifest_path = output / 'manifest.json'

    def save():
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')

    env = dict(os.environ, KBUILD_BUILD_USER='ss07', KBUILD_BUILD_HOST='build',
               KBUILD_BUILD_VERSION='1', KBUILD_BUILD_TIMESTAMP=git(sdk / 'kernel-6.1', 'show', '-s', '--format=%cD', 'HEAD'))

    def run(argv, name, cwd=None):
        record = {'argv': list(map(str, argv)), 'cwd': str(cwd or output), 'log': name + '.log'}
        manifest['commands'].append(record)
        save()
        print('Building ' + name, flush=True)
        start = time.monotonic()
        with (output / record['log']).open('w') as log:
            result = subprocess.run(record['argv'], cwd=str(cwd or output), env=env,
                                    stdout=log, stderr=subprocess.STDOUT)
        record.update(returncode=result.returncode, elapsed_seconds=round(time.monotonic() - start, 3))
        save()
        result.check_returncode()

    save()
    for name in repos:
        target = sources / name
        target.mkdir()
        with (output / (name + '-source.tar')).open('wb') as archive:
            subprocess.run(['git', '-C', str(sdk / name), 'archive', '--format=tar', repos[name]['commit']], stdout=archive, check=True)
        source_tar = output / (name + '-source.tar')
        repos[name]['archive_sha256'] = digest(source_tar)
        subprocess.run(['tar', '-xf', str(source_tar), '-C', str(target)], check=True)
        source_tar.unlink()
    save()
    kernel = sources / 'kernel-6.1'
    # Vendor GPU/WLAN makefiles use source-relative firmware/header paths and fail with O=.
    # An in-tree build is safe here because this is the NEW archive snapshot, not the SDK checkout.
    build = kernel
    make = ['make', '-C', str(kernel), 'ARCH=arm64', 'CROSS_COMPILE=' + str(prefix), '-j' + str(args.jobs)]
    run(make + [manifest['kernel_defconfig']], 'kernel-config')
    run(make + ['Image', 'dtbs', 'modules'], 'kernel-build')
    run(make + ['modules_install', 'INSTALL_MOD_STRIP=1', 'INSTALL_MOD_PATH=' + str(output / 'modules')], 'kernel-modules')
    for path in (output / 'modules/lib/modules').glob('*/*'):
        if path.name in ('build', 'source') and path.is_symlink():
            path.unlink()
    manifest['kernel_release'] = (build / 'include/config/kernel.release').read_text().strip()
    uboot = sources / 'u-boot'
    shutil.copyfile(build / ('arch/arm64/boot/dts/rockchip/' + manifest['dtb'] + '.dtb'), uboot / 'rk-kernel.dtb')
    run(['./make.sh', 'rk3588', 'CROSS_COMPILE=' + str(prefix)], 'uboot-build', uboot)
    artifacts = [build / 'arch/arm64/boot/Image', build / '.config',
                 build / 'System.map', uboot / 'uboot.img', uboot / 'rk-kernel.dtb']
    artifacts += sorted(uboot.glob('rk3588_spl_loader_*.bin'))
    if not list(uboot.glob('rk3588_spl_loader_*.bin')):
        raise RuntimeError('loader image missing')
    artifacts += sorted((build / 'arch/arm64/boot/dts/rockchip').glob('*.dtb'))
    artifacts += sorted((build / 'arch/arm64/boot/dts/rockchip/overlay').glob('*.dtbo'))
    artifacts += sorted(p for p in (output / 'modules').rglob('*') if p.is_file() and not p.is_symlink())
    manifest['files'] = {str(p.relative_to(output)): digest(p) for p in artifacts}
    manifest['complete'] = True
    save()
    print(manifest_path)


if __name__ == '__main__':
    main()
