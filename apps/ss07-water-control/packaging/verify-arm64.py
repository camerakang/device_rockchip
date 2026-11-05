#!/usr/bin/env python3
"""Check ELF architecture and versioned dynamic-library closure inside an ARM64 sysroot."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess


def readelf(path, *options):
    return subprocess.check_output(['readelf'] + list(options) + [str(path)], text=True,
                                   env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'})


def verify(binary, sysroot):
    sysroot = sysroot.resolve()
    directories = [sysroot / name for name in ('lib/aarch64-linux-gnu', 'usr/lib/aarch64-linux-gnu', 'lib', 'usr/lib')]
    checked = {}

    def resolve(name, extra=()):
        for directory in list(extra) + directories:
            candidate = (directory / name).resolve()
            if candidate.is_file():
                candidate.relative_to(sysroot)  # Never satisfy a dependency from the host.
                return candidate
        raise ValueError('missing target dependency: ' + name)

    def visit(path):
        path = path.resolve()
        key = str(path.relative_to(sysroot)) if sysroot in path.parents else str(path)
        if key in checked:
            return
        header = readelf(path, '-hW')
        if 'AArch64' not in header or 'ELF64' not in header or 'little endian' not in header:
            raise ValueError('not an ARM64 little-endian ELF: ' + str(path))
        dynamic = readelf(path, '-dW')
        search = re.findall(r'\((?:RPATH|RUNPATH)\).*\[([^\]]*)\]', dynamic)
        if search and path == binary.resolve():
            raise ValueError('application must not carry RPATH/RUNPATH: ' + str(path))
        extra = []
        for group in search:
            for item in group.split(':'):
                # Distribution libraries may use a target library subdirectory (e.g. elfutils).
                # Reject build paths, relative paths and expansions; resolve only inside the sysroot.
                if not item.startswith(('/usr/lib/aarch64-linux-gnu/', '/lib/aarch64-linux-gnu/')) or '..' in Path(item).parts:
                    raise ValueError('unexpected library search path: ' + item)
                extra.append(sysroot / item.lstrip('/'))
        needed = re.findall(r'\(NEEDED\).*\[([^\]]+)\]', dynamic)
        versions = readelf(path, '-VW')
        requirements = {}
        if 'Version needs section' in versions:
            body = versions.split('Version needs section', 1)[1]
            provider = None
            for line in body.splitlines():
                match = re.search(r'File: (\S+)', line)
                if match:
                    provider = match.group(1)
                    requirements[provider] = []
                match = re.search(r'Name: (\S+)', line)
                if match and provider:
                    requirements[provider].append(match.group(1))
        checked[key] = {'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                        'needed': needed, 'version_requirements': requirements,
                        'target_search_paths': search}
        for name, required in requirements.items():
            dependency = resolve(name, extra)
            definitions = readelf(dependency, '-VW').split('Version needs section', 1)[0]
            provided = set(re.findall(r'Name: (\S+)', definitions))
            missing = set(required) - provided
            if missing:
                raise ValueError('{} lacks versions {}'.format(name, sorted(missing)))
        for name in needed:
            visit(resolve(name, extra))

    visit(binary)
    program_headers = readelf(binary, '-lW')
    match = re.search(r'Requesting program interpreter: ([^\]]+)', program_headers)
    interpreter = match.group(1) if match else None
    if interpreter:
        if interpreter != '/lib/ld-linux-aarch64.so.1':
            raise ValueError('unexpected target ELF interpreter: ' + interpreter)
        loader = (sysroot / interpreter.lstrip('/')).resolve()
        loader.relative_to(sysroot)
        visit(loader)
    return {'architecture': 'arm64', 'interpreter': interpreter,
            'dynamic_dependency_closure': checked, 'hardware_validated': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--sysroot', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(verify(args.binary, args.sysroot), indent=2))


if __name__ == '__main__':
    main()
