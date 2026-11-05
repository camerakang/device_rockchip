#!/usr/bin/env python3
"""Synthetic full-archive pipeline, isolated volume and actual browser; no hardware."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'))
from ss07.api import Server
from ss07.config import load
from ss07.sonar_archive import Catalog,Store
from ss07.sonar_archive_worker import AsyncArchive
from ss07.sonar_core import Request,Reply,event
from ss07.state import State
from ss07.timecode_model import Mapper
from ss07.timecode_wire import Packet
from ss07.volume import DirectoryVolume,open_directory


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',required=True,type=Path)
    parser.add_argument('--chromium',required=True,type=Path)
    parser.add_argument('--node',default='node')
    args=parser.parse_args();output=args.output.absolute()
    if output.exists() or output.is_symlink():parser.error('output must not exist')
    output.mkdir(parents=True);disk=output/'synthetic-volume';disk.mkdir()
    config=load(ROOT/'config/system.example.json');config['storage'].update(
        sonar_recording_enabled=True,filesystem_uuid='synthetic-archive-volume',reserve_bytes='268435456')
    catalog=Catalog(output/'archives.sqlite');epoch=2**63+101;external=2**63+123456
    mapper=Mapper('boot-archive-test','receiver-test','synthetic_test')
    mapper.observe(Packet(epoch,1,external),1000,1000)
    mapper.observe(Packet(epoch+1,1,external+80000),12000,12000)
    class Tagger:
        def tag(self,when,basis):return mapper.tag(when,25000,basis,'boot-archive-test')
    def volume(*a,**kw):return DirectoryVolume(open_directory(disk),'synthetic-archive-volume','synthetic_test')
    archive=AsyncArchive(config['storage'],catalog.path,'boot-archive-test','collector-archive-test',Tagger(),'synthetic_test')
    server=thread=None
    with patch('ss07.sonar_archive_worker.MountLease.acquire',side_effect=volume):
        archive.start()
        try:
            for i in range(20):
                direction=('front','back','left','right','down')[i%5]
                sample=event('boot-archive-test','collector-archive-test',i+1,'MEASUREMENT',2000+i*1000,
                    'synthetic_test',Request(direction,'test-'+direction,str(i+1)),
                    Reply('test-'+direction,str(i+1),True,1234+i,None,None),'wire_id')
                archive.submit(sample)
            deadline=time.monotonic()+10
            while int(archive.status()['written_events'])<20:
                if time.monotonic()>=deadline:raise RuntimeError('archive_pipeline_timeout')
                time.sleep(.02)
            archive.close();archive.thread.join(5)
            if archive.thread.is_alive():raise RuntimeError('archive_shutdown_timeout')
            parts=catalog.list()['items'];assert sum(p['event_count'] for p in parts)==20
            assert {p['source_epoch'] for p in parts}=={str(epoch),str(epoch+1)}
            with volume() as lease:
                for part in parts:
                    fd,_=Store(catalog,lease).open(part['archive_id']);os.close(fd)
            server=Server(('127.0.0.1',0),State(config,sonar_catalog_path=catalog.path))
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            with (output/'browser.log').open('w') as log:
                subprocess.run([args.node,str(ROOT/'tools/test-browser-sonar.cjs'),str(args.chromium),
                    'http://127.0.0.1:'+str(server.server_port),str(output),str(epoch),str(external),str(external+20000)],
                    stdout=log,stderr=subprocess.STDOUT,check=True,timeout=60)
            browser=json.loads((output/'browser.json').read_text())
            report=dict(evidence_kind='synthetic_test',hardware_verified=False,result='passed',
                archive_parts=len(parts),archived_measurements=20,source_epochs=[str(epoch),str(epoch+1)],
                pipeline=archive.status(),browser=browser,deployed=False,flashed=False)
            (output/'result.json').write_text(json.dumps(report,indent=2)+'\n')
            print(json.dumps(report,indent=2))
        finally:
            archive.close()
            if server is not None:server.shutdown();server.server_close()
            if thread is not None:thread.join(3)


if __name__=='__main__':main()
