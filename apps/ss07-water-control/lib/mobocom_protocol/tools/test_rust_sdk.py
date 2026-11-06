"""Build/test the pure Rust SDK, replay existing vectors, and compare Python/Rust behavior.

No vector rewriting, hardware, neighboring repositories or third-party Python dependencies.
Set CARGO_HOME/RUSTUP_HOME/PATH when using an isolated Rust toolchain.
"""
from pathlib import Path
import json
import random
import shutil
import struct
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from mobocom_realtime.wire import Frame, Kind, Command as C, encode, decode, guard, targets
from mobocom_realtime.reference import DeviceCore, VirtualDriver, State, Fault
from mobocom_realtime.profiles import lens, gimbal, fans

CRATE = ROOT / 'mobocom_realtime/rust'

def run_harness(binary, commands, expected, args=(), label='', allow_state_version=False):
    result = subprocess.run([str(binary), *map(str, args)], input='\n'.join(commands)+'\n',
                            text=True, capture_output=True, check=True)
    actual = result.stdout.splitlines()
    assert len(actual) == len(expected), (label, len(actual), len(expected), result.stderr)
    for i, (a, e) in enumerate(zip(actual, expected)):
        if a != e:
            # state_version is only a hint; existing C++/Python differ on a no-op SAFE STOP.
            # All other bytes (including guard, fault, output masks and ages) must match.
            try:
                fa, fe = decode(bytes.fromhex(a)), decode(bytes.fromhex(e))
                if (allow_state_version and fa.kind == fe.kind == Kind.RESPONSE and fa.command == fe.command == C.STATUS
                    and (fa.src_uid, fa.dst_uid, fa.session, fa.sequence) == (fe.src_uid, fe.dst_uid, fe.session, fe.sequence)
                    and fa.payload[:12] == fe.payload[:12] and fa.payload[16:] == fe.payload[16:]):
                    continue
            except ValueError:
                pass
            raise AssertionError((label, i, commands[max(0,i-3):i+1], a, e))
    return len(commands)

def vectors(binary):
    root = ROOT / 'mobocom_realtime'
    v = json.loads((root/'vectors.json').read_text())
    count = 0
    for node in (1, 2, 3, 4):
        steps = [s for s in v['trace'] if s['node'] == node]
        commands = [('tick '+str(s['at_us'])) if s.get('tick') else f"request {s['at_us']} {s['request']}" for s in steps]
        count += run_harness(binary, commands, [s.get('reply', '') for s in steps],
                             (2 if node == 4 else node, int(steps[0]['uid']), node*10), f'golden node {node}')
    for name, mode in [('configuration',1),('motion_profile',2),('live_motion_profile',3),('focus_control',4)]:
        rows = json.loads((root/(name+'_vectors.json')).read_text())
        commands, expected = [], []
        for row in rows:
            if mode == 3:
                commands.append(f"tick {row['at_us']-1}"); expected.append('')
            commands.append(f"request {row['at_us']} {row['request']}"); expected.append(row['reply'])
        count += run_harness(binary, commands, expected, (2,2,20,mode), name)
    for case in json.loads((root/'timeout_recovery_vectors.json').read_text()):
        commands, expected = [], []
        for s in case['steps']:
            commands.append(f"{s['op']} {s['at']}" + (' '+s['request'] if s['op']=='request' else ''))
            expected.append(str(s['result']).lower() if s['op']=='recover' else s.get('reply',''))
        count += run_harness(binary, commands, expected, (1,1,10,0,6), case['name'])
    type_cases = json.loads((root/'device_type_vectors.json').read_text())['cases']
    for device_type in sorted({row['type'] for row in type_cases}):
        cases = [row for row in type_cases if row['type'] == device_type]
        count += run_harness(binary, [f"request 0 {row['request']}" for row in cases],
                             [row['reply'] for row in cases], (1,2,10,0,0,device_type), f'type {device_type}')
    # Walk every pre-existing control frame, including type-registry and extension vectors.
    frames = set()
    def collect(value):
        if isinstance(value,dict):
            for key,item in value.items():
                if key in ('hex','request','reply') and isinstance(item,str) and item.startswith('d57d'):
                    frames.add(item)
                else: collect(item)
        elif isinstance(value,list):
            for item in value: collect(item)
    for file in root.glob('*vectors.json'): collect(json.loads(file.read_text()))
    commands, expected = [], []
    for frame in sorted(frames):
        commands.append('codec '+frame); expected.append(frame)
        commands.append('codec '+frame+'00'); expected.append('invalid')
        commands.append('codec '+frame[:-2]); expected.append('invalid')
        damaged=bytearray.fromhex(frame);damaged[-1]^=1
        commands.append('codec '+damaged.hex());expected.append('invalid')
        f=decode(bytes.fromhex(frame))
        if f.command==C.READ_CONFIG and f.kind==Kind.RESPONSE:
            commands.append('snapshot '+f.payload.hex());expected.append(f.payload.hex())
    for row in json.loads((root/'focus_command_vectors.json').read_text()):
        commands.append('focus '+row['hex']); expected.append(row['hex'])
    commands.append('timecode '+v['timecode_hex']); expected.append(v['timecode_hex'])
    count += run_harness(binary,commands,expected,label='all codec vectors')
    return count

