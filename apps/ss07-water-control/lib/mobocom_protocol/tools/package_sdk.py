"""Deterministic V2 SDK snapshot; validates fixed manifests before updating consumers."""
from pathlib import Path
import argparse
import hashlib
import json
import subprocess
import sys
import zipfile

ROOT=Path(__file__).resolve().parents[1]
VERSION='2.2.0-network.1'
REPOSITORY='git@codeup.aliyun.com:629f6370487c500c27f60603/device_protocol.git'
# 升级时要先校验消费工程里已受管的旧快照，因此verify接受历史线版本；新包只发布0x22。
MANAGED_PROTOCOLS=('0x20','0x21','0x22')
def sha(data):return hashlib.sha256(data).hexdigest()
def contents():
    # 发布前只检查而不自动重新生成：防止把过期常量/向量静默装入消费工程。
    # 注释同样纳入文件摘要，因此生成后的SDK需重新打包才能得到一致清单。
    subprocess.run([sys.executable,str(ROOT/'tools/generate_device_types.py'),'--check'],check=True,cwd=ROOT)
    paths=[ROOT/name for name in ('README.md','CMakeLists.txt','library.json','AGENTS.md','SKILL.md')]
    for directory in ('mobocom_realtime','timecode','docs'):
        paths.extend(p for p in (ROOT/directory).rglob('*') if p.is_file() and not any(part in ('build','target','node_modules','__pycache__') for part in p.relative_to(ROOT).parts))
    paths.extend((ROOT/'tools').glob('*.py'))
    files={p.relative_to(ROOT).as_posix():p.read_bytes().replace(b'\r\n',b'\n') for p in paths if p.suffix in ('.py','.md','.hpp','.cpp','.ts','.rs','.toml','.json','.txt') or p.name == 'Cargo.lock'}
    hashes={name:sha(data) for name,data in sorted(files.items())}
    digest=sha(json.dumps(hashes,sort_keys=True,separators=(',',':')).encode())
    commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    dirty=bool(subprocess.check_output(['git','status','--porcelain'],cwd=ROOT,text=True))
    manifest={'schema':2,'version':VERSION,'protocol':'0x22','timecode_protocol':'0x12','status':'implementation-candidate','repository':REPOSITORY,'source_commit':commit,'source_dirty':dirty,'content_sha256':digest,'files':hashes}
    files['sdk-manifest.json']=(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n').encode()
    return files,manifest
def verify(folder):
    folder=folder.resolve();m=json.loads((folder/'sdk-manifest.json').read_text(encoding='utf-8'))
    # 安装V2.2前仍需验证已受管的V2.0/V2.1快照；不能跳过旧内容完整性检查。
    if m['schema']!=2 or m['protocol'] not in MANAGED_PROTOCOLS:raise ValueError('not a managed V2 SDK')
    for name,digest in m['files'].items():
        path=(folder/name).resolve()
        if not path.is_relative_to(folder) or not path.is_file() or sha(path.read_bytes())!=digest:raise ValueError('SDK file changed or invalid: '+name)
    digest=sha(json.dumps(m['files'],sort_keys=True,separators=(',',':')).encode())
    if digest!=m['content_sha256']:raise ValueError('aggregate mismatch')
    lock=folder.parents[1]/'mobocom-sdk.lock.json'
    if lock.exists():
        pin=json.loads(lock.read_text(encoding='utf-8'))
        if any(pin[k]!=m[k] for k in ('version','protocol','content_sha256')):raise ValueError('lock mismatch')
    return m
def install(project,files,m):
    project=project.resolve();lock_path=project/'mobocom-sdk.lock.json'
    relative=json.loads(lock_path.read_text(encoding='utf-8'))['path'] if lock_path.exists() else 'lib/mobocom_protocol'
    folder=(project/relative).resolve()
    # 仅允许项目 lib 下的单个受管目录；不能跟随绝对路径、父目录或指向项目外的链接。
    if Path(relative).is_absolute() or folder.parent!=(project/'lib').resolve() or not folder.is_relative_to(project):
        raise ValueError('invalid SDK install path')
    if folder.exists() and any(folder.iterdir()) and not (folder/'sdk-manifest.json').exists():raise ValueError('refuse to overwrite unmanaged SDK directory')
    folder.mkdir(parents=True,exist_ok=True)
    old=verify(folder)['files'] if (folder/'sdk-manifest.json').exists() else {}
    for name,data in files.items():
        path=(folder/name).resolve()
        if not path.is_relative_to(folder):raise ValueError('destination escape')
        path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
    for name in old.keys()-files.keys():
        path=(folder/name).resolve()
        if not path.is_relative_to(folder):raise ValueError('old manifest escape')
        path.unlink(missing_ok=True)
    lock={k:m[k] for k in ('schema','version','protocol','timecode_protocol','content_sha256','source_commit','source_dirty')}
    lock.update({'path':folder.relative_to(project).as_posix(),'source':REPOSITORY})
    (project/'mobocom-sdk.lock.json').write_text(json.dumps(lock,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
def main():
    parser=argparse.ArgumentParser();parser.add_argument('--install',type=Path,action='append',default=[]);parser.add_argument('--verify',type=Path);parser.add_argument('--output',type=Path);args=parser.parse_args()
    if args.verify:
        m=verify(args.verify);print('VERIFIED',m['version'],m['content_sha256']);return
    files,m=contents()
    for project in args.install:install(project,files,m);print('INSTALLED',project.resolve(),VERSION)
    output=args.output or ROOT/'release'/f'mobocom-sdk-{VERSION}.zip';output.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(output,'w',compression=zipfile.ZIP_DEFLATED) as archive:
        for name,data in sorted(files.items()):
            entry=zipfile.ZipInfo(name,(2026,1,1,0,0,0));entry.compress_type=zipfile.ZIP_DEFLATED;archive.writestr(entry,data)
    print('PACKAGE',output,'sha256='+sha(output.read_bytes()),'source_dirty='+str(m['source_dirty']))
if __name__=='__main__':main()
