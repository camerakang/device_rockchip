"""Verify packaged SDK outside the repository with no sibling firmware/web dependencies."""
from pathlib import Path
import tempfile
import subprocess
import shutil
import sys
import json
import argparse

ROOT=Path(__file__).resolve().parents[1]
def run(command,cwd):
    p=subprocess.run(command,cwd=cwd,text=True,encoding='utf-8',errors='replace',stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
    if p.returncode:raise RuntimeError(' '.join(map(str,command))+'\n'+p.stdout)
    print(p.stdout[-1800:])
def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--rust-only', action='store_true', help='explicit Rust-only isolation check; not a full SDK acceptance')
    args=parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='mobocom-v2-isolated-') as temporary:
        project=Path(temporary)/'consumer'
        run([sys.executable,str(ROOT/'tools/package_sdk.py'),'--install',str(project),'--output',str(Path(temporary)/'sdk.zip')],ROOT)
        sdk=project/'lib/mobocom_protocol'
        run([sys.executable,str(ROOT/'tools/package_sdk.py'),'--verify',str(sdk)],ROOT)
        manifest=json.loads((sdk/'sdk-manifest.json').read_text(encoding='utf-8'))
        assert not any('mobocom_timeline_v1_2' in name for name in manifest['files'])
        if not args.rust_only:
            command=['cmake','-S',str(sdk),'-B',str(project/'build')]
            if sys.platform=='win32':command+=['-G','MinGW Makefiles']
            run(command,project);run(['cmake','--build',str(project/'build')],project)
            run(['ctest','--test-dir',str(project/'build'),'--output-on-failure'],project)
            ts=sdk/'mobocom_realtime/typescript';npm=shutil.which('npm.cmd') or shutil.which('npm')
            if not npm:raise RuntimeError('npm missing')
            run([npm,'ci','--offline','--ignore-scripts','--no-audit','--no-fund'],ts)
            run([npm,'test'],ts)
        # Rust crate, test fixtures and Cargo.lock must work outside the source checkout.
        run([sys.executable, str(sdk/'tools/test_rust_sdk.py')], sdk)
        assert not any('/target/' in name for name in manifest['files'])
        for name in ('Cargo.toml', 'Cargo.lock', 'src/lib.rs', 'tests/sdk.rs'):
            assert 'mobocom_realtime/rust/' + name in manifest['files']
        rust_path=sdk/'mobocom_realtime/rust/src/wire.rs'
        original=rust_path.read_bytes();rust_path.write_bytes(original+b'\n// modified\n')
        p=subprocess.run([sys.executable,str(ROOT/'tools/package_sdk.py'),'--verify',str(sdk)],capture_output=True)
        if p.returncode==0:raise RuntimeError('modified Rust vendor not rejected')
        rust_path.write_bytes(original)
        # Detect modifications after package generation, rather than silently accepting vendor edits.
        path=sdk/'mobocom_realtime/cpp/mobocom_wire.hpp';path.write_bytes(path.read_bytes()+b'\n// modified\n')
        p=subprocess.run([sys.executable,str(ROOT/'tools/package_sdk.py'),'--verify',str(sdk)],capture_output=True)
        if p.returncode==0:raise RuntimeError('modified vendor not rejected')
        scope='Rust-only (not full SDK acceptance)' if args.rust_only else 'C++/Python/TypeScript/Rust SDK'
        print(f'PASS isolated {scope}, fixed manifest/lock, tamper rejection; no sibling projects')
if __name__=='__main__':main()
