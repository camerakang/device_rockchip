import unittest
from ss07.sonar_params import Control
from ss07.sonar_v11 import Decoder
from ss07.config_runtime import plan
from test_sonar_runtime import fixture
from copy import deepcopy


class ParamsTests(unittest.TestCase):
    def test_readback_and_interleaved_distance(self):
        p=Control.__new__(Control)
        p.ch,p.change,p.peer,p.phase=2,dict(key='thr_mv',value=901),True,'get'
        sent=[]; done=[]
        def issue(command,phase): sent.append(command); p.phase=phase
        p.issue=issue
        p.finish=lambda *args: done.append(args)
        d=Decoder();d.on_control=p.reply
        raw=b'{"params":2,"thr_mv":800,"range_mm":20000,"blank_us":1300,"sound_mps":1480,"offset_mm":0,"width":2,"gain_x10":150}\n'
        distance=b'{"v":1,"ch":2,"id":185,"ms":0,"seq":1,"state":"no_echo","mm":null,"thr_mv":800}\n'
        self.assertEqual(len(d.feed(raw+distance+b'{"ack":true}\n')),1)
        self.assertEqual(sent,['set 2 thr_mv 901','get 2'])
        self.assertEqual(done,[])
        d.feed(raw)
        self.assertEqual(done,[(502,'SONAR_READBACK_MISMATCH')])

    def test_direction_swap_restarts_sonar_and_api_only(self):
        cfg=fixture()[0]
        cfg['sonar']['vendor_protocol']='sonar_drive_v1_1'
        for i,node in enumerate(cfg['sonar']['nodes'].values(),1):node['address']=str(i)
        new=deepcopy(cfg)
        nodes=new['sonar']['nodes']
        nodes['left'],nodes['right']=nodes['right'],nodes['left']
        self.assertEqual(plan(cfg,new)['services'],['sonar','api'])

    def test_serial_transaction_success_noop_and_legacy_rejection(self):
        import os
        import socket
        import tempfile
        import time
        import json
        from unittest.mock import patch
        from types import SimpleNamespace
        tmp=tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup); directory=tmp.name
        with patch('ss07.sonar_params.pwd.getpwnam',return_value=SimpleNamespace(pw_uid=os.getuid())):
            ctl=Control(directory)
            self.addCleanup(ctl.close)
            port,device=socket.socketpair()
            self.addCleanup(port.close); self.addCleanup(device.close)
            device.settimeout(.5)
            current=dict(params=2,thr_mv=800,range_mm=20000,blank_us=1300,sound_mps=1480,offset_mm=0,width=2,gain_x10=150)
            for expected in ('changed','unchanged','legacy','expired'):
                with socket.socket(socket.AF_UNIX,socket.SOCK_SEQPACKET) as api:
                    api.settimeout(.5); api.connect(str(ctl.path))
                    api.send(json.dumps([2,dict(key='thr_mv',value=901),time.monotonic()+(1 if expected!='expired' else -1)]).encode())
                    ctl.tick(port)
                    if expected!='expired':
                        self.assertEqual(device.recv(128),b'get 2\n')
                        ctl.reply({'ack':False} if expected=='legacy' else current)
                    if expected=='changed':
                        self.assertEqual(device.recv(128),b'set 2 thr_mv 901\n')
                        ctl.reply({'ack':True})
                        self.assertEqual(device.recv(128),b'get 2\n')
                        current['thr_mv']=901
                        ctl.reply(current)
                    status,result=json.loads(api.recv(1024))
                    self.assertEqual(status,{'legacy':502,'expired':504}.get(expected,200))
                    if status==200:self.assertEqual(result['params']['thr_mv'],901)
