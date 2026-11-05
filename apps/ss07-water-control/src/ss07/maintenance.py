"""Narrow local maintenance RPC; API never gains config or recording write access."""
import json
import os
import re
import socket
import sqlite3
import stat
import struct
import threading
import uuid
from pathlib import Path

from .accounts import USERNAME
from .audit import Audit
from .catalog import Catalog, RecordConflict, RecordStore, valid_id
from .config import ConfigError, decode_json, load, validate
from .persistence import ConfigStore, RevisionConflict
from .snapshots import u64
from .volume import MountLease, VolumeError
from .worker import Worker, monotonic_us

MAX_MESSAGE = 131072


def error(code, status):
    return dict(error=dict(code=code, status=status))


class Operations:
    def __init__(self, config_path, catalog_path, audit_path, active_path=None, application=None):
        self.path = Path(config_path)
        self.config = load(active_path or self.path)
        self.application = application
        self.catalog = Catalog(catalog_path) if catalog_path else None
        self.audit = Audit(audit_path); self.audit.initialize()

    def execute(self, request):
        try:
            if (type(request) is not dict or set(request) != {'schema_version', 'request_id', 'actor', 'operation', 'payload'}
                    or type(request['schema_version']) is not int or request['schema_version'] != 1):
                return error('INVALID_MAINTENANCE_REQUEST', 422)
            if not isinstance(request['request_id'], str): return error('INVALID_MAINTENANCE_REQUEST', 422)
            identity = str(uuid.UUID(request['request_id']))
            if request['request_id'] != identity: return error('INVALID_MAINTENANCE_REQUEST', 422)
            actor = request['actor']
            if (type(actor) is not dict or set(actor) != {'username', 'role'} or actor['role'] != 'maintainer'
                    or not isinstance(actor['username'], str) or USERNAME.fullmatch(actor['username']) is None):
                return error('FORBIDDEN', 403)
            payload, operation = request['payload'], request['operation']
            if type(payload) is not dict: return error('INVALID_MAINTENANCE_REQUEST', 422)
            if operation in ('config.plan', 'config.apply', 'config.recover'):
                if self.application is None: return error('APPLICATION_UNAVAILABLE', 503)
                self.audit.append('OPERATION_STARTED', operation_id=identity, username=actor['username'], outcome=operation)
                return {k: v for k, v in self.application.request(actor, operation, payload, request_id=identity).items() if k != 'request_id'}
            if operation == 'config.update':
                if set(payload) != {'candidate', 'expected_revision'} or not isinstance(payload['expected_revision'], str):
                    return error('CONFIG_INVALID', 422)
                if re.fullmatch('[0-9a-f]{64}', payload['expected_revision']) is None: return error('CONFIG_INVALID', 422)
                validate(payload['candidate'])
                self.audit.append('OPERATION_STARTED', operation_id=identity, username=actor['username'], outcome='config.update')
                value = ConfigStore(self.path).update(payload['candidate'], payload['expected_revision'])
                self.audit.append('CONFIG_UPDATE', operation_id=identity, username=actor['username'], revision=value, outcome='saved')
                return dict(data=dict(revision=value, persisted=True, hardware_applied=False, restart_required=True))
            if operation == 'recording.lock':
                if set(payload) != {'recording_id', 'locked', 'expected_revision'} or type(payload['locked']) is not bool:
                    return error('INVALID_RECORDING_REQUEST', 422)
                valid_id(payload['recording_id']); u64(payload['expected_revision'])
                if self.catalog is None: return error('CATALOG_UNAVAILABLE', 503)
                self.audit.append('OPERATION_STARTED', operation_id=identity, username=actor['username'], outcome='recording.lock')
                with MountLease.acquire(self.config['storage'], writable=True) as volume:
                    value = RecordStore(self.catalog, volume).set_lock(payload['recording_id'], payload['locked'], payload['expected_revision'])
                self.audit.append('RECORD_LOCK', operation_id=identity, username=actor['username'],
                                  recording_id=payload['recording_id'], locked=payload['locked'], outcome='saved')
                return dict(data=value)
            return error('OPERATION_NOT_ALLOWED', 403)
        except (RevisionConflict, RecordConflict): return error('REVISION_CONFLICT', 409)
        except KeyError: return error('RECORDING_NOT_FOUND', 404)
        except VolumeError: return error('STORAGE_UNAVAILABLE', 409)
        except (OSError, sqlite3.Error): return error('MAINTENANCE_RESULT_UNCERTAIN', 503)
        except (ValueError, TypeError): return error('INVALID_MAINTENANCE_REQUEST', 422)


