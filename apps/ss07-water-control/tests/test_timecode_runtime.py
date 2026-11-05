import errno
import fcntl
import json
import os
import pty
import select
import selectors
import socket
import sqlite3
import tempfile
import termios
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import urlopen

from ss07.api import Server
from ss07.config import ROOT, ConfigError, load
from ss07.serial_receive import ReceivePort, identify, open_port, parameters
from ss07.state import State
from ss07.timecode_ipc import Client, Endpoint
from ss07.timecode_journal import AsyncJournal, Journal, MAX_BYTES, event_json
from ss07.timecode_model import Mapper
from ss07.timecode_status import inspect
from ss07.timecode_wire import Packet
from ss07.timecode_worker import Engine
from ss07.worker import monotonic_us
from test_timecode import wire

UART = dict(baud=500000, data_bits=8, parity='none', stop_bits=1)


def until(check, seconds=3):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            value = check()
            if value: return value
        except (OSError, ValueError, sqlite3.Error):
            pass
        time.sleep(.01)
    raise AssertionError('condition timed out')


class ReceiveTests(unittest.TestCase):
    def test_pty_binary_receive_no_echo_no_flow_control_and_no_writable_handle(self):
        master, slave = pty.openpty()
        fd = os.open(os.ttyname(slave), os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK)
        os.close(slave)
        try:
            with ReceivePort(fd, UART) as port:
                self.assertEqual(fcntl.fcntl(port.fd, fcntl.F_GETFL) & os.O_ACCMODE, os.O_RDONLY)
                attributes = termios.tcgetattr(port.fd)
                self.assertFalse(attributes[0] & (termios.IXON | termios.IXOFF | termios.IXANY))
                self.assertFalse(attributes[3] & termios.ECHO)
                self.assertEqual(attributes[4:6], [termios.B500000, termios.B500000])
                raw = bytes(range(256)) * 4
                os.write(master, raw)
                until(lambda: select.select([port.fd], [], [], 0)[0])
                received = port.read()
                while len(received) < len(raw):
                    until(lambda: select.select([port.fd], [], [], 0)[0]); received += port.read()
                self.assertEqual(received, raw)
                with self.assertRaises(BlockingIOError): port.read()
                self.assertFalse(select.select([master], [], [], .03)[0], 'tty echoed or transmitted bytes')
                with self.assertRaises(OSError) as error: os.write(port.fd, b'forbidden')
                self.assertEqual(error.exception.errno, errno.EBADF)
        finally:
            os.close(master)

    def test_unconfigured_and_non_usb_paths_do_not_open_any_port(self):
        config = load(ROOT / 'config/system.example.json')
        with patch('ss07.serial_receive.os.open') as opened:
            with self.assertRaises(ConfigError): open_port(config)
            opened.assert_not_called()
        config['timecode'].update(binding='/dev/serial/by-path/fixture', uart=UART)
        with patch('ss07.serial_receive.Path.resolve', return_value=Path('/dev/pts/1')), patch('ss07.serial_receive.os.open') as opened:
            with self.assertRaisesRegex(ConfigError, 'requires_usb_serial'): open_port(config)
            opened.assert_not_called()
        with self.assertRaises(ConfigError): parameters(dict(UART, data_bits=7))
        with self.assertRaises(ConfigError): parameters(dict(UART, parity=None))
        with self.assertRaises(ConfigError): parameters(dict(UART, baud=123456))


class RuntimeFixture:
    """Explicit PTY fixture. No production binding bypass or fake hardware snapshot."""
    def __init__(self, root, socket_dir=None):
        self.root = Path(root)
        self.boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        self.journal = AsyncJournal(self.root / 'timecode.sqlite', self.boot, 'test-instance', 'synthetic_test')
        self.engine = Engine(self.boot, 'test-instance', self.journal, 'synthetic_test')
        self.master, slave = pty.openpty()
        self.port = ReceivePort(os.open(os.ttyname(slave), os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK), UART)
        os.close(slave)
        self.selector = selectors.DefaultSelector()
        self.endpoint = Endpoint(socket_dir or self.root, self.selector, self.engine)
        self.selector.register(self.port.fd, selectors.EVENT_READ, ('serial', self.port.fd))
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.errors = []
        self.client = Client(socket_dir or self.root, self.boot, 'synthetic_test')

    def _run(self):
        try:
            self.engine.connection(True, monotonic_us())
            while not self.stop.is_set():
                for key, _ in self.selector.select(.005):
                    if key.data[0] == 'serial':
                        self.engine.receive(self.port.read(), monotonic_us())
                    else:
                        self.endpoint.handle(key, monotonic_us())
                self.endpoint.expire(monotonic_us())
                self.engine.mapper.tick(monotonic_us())
        except Exception as exc:
            self.errors.append(repr(exc))

    def __enter__(self):
        self.journal.start(); self.thread.start()
        return self

    def status(self): return self.client.request({'op': 'status'})['data']

    def feed(self, epoch, seq, source):
        previous = int(self.status()['accepted_samples'])
        os.write(self.master, wire(epoch, seq, source))
        return until(lambda: self.status() if int(self.status()['accepted_samples']) > previous else None)

    def __exit__(self, *args):
        self.stop.set(); self.thread.join(timeout=2)
        self.endpoint.close(); self.selector.close(); self.port.close(); os.close(self.master)
        self.journal.close()
        if self.errors and args[0] is None: raise AssertionError(self.errors)


