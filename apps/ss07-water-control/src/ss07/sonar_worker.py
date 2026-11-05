"""Independent polling and bounded metadata publication; vendor registry is empty."""
from dataclasses import asdict
import copy
import fcntl
import os
import select

from .config import ConfigError
from .sonar_core import Poller, valid_reply
from .sonar_driver import resolve
from .sonar_journal import AsyncHistory
from .sonar_archive_worker import AsyncArchive, disabled_status
from .timecode_ipc import Client
from .timecode_worker import Publisher
from .worker import Worker, monotonic_us

MAX_READ = 1024
MAX_COMMAND = 256
MAX_REPLIES = 32


class Engine:
    def __init__(self, addresses, profile, boot, instance, history, evidence_kind='device_observation', archive=None):
        self.profile, self.history, self.archive = profile, history, archive
        self.codec = profile.codec()
        def submit(sample):
            history.submit(sample)
            if archive is not None:archive.submit(sample)
        self.poller = Poller(addresses, profile.timing, boot, instance, submit, evidence_kind)
        self.connected, self.reason, self.connections = False, 'serial_unavailable', 0

    def connection(self, connected, now, reason=None):
        self.codec.reset()
        self.poller.connection(connected, now)
        self.connected = connected
        self.reason = None if connected else (reason or 'serial_unavailable')
        if connected: self.connections += 1

    def receive(self, raw, received_us):
        if not self.connected: raise ValueError('sonar_transport_disconnected')
        if type(raw) is not bytes or not 1 <= len(raw) <= MAX_READ: raise ValueError('invalid_sonar_read')
        replies = self.codec.feed(raw)
        if type(replies) is not list or len(replies) > MAX_REPLIES: raise ValueError('unbounded_sonar_decoder')
        for reply in replies: valid_reply(reply)
        for reply in replies: self.poller.receive(reply, received_us)

    def poll(self, port, clock=monotonic_us):
        if not self.connected: return
        request = self.poller.next_request(clock())
        if request is None: return
        try:
            self.codec.reset()
            raw = self.codec.encode(request)
            if type(raw) is not bytes or not 1 <= len(raw) <= MAX_COMMAND: raise ValueError('invalid_sonar_command')
            written = port.write(raw)
            if type(written) is not int or written != len(raw): raise OSError('partial_sonar_write')
            self.poller.sent(clock())
        except (OSError, ValueError):
            self.poller.send_failed(clock())
            raise

    def status(self, now):
        self.poller.tick(now)
        return dict(reason=self.reason, protocol_id=self.profile.protocol_id, timing=asdict(self.profile.timing),
                    serial_connected=self.connected, connections=self.connections,
                    samples=copy.deepcopy(self.poller.latest),
                    last_received={k: str(v) for k, v in self.poller.last_received.items()},
                    invalidated=sorted(self.poller.invalidated), counters=dict(self.poller.counters),
                    journal=self.history.status(), archive=self.archive.status() if self.archive else disabled_status())


def run(config, state_dir, journal_path, stop, archive_path=None):
    # No production profile exists yet. Missing/unknown configuration therefore
    # only publishes real service availability; it never opens any serial port.
    with Worker(state_dir, 'sonar', stop, config=config) as worker:
        history = AsyncHistory(journal_path, worker.boot, worker.instance, Client(state_dir, worker.boot))
        publisher = Publisher(worker)
        publisher.update('STARTING', {'reason': 'sonar_starting'})
        engine = port = archive = None
        history.start(); publisher.start()
        try:
            try:
                profile, addresses = resolve(config)
                if config['storage'].get('sonar_recording_enabled',False):
                    if archive_path is None:raise ConfigError('sonar_archive_catalog_required')
                    archive=AsyncArchive(config['storage'],archive_path,worker.boot,worker.instance,Client(state_dir,worker.boot))
                    archive.start()
                engine = Engine(addresses, profile, worker.boot, worker.instance, history, archive=archive)
            except ConfigError as exc:
                reason = str(exc)
            if engine is None:
                while not stop.is_set():
                    publisher.update('UNAVAILABLE', dict(reason=reason, protocol_id=config['sonar']['vendor_protocol'],
                                     serial_connected=False, journal=history.status(),
                                     archive=disabled_status(config['storage'].get('sonar_recording_enabled',False),reason)))
                    stop.wait(.1)
                return
            next_open = checkpoint = 0
            delay = 1
            accepted_before = 0
            while not stop.is_set():
                now = monotonic_us()
                if port is None and now >= next_open:
                    try:
                        port = profile.open_transport(config)
                        fd = port.fileno()
                        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
                        if not os.isatty(fd) or flags & os.O_ACCMODE != os.O_RDWR or not flags & os.O_NONBLOCK:
                            raise ConfigError('nonblocking_sonar_tty_required')
                        engine.connection(True, monotonic_us())
                        accepted_before = engine.poller.counters['measurements']
                    except (OSError, ValueError):
                        if port is not None: port.close(); port = None
                        engine.connection(False, monotonic_us(), 'serial_open_failed')
                        next_open = monotonic_us() + delay * 1000000; delay = min(delay * 2, 30)
                if port is not None:
                    try:
                        # Drain old input before TX and never poll into a flood
                        # of old frames. Limit all work per loop iteration.
                        readable = select.select([port.fileno()], [], [], .005)[0]
                        if readable:
                            raw = port.read(MAX_READ)
                            if not raw: raise OSError('serial_disconnected')
                            engine.receive(raw, monotonic_us())
                        else: engine.poll(port)
                        if engine.poller.counters['measurements'] > accepted_before: delay = 1
                    except (OSError, ValueError):
                        port.close(); port = None
                        engine.connection(False, monotonic_us(), 'serial_io_or_decoder_failed')
                        next_open = monotonic_us() + delay * 1000000; delay = min(delay * 2, 30)
                else: stop.wait(.01)
                now = monotonic_us()
                engine.poller.tick(now)
                if now >= checkpoint:
                    publisher.update('RUNNING' if engine.connected else 'ERROR', engine.status(now))
                    checkpoint = now + 100000
        finally:
            if port is not None:
                port.close()
                engine.connection(False, monotonic_us(), 'worker_stopped')
            publisher.close(); history.close()
            if archive is not None:archive.close()
