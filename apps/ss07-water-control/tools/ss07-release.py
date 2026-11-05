#!/usr/bin/env python3
"""Offline local ARM64 installation, readiness and recovery. Requires explicit invocation."""
import argparse
import json
import os
from pathlib import Path
import platform
import sqlite3
import subprocess
import sys
import tempfile

# Resolve before current can change, so one transaction never mixes releases.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.dont_write_bytecode = True
from ss07.release_install import Installer
from ss07.release_package import unpack


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('inspect', 'install'):
        command = sub.add_parser(name)
        command.add_argument('--archive', required=True, type=Path)
        command.add_argument('--sha256', required=True, help='trusted digest from the independent release record')
    sub.add_parser('status'); sub.add_parser('check-ready'); sub.add_parser('activate')
    command = sub.add_parser('recover'); command.add_argument('--operation-id', required=True)
    command = sub.add_parser('rollback'); command.add_argument('--expected-version', required=True)
    args = parser.parse_args()
    try:
        if args.command == 'inspect':
            with tempfile.TemporaryDirectory(prefix='ss07-package-check-') as temporary:
                result = unpack(args.archive, args.sha256, Path(temporary) / 'import')
        else:
            if os.geteuid() != 0 or platform.machine() != 'aarch64':
                parser.error('local installation commands require root on the intended ARM64 system')
            with Installer() as manager:
                if args.command == 'install': result = manager.install(args.archive, args.sha256)
                elif args.command == 'recover': result = manager.recover(args.operation_id)
                elif args.command == 'rollback': result = manager.rollback(args.expected_version)
                elif args.command == 'check-ready':
                    from ss07.provisioning import check
                    result = check()
                elif args.command == 'activate': result = manager.activate()
                else: result = manager.status()
        print(json.dumps(result, ensure_ascii=False, indent=2)); return 0
    except (OSError, ValueError, sqlite3.Error, subprocess.SubprocessError) as exc:
        # No exception text, request content, TLS material or environment leaks.
        reason = str(exc) if isinstance(exc, ValueError) and str(exc).replace('_', '').isalnum() else type(exc).__name__
        print(json.dumps(dict(error=reason, completed=False, hardware_verified=False)), file=sys.stderr)
        return 1


if __name__ == '__main__': raise SystemExit(main())
