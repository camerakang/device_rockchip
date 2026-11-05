#!/usr/bin/env python3
"""Verify exact package contents and run its ARM64 Python in an isolated root."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.dont_write_bytecode = True
from ss07.release_package import unpack

CHECK = r'''
import json, os, platform, signal, socket, subprocess, time
from pathlib import Path
from urllib.request import urlopen
from ss07 import __version__
from ss07.release_install import Installer
class FactoryBackend:
 def running(self): return []
 def __getattr__(self,name): raise AssertionError('unexpected factory lifecycle call')
with Installer(Path('/image'),FactoryBackend()) as install:
 factory=install.register_image(Path('/import'))
 status=install.status()
 assert status['installed_version']==__version__ and status['provisioned'] is False
with socket.socket() as sock:
 sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
config=json.loads(Path('/image/etc/ss07-active/system.json').read_text());config['api']['port']=port
for name in ('desired','active'):Path('/tmp/'+name+'.json').write_text(json.dumps(config))
argv=['/qemu-aarch64-static','/usr/bin/python3.8','-B','-m','ss07','serve','--config','/tmp/active.json',
      '--desired-config','/tmp/desired.json','--state-dir','/run/ss07']
with Path('/evidence/api.log').open('w') as log:
 proc=subprocess.Popen(argv,stdout=log,stderr=subprocess.STDOUT)
 try:
  def get(path):
   with urlopen('http://127.0.0.1:%d%s'%(port,path),timeout=2) as response:return json.load(response)['data']
  deadline=time.monotonic()+15
  while time.monotonic()<deadline:
   assert proc.poll() is None,'API exited'
   try:health=get('/healthz');break
   except OSError:time.sleep(.1)
  else:raise AssertionError('API timeout')
  assert health['app_version']==__version__ and health['hardware_ready'] is False
  config_status=get('/api/v1/config')
  assert config_status['pending_apply'] is False and config_status['mutable'] is False
  assert config_status['active_revision']==config_status['revision']==config_status['loaded_revision']
  for route in ('/api/v1/status','/api/v1/streams','/api/v1/sonars','/api/v1/timecode'):get(route)
  with urlopen('http://127.0.0.1:%d/'%port,timeout=2) as response:html=response.read()
  assert b'config-plan' in html and b'config-apply' in html and b'config-recover' in html
  proc.send_signal(signal.SIGTERM);assert proc.wait(timeout=5)==0
  assert json.loads(Path('/run/ss07/api.json').read_text())['state']=='STOPPED'
  print(json.dumps(dict(result='passed',version=__version__,runtime={'machine':platform.machine(),'python':platform.python_version()},
    factory=factory,api_health=health,dual_configuration_read=True,api_heartbeat=True,configuration_UI_assets=True,
    readonly_development_mode=True,sigterm_exit=proc.returncode,hardware_validated=False,deployed=False)))
 finally:
  if proc.poll() is None:proc.terminate()
  try:proc.wait(timeout=5)
  except subprocess.TimeoutExpired:proc.kill();proc.wait(timeout=2)
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('archive', 'system-rootfs', 'output'): parser.add_argument('--' + name, required=True, type=Path)
    parser.add_argument('--sha256', required=True)
    args = parser.parse_args(); out = args.output.absolute(); system = args.system_rootfs.resolve()
    if out.exists(): parser.error('output must be new')
    qemu = shutil.which('qemu-aarch64-static')
    if not qemu or not shutil.which('bwrap'): parser.error('QEMU and bwrap required')
    out.mkdir(parents=True)
    imported = out / 'import'; receipt = unpack(args.archive, args.sha256, imported)
    manifest = json.loads((imported / 'SS07-MANIFEST.json').read_text())
    app = imported / 'opt/ss07/releases' / receipt['version']; checked = 0
    for name in ('src', 'tools', 'web', 'config'):
        for path in (app / name).rglob('*'):
            if path.is_file():
                assert path.read_bytes() == (ROOT / path.relative_to(app)).read_bytes(), str(path)
                checked += 1
    assert not list(imported.rglob('ss07-camera-test'))
    image = out / 'image'; image.mkdir()
    for name in ('opt', 'etc', 'usr'): shutil.copytree(imported / name, image / name, symlinks=True)
    (out / 'check.py').write_text(CHECK)
    command = ['bwrap', '--unshare-user', '--uid', '0', '--gid', '0', '--unshare-pid', '--unshare-net', '--unshare-ipc',
               '--die-with-parent', '--tmpfs', '/']
    for name in ('usr', 'lib', 'bin', 'sbin', 'etc'): command += ['--ro-bind', str(system / name), '/' + name]
    command += ['--bind', str(image), '/image', '--ro-bind', str(imported), '/import', '--bind', str(out), '/evidence',
        '--proc', '/proc', '--dev', '/dev', '--tmpfs', '/tmp', '--tmpfs', '/run', '--ro-bind', qemu, '/qemu-aarch64-static',
        '--clearenv', '--setenv', 'PATH', '/usr/bin:/bin', '--setenv', 'LC_ALL', 'C.UTF-8', '--setenv', 'PYTHONHOME', '/usr',
        '--setenv', 'PYTHONPATH', '/image/opt/ss07/current/src', '--setenv', 'PYTHONDONTWRITEBYTECODE', '1',
        '/qemu-aarch64-static', '/usr/bin/python3.8', '/evidence/check.py']
    result = subprocess.run(command, capture_output=True, text=True, timeout=90)
    (out / 'stdout.log').write_text(result.stdout); (out / 'stderr.log').write_text(result.stderr)
    result.check_returncode()
    report = json.loads(result.stdout)
    report.update(evidence_kind='arm64_extracted_application_package_smoke', archive_sha256=args.sha256,
                  files_verified=len(manifest['files']), symlinks_verified=len(manifest['symlinks']),
                  runtime_files_match_workspace=checked, command=command)
    (out / 'result.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({k: v for k, v in report.items() if k != 'command'}, indent=2))


if __name__ == '__main__': main()