class Client:
    def __init__(self, state_dir, server_uid=None, socket_name='maintenance.sock'):
        if socket_name not in ('maintenance.sock', 'application.sock'): raise ValueError('invalid RPC endpoint')
        self.path = Path(state_dir) / socket_name if state_dir else None
        self.server_uid = os.geteuid() if server_uid is None else server_uid

    def request(self, actor, operation, payload, request_id=None):
        if self.path is None or not stat.S_ISSOCK(self.path.lstat().st_mode): raise OSError('maintenance unavailable')
        identity = str(uuid.uuid4()) if request_id is None else request_id
        if not isinstance(identity, str) or str(uuid.UUID(identity)) != identity: raise ValueError('invalid RPC request identity')
        body = json.dumps(dict(schema_version=1, request_id=identity, actor=actor, operation=operation, payload=payload),
                          separators=(',', ':'), allow_nan=False).encode()
        if len(body) > MAX_MESSAGE: raise ValueError('maintenance request too large')
        with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as sock:
            sock.settimeout(3); sock.connect(str(self.path))
            _, uid, _ = struct.unpack('3i', sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid != self.server_uid: raise OSError('untrusted maintenance service')
            sock.sendall(body)
            raw, _, flags, _ = sock.recvmsg(MAX_MESSAGE + 1)
        if len(raw) > MAX_MESSAGE or flags & socket.MSG_TRUNC: raise ValueError('maintenance response too large')
        result = decode_json(raw, max_bytes=MAX_MESSAGE)
        if (type(result) is not dict or result.get('request_id') != identity
                or set(result) not in ({'request_id', 'data'}, {'request_id', 'error'})):
            raise ValueError('invalid maintenance response')
        return result


def run(config_path, state_dir, catalog_path, audit_path, api_uid, stop, active_path=None, application=None):
    operations = Operations(config_path, catalog_path, audit_path, active_path, application)
    path = Path(state_dir) / 'maintenance.sock'
    slots = threading.BoundedSemaphore(2)
    threads = set(); lock = threading.Lock()
    def handle(client):
        identity = None
        try:
            _, uid, _ = struct.unpack('3i', client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid != api_uid: return
            client.settimeout(3)
            raw, _, flags, _ = client.recvmsg(MAX_MESSAGE + 1)
            if flags & socket.MSG_TRUNC or len(raw) > MAX_MESSAGE: return
            request = decode_json(raw, max_bytes=MAX_MESSAGE)
            if isinstance(request, dict): identity = request.get('request_id')
            result = operations.execute(request)
            client.sendall(json.dumps(dict(request_id=identity, **result), allow_nan=False).encode())
        except (OSError, ValueError):
            pass
        finally:
            client.close(); slots.release()
            with lock: threads.discard(threading.current_thread())
    with Worker(state_dir, 'maintenance', stop, config=operations.config) as worker, socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as listener:
        try:
            old = path.lstat()
            if not stat.S_ISSOCK(old.st_mode) or old.st_uid != os.getuid(): raise ConfigError('unsafe maintenance socket')
            path.unlink()
        except FileNotFoundError: pass
        listener.bind(str(path)); os.chmod(str(path), 0o660)
        identity = path.stat().st_ino
        listener.listen(4); listener.settimeout(.2)
        try:
            next_snapshot = 0
            while not stop.is_set():
                if monotonic_us() >= next_snapshot:
                    worker.emit('RUNNING', dict(reason=None, operations=['config.update', 'recording.lock'] +
                                (['config.plan', 'config.apply', 'config.recover'] if application else [])))
                    next_snapshot = monotonic_us() + 1000000
                try: client, _ = listener.accept()
                except socket.timeout: continue
                if not slots.acquire(blocking=False): client.close(); continue
                thread = threading.Thread(target=handle, args=(client,), daemon=True)
                with lock: threads.add(thread)
                thread.start()
        finally:
            try:
                if path.lstat().st_ino == identity: path.unlink()
            except FileNotFoundError: pass
            with lock: pending = list(threads)
            for thread in pending: thread.join(timeout=1)
