"""One in-flight transaction on the same U10 stream as distance measurements."""
import os
import pwd
import socket
import stat
import struct
import time
import threading
import json
from pathlib import Path

from .config import decode_json

LIMITS = dict(thr_mv=(200,3200), blank_us=(200,10000), range_mm=(500,20000),
              sound_mps=(1400,1600), offset_mm=(-1000,1000), width=(1,16), gain_x10=(100,125,150,200))
PATHS = {'/api/v1/sonars/%d/params' % ch: ch for ch in range(1,6)}


def valid(key, value):
    if not isinstance(key,str) or key not in LIMITS or type(value) is not int:
        return False
    bounds = LIMITS[key]
    return value in bounds if key == 'gain_x10' else bounds[0] <= value <= bounds[1]


_gate = threading.Lock()


def exchange(directory, ch, change):
    # No queued NVS writes after an HTTP client has timed out. The serial
    # protocol has one untagged ACK and must finish one transaction at a time.
    if not _gate.acquire(blocking=False):
        return 409, 'SONAR_PARAMS_BUSY'
    try:
        return _exchange(directory, ch, change)
    finally:
        _gate.release()


def _exchange(directory, ch, change):
    with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as sock:
        sock.settimeout(11)
        sock.connect(str(Path(directory) / 'sonar-params.sock'))
        sock.sendall(json.dumps([ch, change, time.monotonic() + 1]).encode())
        return decode_json(sock.recv(2048), 2048)


class Control:
    def __init__(self, directory):
        self.path = Path(directory) / 'sonar-params.sock'
        self.uid = pwd.getpwnam('ss07-api').pw_uid
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        if self.path.exists():
            s = self.path.lstat()
            if not stat.S_ISSOCK(s.st_mode) or s.st_uid != os.geteuid():
                raise ValueError('unexpected_sonar_socket')
            self.path.unlink()
        self.listener.bind(str(self.path))
        os.chmod(self.path, 0o660)
        self.listener.listen(4)
        self.listener.setblocking(False)
        self.peer = None
        self.cooldown = 0

    def finish(self, status, result):
        if self.peer is not None:
            try: self.peer.send(json.dumps([status,result]).encode())
            except OSError: pass
            self.peer.close()
            self.peer = None

    def fail(self):
        self.finish(503,'SONAR_DISCONNECTED')

    def issue(self, command, phase):
        wire = (command+'\n').encode('ascii')
        if os.write(self.port.fileno(), wire) != len(wire): raise OSError('short_sonar_command')
        self.phase = phase
        self.deadline = time.monotonic()+3

    def tick(self, port):
        now=time.monotonic()
        if self.peer is not None and now >= self.deadline:
            self.finish(504,'SONAR_PARAMS_TIMEOUT')
            self.cooldown = now+3
        if self.peer is None:
            try: client,_ = self.listener.accept()
            except BlockingIOError: return
            client.setblocking(False)
            self.peer=client
            self.phase='request'
            self.deadline=now+0.5
            _,uid,_ = struct.unpack('3i',client.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
            if uid != self.uid: return self.finish(403,'FORBIDDEN')
        if self.phase != 'request': return
        try:
            ch, change, start_by = decode_json(self.peer.recv(1025),1024)
            if type(ch) is not int or not 1<=ch<=5 or (change is not None and
                (type(change) is not dict or set(change)!={'key','value'} or not valid(change['key'],change['value']))):
                raise ValueError()
        except BlockingIOError: return
        except (ValueError,TypeError,OSError): return self.finish(422,'INVALID_SONAR_PARAMS')
        if type(start_by) not in (int, float) or not now <= start_by <= now + 1:
            return self.finish(504,'SONAR_PARAMS_TIMEOUT')
        if now < self.cooldown: return self.finish(409,'SONAR_PARAMS_BUSY')
        if port is None: return self.fail()
        self.ch,self.change,self.port=ch,change,port
        self.issue('get %d'%ch,'get')

    def reply(self, msg):
        if self.peer is None or self.phase=='request': return
        if msg.get('ack') is False:
            return self.finish(422 if self.phase=='ack' else 502,'SONAR_COMMAND_REJECTED')
        if self.phase=='ack':
            if msg.get('ack') is True: self.issue('get %d'%self.ch,'verify')
            return
        if type(msg.get('params')) is not int or msg['params']!=self.ch: return
        if not all(valid(k,msg.get(k)) for k in LIMITS): return self.finish(502,'INVALID_SONAR_READBACK')
        params={k:msg[k] for k in LIMITS}
        if self.change and params[self.change['key']]!=self.change['value']:
            if self.phase=='verify': return self.finish(502,'SONAR_READBACK_MISMATCH')
            self.issue('set %d %s %d'%(self.ch,self.change['key'],self.change['value']),'ack')
        else: self.finish(200,dict(channel=self.ch,params=params))

    def close(self):
        self.fail()
        self.listener.close()
        self.path.unlink()
