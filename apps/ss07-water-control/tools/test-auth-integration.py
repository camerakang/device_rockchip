#!/usr/bin/env python3
"""Local HTTPS/roles/maintenance with six native synthetic streams and recording."""
import argparse
import base64
import hashlib
import http.client
import importlib.util
import json
import os
import secrets
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from ss07.accounts import password_record
from ss07.api import Server
from ss07.audit import Audit
from ss07.auth import Auth
from ss07.catalog import Catalog
from ss07.maintenance import Client, run as maintenance_run
from ss07.media import CHANNELS, MediaObserver, get_paths, render, rtp_port, settings
from ss07.media_sessions import media_call
from ss07.persistence import atomic_json
from ss07.recorder import RecorderSession, verify_binary
from ss07.state import State
from ss07.volume import DirectoryVolume, open_directory
from ss07.worker import terminate

spec=importlib.util.spec_from_file_location('media_fixture',ROOT/'tools/test-media.py')
fixture=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixture)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('camera-test','mediamtx','caddy','chromium','output'):parser.add_argument('--'+name,type=Path,required=True)
    args=parser.parse_args()
    if args.output.exists():parser.error('output must be new')
    args.output.mkdir(parents=True)
    verify_binary(args.mediamtx)
    evidence=dict(evidence_kind='synthetic_test',hardware_verified=False,result='failed',
                  observed_at_utc=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),
                  tls='ephemeral self-signed test certificate; Chromium SPKI exception only; no system trust change')
    procs=[];files=[];broker_stop=threading.Event();recorder_stop=threading.Event()
    config,upstream=fixture.configuration(),fixture.configuration()
    config['storage']['segment_seconds']=2
    config['sdi']['rtsp_url']='rtsp://127.0.0.1:%d/front'%upstream['media']['rtsp_port']
    root=args.output/'recordings';root.mkdir()
    volume=DirectoryVolume(open_directory(root),'auth-integration-fixture','synthetic_test')
    catalog=Catalog(args.output/'catalog.sqlite');catalog.initialize()
    def start(name,argv,env=None):
        log=open(args.output/(name+'.log'),'wb');files.append(log)
        proc=subprocess.Popen([str(a) for a in argv],stdout=log,stderr=subprocess.STDOUT,start_new_session=True,env=env)
        procs.append(proc);return proc
    def counts():return Counter(item['channel_id'] for item in catalog.list(limit=200)['items'] if item['complete'])
    def media_sessions():
        status,raw=media_call(settings(config)['api_port'],'GET','/v3/webrtc/sessions/list?itemsPerPage=128')
        assert status==200
        return json.loads(raw)['items']
    broker=record_thread=None;record_errors=[]
    try:
        with tempfile.TemporaryDirectory(prefix='ss07-auth-test-') as tmp:
            runtime=Path(tmp);config_path=runtime/'system.json';atomic_json(config_path,config,mode=0o640)
            subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(runtime/'key.pem'),
                '-out',str(runtime/'cert.pem'),'-days','1','-subj','/CN=127.0.0.1','-addext','subjectAltName=IP:127.0.0.1'],check=True,capture_output=True)
            pub=subprocess.run(['openssl','x509','-in',str(runtime/'cert.pem'),'-pubkey','-noout'],check=True,capture_output=True).stdout
            der=subprocess.run(['openssl','pkey','-pubin','-outform','DER'],input=pub,check=True,capture_output=True).stdout
            spki=base64.b64encode(hashlib.sha256(der).digest()).decode()
            gateway=dict(schema_version=1,public_origin='https://127.0.0.1:%d'%fixture.port(),listen_ip='127.0.0.1',
                         ice_ip='127.0.0.1',certificate=str(runtime/'cert.pem'),private_key=str(runtime/'key.pem'))
            gateway_path=runtime/'gateway.json';atomic_json(gateway_path,gateway)
            password=secrets.token_urlsafe(24);record=password_record(password)
            accounts=runtime/'accounts.json'
            atomic_json(accounts,dict(schema_version=1,users={name:dict(role=role,enabled=True,password=record)
                for name,role in [('admin','maintainer'),('viewer','monitor'),('reader','reader')]}),mode=0o640)
            access=Audit(args.output/'access.sqlite');access.initialize()
            auth=Auth(accounts,access,gateway['public_origin'])
            up=render(upstream);up['paths']={'front':up['paths']['front']}
            for name,value in [('upstream',up),('main',render(config,gateway))]:
                path=runtime/(name+'.json');path.write_text(json.dumps(value));start(name,[args.mediamtx,path])
            fixture.wait_for(lambda:get_paths(config))
            for channel in CHANNELS:
                port=rtp_port(upstream,'front') if channel=='sdi' else rtp_port(config,channel)
                start('source-'+channel,[args.camera_test,'--device','synthetic-test','--width','640','--height','360',
                    '--fps-num','25','--fps-den','1','--port',port])
            observer=MediaObserver()
            fixture.wait_for(lambda:all(v['ready'] for v in observer.observe(get_paths(config))['streams'].values()))
            def lease(*args,**kwargs):return DirectoryVolume(open_directory(root),'auth-integration-fixture','synthetic_test')
            # Test-only injection. Production has no switch permitting a directory as a data disk.
            with patch('ss07.volume.MountLease.acquire',side_effect=lease), \
                    RecorderSession(config,catalog,volume,args.mediamtx,'auth-integration-boot') as recorder:
                def tick():
                    try:
                        while not recorder_stop.is_set():recorder.tick()
                    except Exception as exc:record_errors.append(repr(exc))
                record_thread=threading.Thread(target=tick,daemon=True);record_thread.start()
                fixture.wait_for(lambda:all(counts()[c]>=2 for c in CHANNELS),seconds=20)
                baseline=counts()
                broker=threading.Thread(target=maintenance_run,args=(config_path,runtime/'state',catalog.path,
                    args.output/'maintenance.sqlite',os.geteuid(),broker_stop),daemon=True)
                broker.start();fixture.wait_for(lambda:(runtime/'state/maintenance.sock').exists())
                with Server(('127.0.0.1',config['api']['port']),State(config,runtime/'state',config_path,catalog.path),
                            auth,Client(runtime/'state'),Audit(args.output/'maintenance.sqlite'),runtime/'loaded-media.json') as server:
                    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
                    try:
                        env=dict(os.environ,PYTHONPATH=str(ROOT/'src'),XDG_DATA_HOME=str(runtime/'caddy-data'),XDG_CONFIG_HOME=str(runtime/'caddy-config'))
                        gateway_proc=start('https',[sys.executable,'-m','ss07','gateway-worker','--config',config_path,
                            '--gateway-config',gateway_path,'--binary',args.caddy],env=env)
                        tls=ssl.create_default_context(cafile=str(runtime/'cert.pem'))
                        port=int(gateway['public_origin'].rsplit(':',1)[1])
                        def health():
                            conn=http.client.HTTPSConnection('127.0.0.1',port,context=tls,timeout=2)
                            try:
                                conn.request('GET','/healthz');response=conn.getresponse()
                                return response.status==200 and json.loads(response.read())['data']['hardware_ready'] is False
                            finally:conn.close()
                        fixture.wait_for(health)
                        exchange=runtime/'exchange';exchange.mkdir()
                        browser=start('browser',['node',ROOT/'tools/test-browser-auth.cjs',args.chromium,gateway['public_origin'],args.output,exchange],
                            env=dict(os.environ,SS07_TEST_PASSWORD=password,SS07_TEST_SPKI=spki))
                        deadline=time.monotonic()+90;handled=set()
                        while browser.poll() is None and time.monotonic()<deadline:
                            for phase in ('logout','orphan'):
                                if phase in handled or not (exchange/(phase+'.request')).exists():continue
                                if phase=='orphan':
                                    assert len(media_sessions())==6
                                    # The desired file is deliberately invalid; cleanup must use the loaded port.
                                    bogus=runtime/'pending-invalid.json';bogus.write_text('{}')
                                    subprocess.run([sys.executable,'-m','ss07','close-webrtc','--config',str(bogus),
                                        '--cleanup-state',str(runtime/'loaded-media.json')],env=env,check=True,
                                        capture_output=True,timeout=12)
                                fixture.wait_for(lambda:not media_sessions(),seconds=8)
                                before=counts()
                                fixture.wait_for(lambda:all(counts()[c]>before[c] for c in CHANNELS),seconds=8)
                                evidence[phase+'_revocation']=dict(native_media_sessions=0,six_recorders_continue=True)
                                (exchange/(phase+'.response')).write_text('verified');handled.add(phase)
                            time.sleep(.1)
                        if browser.poll() is None:raise AssertionError('browser integration timeout')
                        assert browser.returncode==0,(args.output/'browser.log').read_text()
                        assert handled=={'logout','orphan'}
                        assert all(counts()[c]>baseline[c] for c in CHANNELS)
                        assert not record_errors,record_errors
                        evidence['browser']=json.loads((args.output/'browser-auth.json').read_text())
                        evidence['completed_counts']=dict(counts())
                        evidence['loaded_vs_persisted_config']='passed'
                        assert json.loads(config_path.read_text())['device_id']=='ss07-auth-integration'
                        assert server.state.config['device_id']!=json.loads(config_path.read_text())['device_id']
                        evidence['result']='passed'
                    finally:server.shutdown();thread.join(5)
                recorder_stop.set();record_thread.join(3)
    finally:
        recorder_stop.set();broker_stop.set()
        if record_thread:record_thread.join(3)
        if broker:broker.join(4)
        for proc in reversed(procs):terminate(proc)
        for stream in files:stream.close()
        volume.close()
        (args.output/'result.json').write_text(json.dumps(evidence,indent=2)+'\n')
    print(json.dumps(evidence,indent=2))


if __name__=='__main__':main()
