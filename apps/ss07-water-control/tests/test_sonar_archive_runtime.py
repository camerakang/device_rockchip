"""Synthetic archival pipeline and real HTTP; no manufacturer adapter is selected."""
import copy
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request,urlopen

from ss07.api import Server
from ss07.config import ROOT,ConfigError,load,validate
from ss07.config_runtime import plan
from ss07.sonar_archive import Catalog,Store,Writer
from ss07.sonar_archive_worker import AsyncArchive
from ss07.sonar_journal import AsyncHistory,Journal
from ss07.sonar_worker import Engine
from ss07.state import State
from ss07.timecode_model import unavailable
from ss07.volume import DirectoryVolume,VolumeError,open_directory
from test_sonar import measurement,tagged
from test_sonar_runtime import fixture,Reply


def eventually(fn,seconds=5):
    end=time.monotonic()+seconds
    while time.monotonic()<end:
        if fn():return
        time.sleep(.01)
    raise AssertionError('condition timed out')


class Tagger:
    def tag(self,when,basis):return unavailable('boot-test',when,basis,'test_no_timecode','synthetic_test')


class Environment(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.disk=self.root/'disk';self.disk.mkdir()
        self.config=load(ROOT/'config/system.example.json')
        self.config['storage'].update(sonar_recording_enabled=True,filesystem_uuid='test-volume',reserve_bytes='268435456')
        self.catalog=Catalog(self.root/'archives.sqlite')

    def volume(self,*args,**kwargs):return DirectoryVolume(open_directory(self.disk),'test-volume','synthetic_test')

    def sink(self):return AsyncArchive(self.config['storage'],self.catalog.path,'boot-test','collector-test',Tagger(),'synthetic_test')

    def close_sink(self,sink):
        sink.close();sink.thread.join(5);self.assertFalse(sink.thread.is_alive())


class PipelineTests(Environment):
    def test_rolling_diagnostic_history_does_not_truncate_full_archive(self):
        config,addresses,profile=fixture();archive=self.sink()
        history=AsyncHistory(self.root/'diagnostics.sqlite','boot-test','collector-test',Tagger(),'synthetic_test')
        with patch('ss07.sonar_archive_worker.MountLease.acquire',side_effect=self.volume),patch('ss07.sonar_journal.MAX_EVENTS',3):
            archive.start();history.start()
            try:
                engine=Engine(addresses,profile,'boot-test','collector-test',history,'synthetic_test',archive)
                engine.connection(True,0)
                now=31000
                for i in range(20):
                    request=engine.poller.next_request(now);self.assertIsNotNone(request);engine.poller.sent(now)
                    engine.poller.receive(Reply(request.address,request.request_id,True,1000+i,None,None),now+1000)
                    now+=2000
                eventually(lambda:int(archive.status()['written_events'])==21)
            finally:
                self.close_sink(archive);history.close();history.thread.join(5)
        with self.volume() as volume:
            stored=[]
            for part in self.catalog.list()['items']:
                fd,_=Store(self.catalog,volume).open(part['archive_id'],lambda i,s:stored.append(s));os.close(fd)
        self.assertEqual(len([s for s in stored if s['kind']=='MEASUREMENT']),20)
        self.assertTrue(all(s['evidence_kind']=='synthetic_test' for s in stored))
        self.assertLessEqual(Journal(history.journal.path).history()['retained_events'],3)
        self.assertEqual(archive.status()['dropped_events'],'0')

    def test_slow_disk_keeps_submit_bounded_and_persists_loss_gap(self):
        entered,release=threading.Event(),threading.Event()
        def slow_volume(*args,**kwargs):
            entered.set();release.wait(8);return self.volume()
        archive=self.sink()
        with patch('ss07.sonar_archive_worker.MountLease.acquire',side_effect=slow_volume):
            archive.start()
            try:
                archive.submit(measurement());self.assertTrue(entered.wait(3))
                start=time.monotonic()
                for i in range(2,402):archive.submit(measurement(i,received=i*1000))
                self.assertLess(time.monotonic()-start,3)
                self.assertLessEqual(archive.status()['queued_events'],256)
                self.assertGreater(int(archive.status()['dropped_events']),0)
            finally:release.set();self.close_sink(archive)
        parts=self.catalog.list()['items']
        self.assertEqual(sum(int(p['known_lost_events']) for p in parts),int(archive.status()['dropped_events']))
        self.assertTrue(any(p['contains_gap'] for p in parts))

    def test_missing_disk_never_creates_an_archive_directory(self):
        archive=self.sink()
        with patch('ss07.sonar_archive_worker.MountLease.acquire',side_effect=VolumeError('mount_missing')):
            archive.start()
            try:
                archive.submit(measurement());eventually(lambda:archive.status()['dropped_events']=='1')
                self.assertEqual(archive.status()['state'],'BLOCKED')
                self.assertEqual(list(self.disk.iterdir()),[])
            finally:self.close_sink(archive)
        self.assertEqual(self.catalog.list()['items'],[])

    def test_live_archiver_excludes_offline_maintenance_even_with_another_worker_lock(self):
        archive=self.sink()
        with patch('ss07.sonar_archive_worker.MountLease.acquire',side_effect=self.volume):
            archive.start()
            try:
                archive.submit(measurement());eventually(lambda:archive.status()['written_events']=='1')
                with self.assertRaises(BlockingIOError):
                    with self.catalog.exclusive():pass
            finally:self.close_sink(archive)
        with self.catalog.exclusive():pass

    def test_configuration_requires_uuid_and_apply_cannot_claim_missing_sonar_unit(self):
        candidate=copy.deepcopy(self.config);candidate['storage']['filesystem_uuid']=None
        with self.assertRaises(ConfigError):validate(candidate)
        validate(self.config)
        previous=load(ROOT/'config/system.example.json')
        with self.assertRaisesRegex(ConfigError,'sonar_driver_unavailable'):plan(previous,self.config)


class ArchiveHTTPTests(Environment):
    def setUp(self):
        super().setUp();self.catalog.initialize()
        with self.volume() as volume:
            writer=Writer(self.catalog,volume);writer.append([tagged(measurement(i,received=i*1000)) for i in range(1,6)])
            self.meta=writer.finish()
        self.state=State(self.config,sonar_catalog_path=self.catalog.path)
        self.server=Server(('127.0.0.1',0),self.state)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.close)
        self.base='http://127.0.0.1:'+str(self.server.server_port)
        self.path='/api/v1/sonars/archives/'+self.meta['archive_id']

    def close(self):self.server.shutdown();self.server.server_close();self.thread.join(3)

    def request(self,path,method='GET',headers=None):
        try:response=urlopen(Request(self.base+path,method=method,headers=headers or {}),timeout=4)
        except HTTPError as exc:response=exc
        with response:return response.status,dict(response.headers),response.read()

    def test_listing_and_filtered_samples_keep_original_tags_and_evidence(self):
        with patch('ss07.sonar_archive_http.MountLease.acquire',side_effect=self.volume):
            status,headers,raw=self.request('/api/v1/sonars/archives?direction=front')
            self.assertEqual(status,200);data=json.loads(raw)['data']
            self.assertTrue(data['disk_online']);self.assertEqual(data['items'][0]['evidence_kind'],'synthetic_test')
            self.assertNotIn('relative_path',data['items'][0])
            status,headers,raw=self.request(self.path+'/samples?limit=2&direction=front')
            self.assertEqual(status,200);data=json.loads(raw)['data']
            self.assertEqual([s['seq'] for s in data['items']],['1','2'])
            status,headers,raw=self.request(self.path+'/samples?cursor='+data['next_cursor'])
            self.assertEqual([s['seq'] for s in json.loads(raw)['data']['items']],['3','4','5'])

    def test_download_head_range_and_content_checksum(self):
        expected=(self.disk/self.meta['relative_path']).read_bytes()
        with patch('ss07.sonar_archive_http.MountLease.acquire',side_effect=self.volume):
            status,headers,raw=self.request(self.path+'/content?download=1')
            self.assertEqual(status,200);self.assertEqual(raw,expected)
            self.assertIn('attachment',headers['Content-Disposition'])
            status,headers,raw=self.request(self.path+'/content','HEAD')
            self.assertEqual(status,200);self.assertEqual(raw,b'');self.assertEqual(int(headers['Content-Length']),len(expected))
            status,headers,raw=self.request(self.path+'/content',headers={'Range':'bytes=7-42'})
            self.assertEqual(status,206);self.assertEqual(raw,expected[7:43])
            self.assertEqual(self.request(self.path+'/content',headers={'Range':'bytes=999999999-'})[0],416)

    def test_invalid_queries_offline_wrong_volume_and_corrupt_file_are_rejected(self):
        for suffix in ('?start_us=1','?source_epoch=0','?limit=201','?limit=1&limit=2'):
            self.assertEqual(self.request('/api/v1/sonars/archives'+suffix)[0],422)
        with patch('ss07.sonar_archive_http.MountLease.acquire',side_effect=VolumeError('mount_missing')):
            self.assertEqual(self.request(self.path+'/content')[0],409)
            data=json.loads(self.request('/api/v1/sonars/archives')[2])['data']
            self.assertFalse(data['disk_online']);self.assertEqual(data['items'][0]['availability'],'disk_offline')
        file=self.disk/self.meta['relative_path'];raw=file.read_bytes();file.write_bytes(raw.replace(b'1234',b'1235'))
        with patch('ss07.sonar_archive_http.MountLease.acquire',side_effect=self.volume):
            self.assertEqual(self.request(self.path+'/content')[0],409)
            self.assertEqual(self.request(self.path+'/samples')[0],409)


if __name__=='__main__':unittest.main()