class RuntimeTests(unittest.TestCase):
    def test_initial_journal_failure_retries_without_stalling_receive(self):
        original = Journal.initialize
        attempts = []
        def transient(journal):
            attempts.append(1)
            if len(attempts) == 1: raise OSError('injected unavailable filesystem')
            return original(journal)
        with tempfile.TemporaryDirectory() as tmp, patch.object(Journal, 'initialize', transient):
            with RuntimeFixture(tmp) as fixture:
                until(lambda: fixture.journal.status()['error'] == 'journal_unavailable')
                self.assertEqual(fixture.feed(1, 0, 0)['tag']['quality'], 'ESTIMATED')
                until(lambda: fixture.journal.status()['last_committed_monotonic_us'])
                self.assertIsNone(fixture.journal.status()['error'])
                self.assertGreaterEqual(len(attempts), 2)
                self.assertGreaterEqual(len(fixture.journal.journal.history()['items']), 2)

    def test_pty_rpc_history_old_event_and_synthetic_rejection(self):
        with tempfile.TemporaryDirectory() as tmp, RuntimeFixture(tmp) as fixture:
            first = fixture.feed(11, 0, 100000)
            event_us = int(first['tag']['source_received_monotonic_us'])
            tag = fixture.client.tag(event_us, 'segment_create_notification')
            self.assertEqual(tag['source_epoch'], '11')
            self.assertEqual(tag['estimated_time_us'], '100000')
            fixture.feed(12, 0, 1)
            queued = fixture.client.tag(event_us, 'segment_create_notification')
            self.assertEqual(queued['mapping_id'], tag['mapping_id'])
            self.assertEqual(queued['source_epoch'], '11')
            real = Client(tmp, fixture.boot).tag(event_us, 'segment_create_notification')
            self.assertEqual(real['quality'], 'UNAVAILABLE')
            self.assertEqual(real['reason'], 'timecode_service_unavailable')
            until(lambda: len(fixture.journal.journal.history()['items']) >= 4)
            history = fixture.journal.journal.history(limit=2)
            self.assertIsNotNone(history['next_cursor'])
            self.assertEqual(len(fixture.journal.journal.history(cursor=history['next_cursor'])['items']), 2)
            self.assertFalse(select.select([fixture.master], [], [], .03)[0])
            with self.assertRaises(ValueError): fixture.client.request({'op': 'send'})
            self.assertFalse(select.select([fixture.master], [], [], .03)[0])

    def test_stalled_journal_has_bounded_loss_without_stalling_receive_or_rpc(self):
        blocked, release = threading.Event(), threading.Event()
        def stall(*_):
            blocked.set(); release.wait(timeout=3)
            raise OSError('injected slow/full disk')
        with tempfile.TemporaryDirectory() as tmp, patch.object(Journal, 'append', side_effect=stall):
            with RuntimeFixture(tmp) as fixture:
                try:
                    self.assertTrue(blocked.wait(timeout=2))
                    for _ in range(300):
                        fixture.journal.submit(dict(schema_version=1, kind='CHECKPOINT', rk_boot_id=fixture.boot,
                            receiver_instance_id='test-instance', observed_monotonic_us=str(monotonic_us()), evidence_kind='synthetic_test'))
                    status = fixture.feed(1, 0, 0)
                    self.assertGreater(int(status['journal']['dropped_events']), 0)
                    self.assertLessEqual(status['journal']['queued_events'], 128)
                    self.assertEqual(status['tag']['quality'], 'ESTIMATED')
                    self.assertFalse(select.select([fixture.master], [], [], .02)[0])
                finally:
                    release.set()

    def test_partial_rpc_clients_expire_without_stalling_serial(self):
        with tempfile.TemporaryDirectory() as tmp, RuntimeFixture(tmp) as fixture:
            clients = []
            try:
                for _ in range(8):
                    client = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
                    client.connect(str(Path(tmp) / 'timecode.sock')); clients.append(client)
                os.write(fixture.master, wire(1, 0, 0))
                until(lambda: int(fixture.status()['accepted_samples']) == 1)
                self.assertFalse(select.select([fixture.master], [], [], .02)[0])
            finally:
                for client in clients: client.close()


