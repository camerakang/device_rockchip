"""Privileged coordinator endpoint: fixed configuration operations and peer UID only."""
import json
import os
import socket
import sqlite3
import stat
import struct
import threading
import uuid
from pathlib import Path

from .accounts import USERNAME
from .application import Application, ApplyError, SystemdBackend
from .config import decode_json
from .maintenance import MAX_MESSAGE, error
from .persistence import RevisionConflict
from .worker import Worker, monotonic_us


def dispatch(application, request):
    try:
        if (type(request) is not dict or set(request) != {'schema_version', 'request_id', 'actor', 'operation', 'payload'}
                or type(request['schema_version']) is not int or request['schema_version'] != 1
                or not isinstance(request['request_id'], str) or str(uuid.UUID(request['request_id'])) != request['request_id']):
            return error('INVALID_APPLICATION_REQUEST', 422)
        actor, operation, payload = request['actor'], request['operation'], request['payload']
        if (type(actor) is not dict or set(actor) != {'username', 'role'} or actor['role'] != 'maintainer'
                or not isinstance(actor['username'], str) or USERNAME.fullmatch(actor['username']) is None):
            return error('FORBIDDEN', 403)
        if type(payload) is not dict: return error('INVALID_APPLICATION_REQUEST', 422)
        if operation == 'config.recover':
            if set(payload) != {'expected_operation_id'}: return error('INVALID_APPLICATION_REQUEST', 422)
            if not isinstance(payload['expected_operation_id'], str): return error('INVALID_APPLICATION_REQUEST', 422)
            return {'data': application.recover(payload['expected_operation_id'])}
        if operation not in ('config.plan', 'config.apply'): return error('OPERATION_NOT_ALLOWED', 403)
        if set(payload) != {'expected_revision', 'expected_active_revision'}: return error('INVALID_APPLICATION_REQUEST', 422)
        if operation == 'config.plan':
            _, _, planned = application.preview(**payload)
            return {'data': dict(plan=planned, active_revision=payload['expected_active_revision'], revision=payload['expected_revision'])}
        return {'data': application.submit(actor=actor['username'], identity=request['request_id'], **payload)}
    except RevisionConflict: return error('REVISION_CONFLICT', 409)
    except ApplyError as exc:
        return error(str(exc).upper(), 409)
    except (OSError, RuntimeError, sqlite3.Error): return error('APPLICATION_RESULT_UNCERTAIN', 503)
    except (ValueError, TypeError, KeyError): return error('INVALID_APPLICATION_REQUEST', 422)


def run(desired, active, directory, state_dir, socket_dir, gateway, client_uid, stop, rtsp_profile=None):
    path = Path(socket_dir) / 'application.sock'
    slots = threading.BoundedSemaphore(2)
    def handle(client, application):
        identity = None
        try:
            _, uid, _ = struct.unpack('3i', client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid != client_uid: return
            client.settimeout(3)
            raw, _, flags, _ = client.recvmsg(MAX_MESSAGE + 1)
            if flags & socket.MSG_TRUNC or len(raw) > MAX_MESSAGE: return
            request = decode_json(raw, max_bytes=MAX_MESSAGE)
            if isinstance(request, dict): identity = request.get('request_id')
            value = dispatch(application, request)
            client.sendall(json.dumps(dict(request_id=identity, **value), allow_nan=False).encode())
        except (OSError, ValueError): pass
        finally: client.close(); slots.release()
    with Worker(state_dir, 'configuration', stop) as worker, socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as listener:
        application = Application(desired, active, directory, SystemdBackend(state_dir), gateway=gateway, stop=stop,
                                  rtsp_profile=rtsp_profile)
        application.recover()
        try:
            info = path.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid(): raise ApplyError('unsafe_application_socket')
            path.unlink()
        except FileNotFoundError: pass
        listener.bind(str(path)); os.chmod(str(path), 0o660)
        inode = path.stat().st_ino
        listener.listen(4); listener.settimeout(.2)
        try:
            next_publish = 0
            while not stop.is_set():
                if monotonic_us() >= next_publish:
                    state = application.status()
                    worker.emit('ERROR' if state['fatal'] else 'RUNNING', state)
                    next_publish = monotonic_us() + 1000000
                try: client, _ = listener.accept()
                except socket.timeout: continue
                if not slots.acquire(blocking=False): client.close(); continue
                threading.Thread(target=handle, args=(client, application), daemon=True).start()
        finally:
            try:
                if path.lstat().st_ino == inode: path.unlink()
            except FileNotFoundError: pass
            if application.thread is not None: application.thread.join(timeout=1)
