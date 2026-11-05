#!/usr/bin/env python3
"""Cross-build the production camera against the locked focal/SDK sysroot, in a NEW directory."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sdk', type=Path, required=True)
    parser.add_argument('--sysroot', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--with-test-source', action='store_true', help='also build an explicitly synthetic binary, never include it in a release')
    args = parser.parse_args()
    dest, sysroot = args.output.absolute(), args.sysroot.resolve()
    if dest.exists() or dest.is_symlink():
        parser.error('output must not exist')
    lock_sha = digest(ROOT / 'packaging/arm64-sysroot.lock.json')
    prepared = json.loads((sysroot / 'ss07-sysroot.json').read_text())
    if prepared.get('lock_sha256') != lock_sha or prepared.get('architecture') != 'arm64':
        parser.error('prepare a matching locked ARM64 sysroot first')
    prefix = args.sdk.resolve() / ('prebuilts/gcc/linux-x86/aarch64/'
                                  'gcc-arm-10.3-2021.07-x86_64-aarch64-none-linux-gnu/bin/'
                                  'aarch64-none-linux-gnu-')
    compiler = Path(str(prefix) + 'g++')
    version = subprocess.check_output([str(compiler), '-dumpfullversion'], text=True).strip()
    machine = subprocess.check_output([str(compiler), '-dumpmachine'], text=True).strip()
    if version != '10.3.1' or machine != 'aarch64-none-linux-gnu':
        parser.error('expected the observed SDK AArch64 GCC 10.3.1')
    builtin = Path(subprocess.check_output([str(compiler), '-print-file-name=include'], text=True).strip()).resolve()
    # GCC's include-fixed/pthread.h was generated for its own newer libc. Exclude it,
    # while retaining compiler builtin headers and the pinned Ubuntu libc/libstdc++ headers.
    includes = [sysroot / 'usr/include/c++/10', sysroot / 'usr/include/aarch64-linux-gnu/c++/10',
                sysroot / 'usr/include/c++/10/backward', builtin,
                sysroot / 'usr/lib/gcc/aarch64-linux-gnu/10/include',
                sysroot / 'usr/include/aarch64-linux-gnu', sysroot / 'usr/include']
    # Paths become CMake code only as bracket arguments; reject the terminator and list separator.
    for path in (dest, sysroot, compiler):
        if any(char in str(path) for char in (';', ']==]', '\n', '\r')):
            parser.error('unsupported build path')
    dest.mkdir(parents=True)
    toolchain = dest / 'toolchain.cmake'
    toolchain.write_text('\n'.join([
        'set(CMAKE_SYSTEM_NAME Linux)', 'set(CMAKE_SYSTEM_PROCESSOR aarch64)',
        'set(CMAKE_CXX_COMPILER [==[' + str(compiler) + ']==])',
        'set(CMAKE_SYSROOT [==[' + str(sysroot) + ']==])',
        'set(CMAKE_FIND_ROOT_PATH [==[' + str(sysroot) + ']==])',
        'set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)',
        'set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)',
        'set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)',
        'set(CMAKE_FIND_ROOT_PATH_MODE_PACKAGE ONLY)',
        # Arm's non-Debian compiler does not automatically search Ubuntu multiarch CRT/header directories.
        'set(CMAKE_CXX_FLAGS_INIT [==[-B' + str(sysroot / 'usr/lib/aarch64-linux-gnu') +
        '/ -nostdinc -nostdinc++ ' + ' '.join('-isystem ' + str(p) for p in includes) + ']==])',
        'set(CMAKE_EXE_LINKER_FLAGS_INIT [==[-L' + str(sysroot / 'usr/lib/gcc/aarch64-linux-gnu/10') +
        ' -L' + str(sysroot / 'usr/lib/aarch64-linux-gnu') +
        ' -Wl,-rpath-link,' + str(sysroot / 'lib/aarch64-linux-gnu') +
        ' -Wl,-rpath-link,' + str(sysroot / 'usr/lib/aarch64-linux-gnu') + ']==])', '']))
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(('PKG_CONFIG', 'CMAKE_')) and key not in
           ('CPATH', 'CPLUS_INCLUDE_PATH', 'C_INCLUDE_PATH', 'LIBRARY_PATH', 'LD_LIBRARY_PATH', 'CXX', 'CXXFLAGS', 'LDFLAGS')}
    env.update(PKG_CONFIG_SYSROOT_DIR=str(sysroot),
               PKG_CONFIG_LIBDIR=os.pathsep.join(str(sysroot / path) for path in
                   ('usr/lib/aarch64-linux-gnu/pkgconfig', 'usr/share/pkgconfig')), PKG_CONFIG_PATH='', LC_ALL='C')
    gst = subprocess.check_output(['pkg-config', '--modversion', 'gstreamer-1.0'], env=env, text=True).strip()
    if gst != '1.18.5':
        parser.error('expected SDK GStreamer 1.18.5 headers')
    commands = [
        ['cmake', '-S', str(ROOT), '-B', str(dest / 'build'), '-DCMAKE_TOOLCHAIN_FILE=' + str(toolchain),
         '-DCMAKE_BUILD_TYPE=Release', '-DSS07_BUILD_TEST_SOURCE=' + ('ON' if args.with_test_source else 'OFF')],
        ['cmake', '--build', str(dest / 'build'), '--parallel', '2']]
    with (dest / 'build.log').open('w') as log:
        for command in commands:
            subprocess.run(command, env=env, stdout=log, stderr=subprocess.STDOUT, check=True)
    spec = importlib.util.spec_from_file_location('ss07_verify_arm64', ROOT / 'packaging/verify-arm64.py')
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)
    native = dest / 'build/ss07-camera'
    abi = verifier.verify(native, sysroot)
    (dest / 'abi.json').write_text(json.dumps(abi, indent=2) + '\n')
    manifest = {'artifact': 'ss07-arm64-camera-build', 'architecture': machine, 'compiler_version': version,
                'compiler_sha256': digest(compiler), 'sysroot_lock_sha256': lock_sha, 'gstreamer_core': gst,
                'commands': commands, 'binary_sha256': digest(native), 'hardware_validated': False,
                'synthetic_binary_built': args.with_test_source,
                'source_sha256': {str(p.relative_to(ROOT)): digest(p) for p in
                    (ROOT / 'CMakeLists.txt', ROOT / 'src/camera/main.cpp')}}
    (dest / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(native)


if __name__ == '__main__':
    main()