class StatusAndJournalTests(unittest.TestCase):
    def test_history_http_bounds_and_unavailable_journal(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = Journal(Path(tmp) / 'journal.sqlite'); journal.initialize()
            journal.append([self.event(1), self.event(2)])
            state = State(load(ROOT / 'config/system.example.json'), timecode_journal_path=journal.path)
            with Server(('127.0.0.1', 0), state) as server:
                thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
                url = 'http://127.0.0.1:%d/api/v1/timecode/history' % server.server_port
                try:
                    with urlopen(url + '?limit=1', timeout=2) as response:
                        result = json.load(response)['data']
                    self.assertEqual(len(result['items']), 1)
                    self.assertIsNotNone(result['next_cursor'])
                    for query in ('limit=201', 'cursor=01', 'send=true', 'limit=1&limit=2'):
                        with self.assertRaises(HTTPError) as error: urlopen(url + '?' + query, timeout=2)
                        self.assertEqual(error.exception.code, 422)
                    with patch.object(state.timecode_journal, 'history', side_effect=ValueError('invalid persisted schema')):
                        with self.assertRaises(HTTPError) as error: urlopen(url, timeout=2)
                        self.assertEqual(error.exception.code, 503)
                    journal.path.unlink()
                    with self.assertRaises(HTTPError) as error: urlopen(url, timeout=2)
                    self.assertEqual(error.exception.code, 503)
                finally:
                    server.shutdown(); thread.join(timeout=3)

    def test_status_rechecks_30ms_boundary_even_with_fresh_heartbeat(self):
        mapper = Mapper('test-boot', 'test-instance')
        mapper.observe(Packet(1, 0, 100), 100000, 100000)
        response = dict(receiver_instance_id='test-instance', data=dict(mapper.status(100000),
            read_only=True, software_tx_bytes='0', serial_connected=True, reason=None, journal={}, wire={}))
        client = Mock(boot_id='test-boot', evidence='device_observation')
        client.request.return_value = response
        for now, quality, fresh in ((129999, 'ESTIMATED', True), (130000, 'ESTIMATED', False),
                                    (5100000, 'UNAVAILABLE', False)):
            with patch('ss07.timecode_status.time.monotonic_ns', return_value=now*1000):
                result = inspect(client, dict(state='RUNNING'))
            self.assertEqual((result['quality'], result['source_valid']), (quality, fresh))
        client.request.reset_mock()
        self.assertEqual(inspect(client, dict(state='STALE'))['quality'], 'UNAVAILABLE')
        client.request.assert_not_called()

    def event(self, number):
        return dict(schema_version=1, kind='CHECKPOINT', rk_boot_id='test-boot',
            receiver_instance_id='test-instance', observed_monotonic_us=str(number),
            observed_at_utc='2026-10-23T00:00:00+00:00', evidence_kind='synthetic_test')

    def test_atomic_initialization_readonly_retention_and_capacity(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = Journal(Path(tmp) / 'journal.sqlite')
            with patch('ss07.timecode_journal.os.link', side_effect=OSError('disk error')):
                with self.assertRaises(OSError): journal.initialize()
            self.assertFalse(journal.path.exists())
            self.assertFalse(list(Path(tmp).glob('.ss07-timecode-*')))
            journal.initialize()
            with patch('ss07.timecode_journal.MAX_EVENTS', 4):
                journal.append([self.event(i) for i in range(8)])
            result = journal.history(limit=2)
            self.assertEqual([item['observed_monotonic_us'] for item in result['items']], ['7', '6'])
            self.assertEqual(len(journal.history(cursor=result['next_cursor'])['items']), 2)
            self.assertFalse(result['complete_history'])
            db = journal.connect()
            try:
                with self.assertRaises(sqlite3.OperationalError): db.execute('DELETE FROM events')
            finally: db.close()
            db = journal.connect(writable=True)
            try:
                self.assertEqual(db.execute('PRAGMA max_page_count').fetchone()[0] *
                                 db.execute('PRAGMA page_size').fetchone()[0], MAX_BYTES)
            finally: db.close()
            with self.assertRaises(ValueError): event_json(dict(self.event(1), schema_version=True))
            with self.assertRaises(ValueError): journal.append([dict(self.event(1), payload='a'*16384)])
            with self.assertRaises(ValueError): journal.history(cursor='01')


if __name__ == '__main__': unittest.main()