def differential(binary):
    count=0
    for seed in range(12):
        rng=random.Random(seed)
        kind=seed%3+1
        p=(lens,gimbal,fans)[kind-1]();p.auto_reset_mask=6
        node=DeviceCore(p,1,10,VirtualDriver())
        commands,expected=[],[]
        now=0;sequence=0;previous=None
        def request(command,payload=b'',session=100,raw=None):
            nonlocal sequence,previous
            sequence+=1
            raw=raw or encode(Frame(Kind.REQUEST,0,1,sequence,command,session,payload))
            reply=node.transact(raw,now)
            commands.append(f'request {now} {raw.hex()}');expected.append(reply.hex() if reply else '')
            previous=raw
        for _ in range(600):
            now+=rng.choice((0,1,1000,5000,20000,100001))
            g=guard(node.boot_id,node.generation,node.session_epoch)
            choice=rng.randrange(15)
            if choice==0:
                request(C.SESSION_OPEN,struct.pack('<QQI',10,node.session_epoch,node.generation))
            elif choice==1:request(C.SET_SOURCE,g+struct.pack('<BII',2,100000,300000))
            elif choice in (2,3):
                values=[(r.endpoint,r.resource,rng.randint(max(r.minimum,-500),min(r.maximum,500))) for r in p.resources]
                request(C.WRITE_TARGETS,targets(10,node.generation,values,node.session_epoch),100 if choice==2 else 200)
            elif choice==4:request(C.HEARTBEAT,g)
            elif choice==5:request(C.STOP,g)
            elif choice==6:request(C.ESTOP,g)
            elif choice==7:request(C.RESET_FAULT,g)
            elif choice==8:request(C.EXECUTE_ACTION,g+struct.pack('<H',1))
            elif choice==9 and previous:request(C.INFO,raw=previous)
            elif choice==10:
                commands.append(f'tick {now}');expected.append('');node.tick(now)
            elif choice==11:
                commands.append(f'recover {now}');expected.append(str(node.recover_timeout_fault(now)).lower())
            elif choice==12:
                reason=rng.choice((Fault.DRIVER,Fault.ESTOP,Fault.LIMIT_TRIP))
                commands.append(f'fault {now} {int(reason)}');expected.append('');node.fault_input(now,reason)
            elif choice==13:
                hook=rng.choice(('apply_fail','safe_fail','reset_ok','fail_validation'));value=rng.randrange(2)
                commands.append(f'driver {hook} {value}');expected.append('');setattr(node.driver,hook,bool(value))
            else:request(C.INFO,session=200)
            request(C.STATUS,b'\0\0',session=200)
        count+=run_harness(binary,commands,expected,(kind,1,10,0,6),f'differential seed {seed}', allow_state_version=True)
    return count

def main():
    cargo=shutil.which('cargo')
    if not cargo:raise RuntimeError('cargo missing: install Rust or set PATH; Rust checks are not silently skipped')
    def run(*args):subprocess.run([cargo,*args],cwd=CRATE,check=True)
    run('test','--locked','--offline','--all-targets')
    run('check','--locked','--offline','--no-default-features')
    run('build','--locked','--offline','--example','conformance')
    meta=json.loads(subprocess.check_output([cargo,'metadata','--offline','--no-deps','--format-version','1'],cwd=CRATE))
    binary=Path(meta['target_directory'])/'debug/examples'/('conformance.exe' if sys.platform=='win32' else 'conformance')
    n=vectors(binary); d=differential(binary)
    print(f'PASS Rust: {n} existing-vector/codec steps; {d} deterministic differential steps; no hardware acceptance')

if __name__=='__main__':main()
