"""Bounded asynchronous mapping audit journal; serial reception never waits for disk."""
import fcntl
import json
import os
import queue
import sqlite3
import stat
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from .config import decode_json
from .snapshots import u64
from .timecode_model import IDENTITY

APP_ID = 0x53535443
MAX_EVENTS = 8192
MAX_BYTES = 67108864


def event_json(event):
    required = {'schema_version', 'kind', 'rk_boot_id', 'receiver_instance_id',
                'observed_monotonic_us', 'observed_at_utc', 'evidence_kind'}
    if (not isinstance(event, dict) or not required <= event.keys() or type(event['schema_version']) is not int
            or event['schema_version'] != 1
            or event['evidence_kind'] not in ('device_observation', 'synthetic_test')
            or not isinstance(event['kind'], str) or not event['kind'].isupper() or len(event['kind']) > 40):
        raise ValueError('invalid_timecode_journal_event')
    for key in ('rk_boot_id', 'receiver_instance_id'):
        if not isinstance(event[key], str) or IDENTITY.fullmatch(event[key]) is None:
            raise ValueError('invalid_timecode_journal_identity')
    u64(event['observed_monotonic_us'])
    raw = json.dumps(event, allow_nan=False, separators=(',', ':'))
    if len(raw.encode()) > 16384:
        raise ValueError('timecode_journal_event_too_large')
    return raw


