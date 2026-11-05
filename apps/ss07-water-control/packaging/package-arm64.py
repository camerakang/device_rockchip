#!/usr/bin/env python3
"""Package verified ARM64 application binaries and a non-enabled staging tree; never install or flash."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from ss07 import __version__
from ss07.release_contract import contract


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--native-build', type=Path, required=True, help='successful build-arm64.py output')
    parser.add_argument('--media-dir', type=Path, required=True)
    parser.add_argument('--gateway-dir', type=Path, required=True)
    parser.add_argument('--validation', type=Path, required=True, help='successful test-arm64.py result.json')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    dest = args.output.absolute()
    if dest.exists() or dest.is_symlink():
        parser.error('output must not exist')
    native = args.native_build / 'build/ss07-camera'
    built = json.loads((args.native_build / 'manifest.json').read_text())
    validation = json.loads(args.validation.read_text())
    if (built.get('artifact') != 'ss07-arm64-camera-build'
            or built.get('binary_sha256') != digest(native)
            or validation.get('binary_sha256') != digest(native)
            or validation.get('evidence_kind') != 'arm64_qemu_synthetic'
            or validation.get('native_sigterm_exit') != 0
            or validation.get('rtp_sequence_gaps') != 0
            or validation.get('production_rejects_synthetic') is not True
            or validation.get('missing_device_rejected') is not True
            or validation.get('decoded_stream', {}).get('decoded_frames', 0) < 5
            or built.get('sysroot_lock_sha256') != digest(ROOT / 'packaging/arm64-sysroot.lock.json')):
        parser.error('native build/validation does not match the production binary and current lock')
    for name in ('CMakeLists.txt', 'src/camera/main.cpp'):
        if built.get('source_sha256', {}).get(name) != digest(ROOT / name):
            parser.error('native source changed after build: ' + name)
    spec = importlib.util.spec_from_file_location('ss07_staging', ROOT / 'packaging/stage-rootfs.py')
    staging = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(staging)
    if staging.architecture(native) != 'arm64':
        parser.error('expected ARM64 camera binary')
    for directory, name in ((args.media_dir, 'mediamtx'), (args.gateway_dir, 'caddy')):
        if staging.verified_asset(directory, name) != 'arm64':
            parser.error('expected pinned ARM64 ' + name)
    dest.mkdir(parents=True)
    stage = dest / 'rootfs-stage'
    subprocess.run([sys.executable, str(ROOT / 'packaging/stage-rootfs.py'), '--destdir', str(stage),
                    '--native-dir', str(native.parent), '--media-dir', str(args.media_dir),
                    '--gateway-dir', str(args.gateway_dir)], check=True)
    release = stage / 'opt/ss07/releases' / __version__
    shutil.copyfile(args.native_build / 'manifest.json', release / 'native-manifest.json')
    shutil.copyfile(args.native_build / 'abi.json', release / 'native-abi.json')
    shutil.copyfile(args.validation, release / 'native-qemu-validation.json')
    shutil.copyfile(ROOT / 'packaging/arm64-sysroot.lock.json', release / 'build-test-sysroot.lock.json')
    docs = release / 'docs'
    docs.mkdir()
    for name in ('arm64-build.md', 'auth-maintenance.md', 'timecode-runtime.md', 'configuration-application.md',
                 'installation-recovery.md', 'native-rtsp.md', 'sonar-foundation.md', 'sonar-archive.md',
                 'sonar-services.md', 'sonar-migration.md', 'acceptance-720p25.md'):
        shutil.copyfile(ROOT / 'docs' / name, docs / name)
    files, symlinks = {}, {}
    for path in sorted(stage.rglob('*')):
        name = str(path.relative_to(stage))
        if path.is_symlink():
            symlinks[name] = os.readlink(str(path))
        elif path.is_file():
            files[name] = digest(path)
    if any(name.endswith('/ss07-camera-test') for name in files):
        raise ValueError('synthetic source must not enter a release')
    manifest = {'artifact': 'ss07-arm64-application', 'version': __version__, 'architecture': 'arm64',
                'deployment': contract(),
                'firmware_image': False, 'hardware_validated': False, 'services_enabled': False,
                'requires': {'python': '>=3.8 with sqlite3 and OpenSSL scrypt',
                             'validated_os_abi': 'Ubuntu 20.04 ARM64, glibc 2.31; not proof of board operation',
                             'gstreamer_core': '1.18.5 (SDK)',
                             'plugins': ['v4l2src', 'h264parse', 'queue', 'capsfilter', 'rtph264pay', 'udpsink'],
                             'local_installation': 'systemd/sysusers/tmpfiles, iproute2, OpenSSL CLI compatible with 1.1.1; root on ARM64',
                             'provisioning': 'explicit accounts, TLS, gateway address, hardware binding and recording volume'},
                'contents': 'application overlay only; no OS libraries, credentials, data volume or synthetic binary',
                'files': files, 'symlinks': symlinks}
    (stage / 'SS07-MANIFEST.json').write_text(json.dumps(manifest, indent=2) + '\n')
    name = 'ss07-' + __version__ + '-linux-arm64'
    archive = dest / (name + '.tar.gz')

    def ownership(info):
        info.uid = info.gid = 0
        info.uname = info.gname = 'root'
        return info

    with tarfile.open(archive, 'w:gz') as tar:
        tar.add(stage, arcname=name, filter=ownership)
    (dest / 'SHA256SUMS').write_text(digest(archive) + '  ' + archive.name + '\n')
    (dest / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(archive)


if __name__ == '__main__':
    main()
