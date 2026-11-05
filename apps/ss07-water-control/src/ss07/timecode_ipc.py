"""Read-only local snapshot/association RPC. No serial or motion commands exist."""
import json
import os
import socket
import stat
import time
from pathlib import Path

from .config import decode_json
from .snapshots import u64
from .timecode_model import BASES, unavailable, validate_tag

MAX_RESPONSE = 16384


class Endpoint:
    def __init__(self, directory, selector, engine):
        self.path = Path(directory) / 'timecode.sock'
        self.selector, self.engine = selector, engine
        self.clients = {}
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        try:
            try:
                previous = self.path.lstat()
                if not stat.S_ISSOCK(previous.st_mode) or previous.st_uid != os.getuid():
                    raise ValueError('unsafe_timecode_socket_path')
                self.path.unlink()  # Caller holds the exclusive timecode Worker lock.
            except FileNotFoundError:
                pass
            self.socket.bind(str(self.path))
            os.chmod(str(self.path), 0o660)
            self.identity = self.path.stat().st_ino
            self.socket.listen(8)
            self.socket.setblocking(False)
            selector.register(self.socket, 1, ('listener', self.socket))
        except Exception:
            self.socket.close()
            raise

    def _close_client(self, client):
        try: self.selector.unregister(client)
        except KeyError: pass
        self.clients.pop(client, None)
        client.close()

    def handle(self, key, now):
        kind, obj = key.data
        if kind == 'listener':
            for _ in range(8):
                try: client, _ = self.socket.accept()
                except BlockingIOError: break
                client.setblocking(False)
                if len(self.clients) >= 8:
                    client.close(); continue
                self.clients[client] = now + 100000
                self.selector.register(client, 1, ('client', client))
        elif kind == 'client':
            try:
                raw, _, flags, _ = obj.recvmsg(4097)
                if flags & socket.MSG_TRUNC or len(raw) > 4096:
                    raise ValueError('request_too_large')
                request = decode_json(raw, max_bytes=4096)
                if (not isinstance(request, dict) or type(request.get('schema_version')) is not int
                        or request['schema_version'] != 1):
                    raise ValueError('invalid_timecode_request')
                if request.get('op') == 'status' and set(request) == {'schema_version', 'op'}:
                    data = self.engine.status(now)
                elif request.get('op') == 'tag' and set(request) == {'schema_version', 'op', 'rk_boot_id', 'event_us', 'timestamp_basis'}:
                    if request['timestamp_basis'] not in BASES:
                        raise ValueError('invalid_timestamp_basis')
                    data = self.engine.mapper.tag(u64(request['event_us']), now, request['timestamp_basis'], request['rk_boot_id'])
                else:
                    raise ValueError('operation_not_allowed')
                result = dict(schema_version=1, rk_boot_id=self.engine.mapper.boot_id,
                    receiver_instance_id=self.engine.mapper.instance, observed_monotonic_us=str(now),
                    evidence_kind=self.engine.mapper.evidence_kind, data=data)
                encoded = json.dumps(result, allow_nan=False, separators=(',', ':')).encode()
                if len(encoded) > MAX_RESPONSE: raise ValueError('response_too_large')
                obj.send(encoded)
            except (OSError, ValueError, TypeError, KeyError):
                pass
            finally:
                self._close_client(obj)

    def expire(self, now):
        for client, deadline in list(self.clients.items()):
            if now >= deadline: self._close_client(client)

    def close(self):
        for client in list(self.clients): self._close_client(client)
        self.selector.unregister(self.socket)
        self.socket.close()
        try:
            if self.path.lstat().st_ino == self.identity: self.path.unlink()
        except FileNotFoundError:
            pass


class Client:
    def __init__(self, directory, boot_id, evidence_kind='device_observation'):
        self.path = Path(directory) / 'timecode.sock' if directory else None
        self.boot_id, self.evidence = boot_id, evidence_kind

    def request(self, request):
        if self.path is None: raise OSError('timecode_not_connected')
        info = self.path.lstat()
        if not stat.S_ISSOCK(info.st_mode): raise OSError('invalid_timecode_socket')
        with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as client:
            client.settimeout(.1)
            client.connect(str(self.path))
            client.sendall(json.dumps(dict(schema_version=1, **request), separators=(',', ':')).encode())
            raw, _, flags, _ = client.recvmsg(MAX_RESPONSE + 1)
        if flags & socket.MSG_TRUNC or len(raw) > MAX_RESPONSE: raise ValueError('timecode_response_too_large')
        result = decode_json(raw, max_bytes=MAX_RESPONSE)
        now = time.monotonic_ns() // 1000
        keys = {'schema_version', 'rk_boot_id', 'receiver_instance_id', 'observed_monotonic_us', 'evidence_kind', 'data'}
        if (not isinstance(result, dict) or set(result) != keys or type(result['schema_version']) is not int
                or result['schema_version'] != 1 or result['rk_boot_id'] != self.boot_id
                or result['evidence_kind'] != self.evidence or not 0 <= now-u64(result['observed_monotonic_us']) < 100000):
            raise ValueError('invalid_timecode_response')
        return result

    def tag(self, event_us, basis):
        if event_us is None:
            return unavailable(self.boot_id, None, basis, 'event_time_missing', self.evidence)
        try:
            response = self.request(dict(op='tag', rk_boot_id=self.boot_id, event_us=str(event_us), timestamp_basis=basis))
            tag = validate_tag(response['data'])
            if (tag['rk_boot_id'] != self.boot_id or tag['local_monotonic_us'] != str(event_us)
                    or tag['timestamp_basis'] != basis or tag['evidence_kind'] != self.evidence
                    or tag['receiver_instance_id'] != response['receiver_instance_id']):
                raise ValueError('tag_identity_mismatch')
            return tag
        except (OSError, ValueError, KeyError, TypeError):
            return unavailable(self.boot_id, event_us, basis, 'timecode_service_unavailable', self.evidence)
