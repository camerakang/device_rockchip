"""Dedicated read-only serial owner with bounded RPC, history and disk isolation."""
import copy
import selectors
import threading
import time

from .config import ConfigError
from .serial_receive import open_port, parameters
from .timecode_ipc import Endpoint
from .timecode_journal import AsyncJournal
from .timecode_model import Mapper
from .timecode_wire import Decoder
from .worker import Worker, monotonic_us


class Engine:
    def __init__(self, boot_id, instance, journal, evidence_kind='device_observation'):
        self.journal = journal
        self.mapper = Mapper(boot_id, instance, evidence_kind, journal.submit)
        self.decoder = Decoder()
        self.state, self.reason = 'STARTING', 'receiver_starting'
        self.serial_connected = False
        self.connections = 0

    def receive(self, raw, received_us):
        for packet, first_read in self.decoder.feed(raw, received_us):
            self.mapper.observe(packet, first_read, monotonic_us())

    def connection(self, connected, now, reason=None):
        self.serial_connected = connected
        self.reason = reason
        self.state = 'RUNNING' if connected else 'ERROR'
        if connected:
            self.connections += 1
        else:
            self.mapper.disconnect(now)
            self.decoder._discard(len(self.decoder.buffer))
        self.mapper._event('SERIAL_CONNECTED' if connected else 'SERIAL_DISCONNECTED', now, reason=reason)

    def status(self, now):
        return dict(self.mapper.status(now), read_only=True, software_tx_bytes='0',
                    serial_connected=self.serial_connected, connections=self.connections,
                    receiver_state=self.state, reason=self.reason, wire=self.decoder.counters(),
                    journal=self.journal.status())


class Publisher:
    """A stalled snapshot filesystem cannot stall tty reads or association RPC."""
    def __init__(self, worker):
        self.worker = worker
        self.lock = threading.Lock()
        self.latest = ('STARTING', {'reason': 'receiver_starting'})
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.error = None

    def start(self): self.thread.start()

    def update(self, state, payload):
        with self.lock: self.latest = (state, copy.deepcopy(payload))

    def close(self):
        self.stop.set(); self.thread.join(timeout=1)

    def _run(self):
        while not self.stop.is_set():
            with self.lock: state, payload = self.latest
            try:
                self.worker.emit(state, payload)
                self.error = None
            except (OSError, ValueError):
                self.error = 'snapshot_write_failed'
            self.stop.wait(.5)


def run(config, state_dir, journal_path, stop):
    with Worker(state_dir, 'timecode', stop, config=config) as worker:
        journal = AsyncJournal(journal_path, worker.boot, worker.instance, 'device_observation')
        engine = Engine(worker.boot, worker.instance, journal)
        publisher = Publisher(worker)
        port = endpoint = None
        journal.start(); publisher.start()
        with selectors.DefaultSelector() as selector:
            try:
                endpoint = Endpoint(state_dir, selector, engine)
                configured = bool(config['timecode']['binding']) and all(v is not None for v in config['timecode']['uart'].values())
                if configured: parameters(config['timecode']['uart'])
                else: engine.state, engine.reason = 'UNAVAILABLE', 'timecode_not_configured'
                next_open = checkpoint = 0
                delay = 1
                while not stop.is_set():
                    now = monotonic_us()
                    engine.mapper.tick(now)
                    if engine.mapper.clock_fault:
                        raise ValueError('local_clock_regressed')
                    if configured and port is None and now >= next_open:
                        try:
                            port = open_port(config)
                            selector.register(port.fd, selectors.EVENT_READ, ('serial', port.fd))
                            engine.connection(True, monotonic_us())
                        except (OSError, ValueError) as exc:
                            if port:
                                port.close(); port = None
                            reason = str(exc) if isinstance(exc, ConfigError) else 'serial_unavailable'
                            if engine.reason != reason: engine.connection(False, monotonic_us(), reason)
                            next_open = monotonic_us() + delay * 1000000
                            delay = min(30, delay * 2)
                    for key, _ in sorted(selector.select(.01), key=lambda item: item[0].data[0] != 'serial'):
                        now = monotonic_us()
                        if key.data[0] == 'serial':
                            try:
                                accepted_before = engine.mapper.accepted
                                # Bound each cycle even if a wrong device floods this port.
                                for _ in range(4):
                                    try: raw = port.read()
                                    except BlockingIOError: break
                                    if not raw: raise OSError('serial_disconnected')
                                    engine.receive(raw, monotonic_us())
                                    if len(raw) < 1024: break
                                if engine.mapper.accepted > accepted_before:
                                    delay = 1
                            except (OSError, ValueError):
                                selector.unregister(port.fd); port.close(); port = None
                                engine.connection(False, monotonic_us(), 'serial_disconnected')
                                next_open = monotonic_us() + delay * 1000000
                                delay = min(30, delay * 2)
                        else:
                            endpoint.handle(key, now)
                    now = monotonic_us()
                    endpoint.expire(now)
                    status = engine.status(now)
                    status['snapshot_error'] = publisher.error
                    publisher.update(engine.state, status)
                    if now >= checkpoint:
                        engine.mapper._event('CHECKPOINT', now, status=status)
                        checkpoint = now + 1000000
            finally:
                now = monotonic_us()
                engine.mapper.disconnect(now, 'receiver_stopped')
                if endpoint: endpoint.close()
                if port:
                    selector.unregister(port.fd); port.close()
                publisher.close(); journal.close()