class Journal:
    def __init__(self, path):
        self.path = Path(path).absolute()

    def connect(self, writable=False):
        fd = os.open(str(self.path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError('timecode_journal_not_regular')
        finally:
            os.close(fd)
        db = sqlite3.connect('file:' + quote(str(self.path), safe='/') + ('?mode=rw' if writable else '?mode=ro'),
                             uri=True, timeout=0.2, isolation_level=None)
        try:
            if db.execute('PRAGMA application_id').fetchone()[0] != APP_ID or db.execute('PRAGMA user_version').fetchone()[0] != 1:
                raise ValueError('unsupported_timecode_journal_schema')
            if writable:
                db.execute('PRAGMA synchronous=FULL')
                limit = MAX_BYTES // db.execute('PRAGMA page_size').fetchone()[0]
                if db.execute('PRAGMA max_page_count=' + str(limit)).fetchone()[0] > limit:
                    raise ValueError('timecode_journal_capacity')
            else:
                db.execute('PRAGMA query_only=ON')
            return db
        except Exception:
            db.close()
            raise

    def initialize(self):
        try:
            self.path.lstat()
        except FileNotFoundError:
            pass
        else:
            self.connect().close()
            return
        fd, temporary = tempfile.mkstemp(prefix='.ss07-timecode-', suffix='.sqlite', dir=str(self.path.parent))
        try:
            os.fchmod(fd, 0o640)
            db = sqlite3.connect(temporary, isolation_level=None)
            try:
                db.execute('PRAGMA journal_mode=DELETE')
                db.execute('PRAGMA synchronous=FULL')
                db.executescript('''BEGIN IMMEDIATE;
                    CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, data TEXT NOT NULL);
                    PRAGMA application_id=1397969987;
                    PRAGMA user_version=1;
                    COMMIT;''')
            finally:
                db.close()
            os.fsync(fd)
            # Never publish a half-initialized DB or overwrite existing history.
            try: os.link(temporary, str(self.path), follow_symlinks=False)
            except FileExistsError: pass
            directory = os.open(str(self.path.parent), os.O_RDONLY | os.O_DIRECTORY)
            try: os.fsync(directory)
            finally: os.close(directory)
        finally:
            os.close(fd)
            for suffix in ('', '-journal'):
                try: os.unlink(temporary + suffix)
                except FileNotFoundError: pass
        self.connect().close()

    def append(self, events):
        rows = [(event_json(event),) for event in events]
        if not 1 <= len(rows) <= 32:
            raise ValueError('timecode_journal_batch_limit')
        db = self.connect(writable=True)
        try:
            db.execute('BEGIN IMMEDIATE')
            db.executemany('INSERT INTO events(data) VALUES(?)', rows)
            db.execute('DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT ?)', (MAX_EVENTS,))
            db.execute('COMMIT')
        finally:
            db.close()

    def history(self, limit=50, cursor=None):
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError('invalid_timecode_history_limit')
        values = []
        clause = ''
        if cursor is not None:
            value = u64(cursor)
            if not 1 <= value <= 2**63-1:
                raise ValueError('invalid_timecode_history_cursor')
            clause = ' WHERE id<?'; values.append(value)
        db = self.connect()
        try:
            rows = db.execute('SELECT id,data FROM events' + clause + ' ORDER BY id DESC LIMIT ?', values + [limit + 1]).fetchall()
            items = []
            for identity, raw in rows[:limit]:
                event = decode_json(raw, max_bytes=16384)
                event_json(event)
                items.append(dict(event_id=str(identity), **event))
            return dict(items=items, next_cursor=str(rows[limit-1][0]) if len(rows) > limit else None,
                        retention='latest_8192_events', complete_history=False)
        finally:
            db.close()


class AsyncJournal:
    def __init__(self, path, boot_id, instance, evidence_kind):
        self.journal = Journal(path)
        self.boot, self.instance, self.evidence = boot_id, instance, evidence_kind
        self.queue = queue.Queue(maxsize=128)
        self.lock = threading.Lock()
        self.error = 'journal_starting'
        self.dropped = 0
        self.last_committed = None
        self.writing_since = None
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self): self.thread.start()

    def submit(self, event):
        item = dict(event, observed_at_utc=datetime.now(timezone.utc).isoformat())
        event_json(item)
        try:
            self.queue.put_nowait(item)
        except queue.Full:
            with self.lock: self.dropped += 1

    def status(self):
        with self.lock:
            error = self.error
            if self.writing_since is not None and time.monotonic() - self.writing_since >= 1:
                error = 'journal_write_stalled'
            return dict(error=error, queued_events=self.queue.qsize(), dropped_events=str(self.dropped),
                        last_committed_monotonic_us=self.last_committed)

    def close(self):
        self.stopping.set()
        self.thread.join(timeout=1)

    def _run(self):
        fd = None
        reported_drops = 0
        try:
            delay = 1
            while True:
                try:
                    self.journal.path.parent.mkdir(parents=True, exist_ok=True)
                    fd = os.open(str(self.journal.path) + '.lock', os.O_RDONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o640)
                    if not stat.S_ISREG(os.fstat(fd).st_mode):
                        raise ValueError('invalid_timecode_journal_lock')
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self.journal.initialize()
                    break
                except (OSError, ValueError, sqlite3.Error):
                    if fd is not None: os.close(fd); fd = None
                    with self.lock: self.error = 'journal_unavailable'
                    if self.stopping.wait(delay): return
                    delay = min(30, delay * 2)
            with self.lock: self.error = None
            while not self.stopping.is_set() or not self.queue.empty():
                try:
                    batch = [self.queue.get(timeout=.1)]
                except queue.Empty:
                    continue
                while len(batch) < 31:
                    try: batch.append(self.queue.get_nowait())
                    except queue.Empty: break
                with self.lock: dropped = self.dropped
                if dropped > reported_drops:
                    batch.append(dict(schema_version=1, kind='JOURNAL_GAP', rk_boot_id=self.boot,
                        receiver_instance_id=self.instance, observed_monotonic_us=str(time.monotonic_ns() // 1000),
                        observed_at_utc=datetime.now(timezone.utc).isoformat(), evidence_kind=self.evidence,
                        dropped_events=str(dropped - reported_drops)))
                try:
                    with self.lock: self.writing_since = time.monotonic()
                    self.journal.append(batch)
                    reported_drops = dropped
                    with self.lock:
                        self.error = None
                        self.last_committed = str(time.monotonic_ns() // 1000)
                except (OSError, ValueError, sqlite3.Error):
                    with self.lock:
                        self.error = 'journal_write_failed'
                        self.dropped += len(batch)
                    if self.stopping.wait(1): break
                finally:
                    with self.lock: self.writing_since = None
        except (OSError, ValueError, sqlite3.Error):
            with self.lock: self.error = 'journal_unavailable'
        finally:
            if fd is not None: os.close(fd)
