"""Shared lifecycle for independent local workers; never transports video frames."""
import fcntl
import os
import signal
import stat
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .snapshots import SERVICES, publish


def monotonic_us():
    return time.monotonic_ns() // 1000


class Worker:
    def __init__(self, directory, service, stop=None, config=None):
        if service not in SERVICES:
            raise ValueError('unknown worker')
        self.directory, self.service = Path(directory), service
        self.stop = stop or threading.Event()
        self.instance = str(uuid.uuid4())
        self.boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        self.seq = 0
        self.fd = None
        self.configuration = {}
        if config is not None:
            from .config import revision
            from .config_runtime import fingerprint
            self.configuration = dict(config_revision=revision(config), service_revision=fingerprint(config, service))

    def __enter__(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        self.fd = os.open(str(self.directory / (self.service + '.lock')),
                          os.O_RDONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o640)
        try:
            if not stat.S_ISREG(os.fstat(self.fd).st_mode):
                raise ValueError('worker lock is not a regular file')
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except Exception:
            os.close(self.fd)
            self.fd = None
            raise
        return self

    def emit(self, state, payload):
        self.seq += 1
        publish(self.directory, dict(schema_version=1, service=self.service,
            instance_id=self.instance, boot_id=self.boot, seq=str(self.seq),
            observed_monotonic_us=str(monotonic_us()), state=state,
            evidence_kind='device_observation', payload=payload, **self.configuration))

    def __exit__(self, *args):
        try:
            self.emit('STOPPED', {'reason': 'worker_stopped'})
        finally:
            if self.fd is not None:
                os.close(self.fd)
                self.fd = None


def stop_event():
    event = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: event.set())
    return event


@contextmanager
def heartbeat(directory, service, config, stop, on_error):
    """Publish API readiness only after its listening server has been constructed."""
    if directory is None:
        yield
        return
    with Worker(directory, service, stop, config=config) as worker:
        worker.emit('RUNNING', {'reason': None})
        done = threading.Event()
        def emit():
            while not done.wait(1) and not stop.is_set():
                try: worker.emit('RUNNING', {'reason': None})
                except (OSError, ValueError):
                    on_error()
                    return
        thread = threading.Thread(target=emit, daemon=True); thread.start()
        try: yield
        finally:
            done.set(); thread.join(timeout=2)


def terminate(proc):
    """Bounded shutdown including children, even after the leader has exited."""
    if getattr(proc, '_ss07_terminated', False):
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    proc.wait(timeout=2)
    proc._ss07_terminated = True
    for stream in (proc.stdout, proc.stderr):
        if stream:
            stream.close()
