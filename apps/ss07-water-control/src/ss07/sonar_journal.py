"""Bounded sample history and asynchronous association with original receive time.

This local history is a diagnostic window, not a full-session data-disk archive.
Truncation, write losses and an interrupted collector lifetime remain visible.
"""
import fcntl
import json
import os
from pathlib import Path
import queue
import sqlite3
import stat
import tempfile
import threading
import time
from urllib.parse import quote

from .config import decode_json
from .snapshots import DIRECTIONS, u64
from .sonar_core import event, validate_event
from .timecode_model import unavailable

APP_ID = 0x5353534e
MAX_EVENTS = 65536
MAX_BYTES = 134217728
MAX_EVENT_BYTES = 4096


def encoded(value):
    validate_event(value, tagged=True)
    raw = json.dumps(value, allow_nan=False, separators=(',', ':'))
    if len(raw.encode()) > MAX_EVENT_BYTES: raise ValueError('sonar_event_too_large')
    return raw


class Journal:
    def __init__(self, path): self.path = Path(path).absolute()

    def connect(self, writable=False):
        fd = os.open(str(self.path), os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1: raise ValueError('invalid_sonar_history_file')
        finally: os.close(fd)
        db = sqlite3.connect('file:' + quote(str(self.path), safe='/') + ('?mode=rw' if writable else '?mode=ro'),
                             uri=True, isolation_level=None, timeout=.2)
        try:
            if db.execute('PRAGMA application_id').fetchone()[0] != APP_ID or db.execute('PRAGMA user_version').fetchone()[0] != 1:
                raise ValueError('unsupported_sonar_history_schema')
            if writable:
                db.execute('PRAGMA synchronous=FULL')
                limit = MAX_BYTES // db.execute('PRAGMA page_size').fetchone()[0]
                if db.execute('PRAGMA max_page_count=' + str(limit)).fetchone()[0] > limit: raise ValueError('sonar_history_capacity')
            else: db.execute('PRAGMA query_only=ON')
            return db
        except Exception: db.close(); raise

    def initialize(self):
        try: self.path.lstat()
        except FileNotFoundError: pass
        else: self.connect().close(); return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix='.ss07-sonar-', suffix='.sqlite', dir=str(self.path.parent))
        try:
            os.fchmod(fd, 0o640)
            db = sqlite3.connect(temporary, isolation_level=None)
            try:
                db.execute('PRAGMA journal_mode=DELETE'); db.execute('PRAGMA synchronous=FULL')
                db.executescript('''BEGIN IMMEDIATE;
                    CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, sample_id TEXT NOT NULL UNIQUE,
                        sensor_id TEXT, epoch TEXT, external_us TEXT, data TEXT NOT NULL);
                    CREATE INDEX sensor_events ON events(sensor_id,id);
                    CREATE INDEX epoch_events ON events(epoch,external_us,id);
                    CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    INSERT INTO meta VALUES ('evicted_events','0');
                    INSERT INTO meta VALUES ('known_lost_events','0');
                    INSERT INTO meta VALUES ('open_session','');
                    PRAGMA application_id=1397969742;
                    PRAGMA user_version=1;
                    COMMIT;''')
            finally: db.close()
            os.fsync(fd)
            try: os.link(temporary, str(self.path), follow_symlinks=False)
            except FileExistsError: pass
            parent = os.open(str(self.path.parent), os.O_RDONLY | os.O_DIRECTORY)
            try: os.fsync(parent)
            finally: os.close(parent)
        finally:
            os.close(fd)
            for suffix in ('', '-journal'):
                try: os.unlink(temporary + suffix)
                except FileNotFoundError: pass
        self.connect().close()

    def append(self, events, session=None, finish=False):
        if not isinstance(events, list) or not 1 <= len(events) <= 32: raise ValueError('invalid_sonar_history_batch')
        if finish and session is None: raise ValueError('sonar_history_session_required')
        if session is not None:
            marker = events[-1] if finish else events[0]
            if (marker.get('collector_instance_id') != session or marker.get('kind') !=
                    ('COLLECTOR_STOPPED' if finish else 'COLLECTOR_STARTED')):
                raise ValueError('invalid_sonar_history_lifecycle')
        rows = []
        lost = 0
        for sample in events:
            raw = encoded(sample); tag = sample['time_tag']; external = tag['estimated_time_us']
            rows.append((sample['sample_id'], sample['sensor_id'], tag['source_epoch'], external.zfill(20) if external else None, raw))
            lost += u64(sample['lost_events'])
        db = self.connect(writable=True)
        try:
            db.execute('BEGIN IMMEDIATE')
            previous_session = db.execute("SELECT value FROM meta WHERE key='open_session'").fetchone()[0]
            if session is not None:
                if finish and previous_session != session: raise ValueError('sonar_history_session_changed')
                if not finish and previous_session:
                    start = dict(events[0], reason='unclean_previous_session')
                    rows[0] = rows[0][:-1] + (encoded(start),)
                db.execute("UPDATE meta SET value=? WHERE key='open_session'", ('' if finish else session,))
            # Make space before inserting, including the gap/lifecycle rows.
            # SQLite page-count includes reusable freelist pages; count live
            # pages so a high-water mark does not continuously erase history.
            count = db.execute('SELECT COUNT(*) FROM events').fetchone()[0]
            used = (db.execute('PRAGMA page_count').fetchone()[0] - db.execute('PRAGMA freelist_count').fetchone()[0]) * db.execute('PRAGMA page_size').fetchone()[0]
            remove = max(0, count + len(rows) - MAX_EVENTS)
            if used > MAX_BYTES * 3 // 4: remove = max(remove, min(count, max(32, MAX_EVENTS // 16)))
            removed = db.execute('DELETE FROM events WHERE id IN (SELECT id FROM events ORDER BY id LIMIT ?)', (remove,)).rowcount if remove else 0
            db.executemany('INSERT INTO events(sample_id,sensor_id,epoch,external_us,data) VALUES(?,?,?,?,?)', rows)
            # A small retention policy can be smaller than one atomic batch.
            # Validate/insert the complete batch first, then retain its newest
            # rows so duplicate records still roll back the whole transaction.
            overflow = max(0, count - removed + len(rows) - MAX_EVENTS)
            if overflow:
                removed += db.execute('DELETE FROM events WHERE id IN (SELECT id FROM events ORDER BY id LIMIT ?)', (overflow,)).rowcount
            for key, count in (('evicted_events', removed), ('known_lost_events', lost)):
                previous = u64(db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()[0])
                updated = str(previous + count); u64(updated)
                db.execute('UPDATE meta SET value=? WHERE key=?', (updated, key))
            db.execute('COMMIT')
        finally: db.close()

    def history(self, limit=50, cursor=None, direction=None, source_epoch=None, start_us=None, end_us=None):
        if type(limit) is not int or not 1 <= limit <= 200: raise ValueError('invalid_sonar_history_limit')
        where, args = [], []
        if cursor is not None:
            value = u64(cursor)
            if not 1 <= value <= 2**63-1: raise ValueError('invalid_sonar_history_cursor')
            where.append('id<?'); args.append(value)
        if direction is not None:
            if direction not in DIRECTIONS: raise ValueError('invalid_sonar_direction')
            where.append('sensor_id=?'); args.append(direction)
        if source_epoch is not None:
            if not u64(source_epoch): raise ValueError('invalid_source_epoch')
            where.append('epoch=?'); args.append(source_epoch)
        if (start_us is not None or end_us is not None) and source_epoch is None:
            raise ValueError('source_epoch_required_for_external_time')
        for field, operator, value in (('external_us', '>=', start_us), ('external_us', '<=', end_us)):
            if value is not None: u64(value); where.append(field + operator + '?'); args.append(value.zfill(20))
        if start_us is not None and end_us is not None and u64(start_us) > u64(end_us): raise ValueError('reversed_time_range')
        db = self.connect()
        try:
            db.execute('BEGIN')
            rows = db.execute('SELECT id,data FROM events' + (' WHERE ' + ' AND '.join(where) if where else '') +
                              ' ORDER BY id DESC LIMIT ?', args + [limit + 1]).fetchall()
            meta = dict(db.execute('SELECT key,value FROM meta'))
            for key in ('evicted_events', 'known_lost_events'): u64(meta[key])
            first, last, count = db.execute('SELECT MIN(id),MAX(id),COUNT(*) FROM events').fetchone()
            items = []
            for identity, raw in rows[:limit]:
                sample = decode_json(raw, max_bytes=MAX_EVENT_BYTES); validate_event(sample, tagged=True)
                items.append(dict(event_id=str(identity), **sample))
            return dict(items=items, next_cursor=str(rows[limit-1][0]) if len(rows) > limit else None,
                        retention='bounded_diagnostic_history', max_events=MAX_EVENTS, max_database_bytes=str(MAX_BYTES),
                        complete_history=False, data_disk_archive=False,
                        retained_events=count, oldest_event_id=str(first) if first else None, newest_event_id=str(last) if last else None,
                        evicted_events=meta['evicted_events'], known_lost_events=meta['known_lost_events'])
        finally: db.close()


class AsyncHistory:
    def __init__(self, path, boot, instance, timecode, evidence_kind='device_observation'):
        self.journal, self.boot, self.instance = Journal(path), boot, instance
        self.timecode, self.evidence = timecode, evidence_kind
        self.queue = queue.Queue(maxsize=256)
        self.guard, self.stopping = threading.Lock(), threading.Event()
        self.dropped, self.last_committed, self.writing_since = 0, None, None
        self.error = 'sonar_history_starting'
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self): self.thread.start()

    def submit(self, sample):
        validate_event(sample)
        if sample['rk_boot_id'] != self.boot or sample['collector_instance_id'] != self.instance or sample['evidence_kind'] != self.evidence:
            raise ValueError('sonar_collector_identity_changed')
        with self.guard:
            if self.stopping.is_set(): raise ValueError('sonar_history_closed')
            try: self.queue.put_nowait(dict(sample))
            except queue.Full: self.dropped += 1

    def status(self):
        with self.guard:
            return dict(error='sonar_history_stalled' if self.writing_since is not None and time.monotonic()-self.writing_since >= 1 else self.error,
                        queued_events=self.queue.qsize(), dropped_events=str(self.dropped), last_committed_monotonic_us=self.last_committed,
                        data_disk_archive=False)

    def close(self):
        with self.guard: self.stopping.set()
        self.thread.join(timeout=1)

    def tag(self, sample):
        now = int(sample['event_monotonic_us'])
        tag = self.timecode.tag(now, sample['timestamp_basis'])
        value = dict(sample, time_tag=tag)
        validate_event(value, tagged=True)
        return value

    def lifecycle(self, kind):
        now = time.monotonic_ns() // 1000
        value = event(self.boot, self.instance, 0, kind, now, self.evidence)
        value['time_tag'] = unavailable(self.boot, now, 'status_observation', 'collector_lifecycle', self.evidence)
        return value

    def _run(self):
        lock = None; reported = 0; initialized = False
        try:
            delay = 1
            while not self.stopping.is_set():
                try:
                    self.journal.path.parent.mkdir(parents=True, exist_ok=True)
                    lock = os.open(str(self.journal.path) + '.lock', os.O_RDONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o640)
                    if not stat.S_ISREG(os.fstat(lock).st_mode): raise ValueError('invalid_sonar_history_lock')
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self.journal.initialize()
                    self.journal.append([self.lifecycle('COLLECTOR_STARTED')], session=self.instance)
                    initialized = True
                    break
                except (OSError, ValueError, sqlite3.Error):
                    if lock is not None: os.close(lock); lock = None
                    with self.guard: self.error = 'sonar_history_unavailable'
                    if self.stopping.wait(delay): return
                    delay = min(delay * 2, 30)
            if not initialized: return
            with self.guard: self.error = None
            while not self.stopping.is_set() or not self.queue.empty():
                try: batch = [self.queue.get(timeout=.1)]
                except queue.Empty: continue
                batch_until = time.monotonic() + .1
                while len(batch) < 31:
                    try: batch.append(self.queue.get(timeout=0 if self.stopping.is_set() else max(0, batch_until-time.monotonic())))
                    except queue.Empty: break
                with self.guard:
                    self.writing_since = time.monotonic()
                    dropped = self.dropped
                try:
                    tagged = [self.tag(sample) for sample in batch]
                    if dropped > reported:
                        gap = event(self.boot, self.instance, 0, 'HISTORY_GAP', time.monotonic_ns() // 1000, self.evidence, lost=dropped-reported)
                        tagged.append(self.tag(gap))
                    self.journal.append(tagged)
                    reported = dropped
                    with self.guard:
                        self.error = None
                        self.last_committed = str(time.monotonic_ns() // 1000)
                except (OSError, ValueError, sqlite3.Error):
                    with self.guard: self.error = 'sonar_history_write_failed'; self.dropped += len(batch)
                    if self.stopping.wait(.2): break
                finally:
                    with self.guard: self.writing_since = None
            if self.queue.empty():
                final = []
                with self.guard: dropped = self.dropped
                if dropped > reported:
                    gap = event(self.boot, self.instance, 0, 'HISTORY_GAP', time.monotonic_ns() // 1000, self.evidence, lost=dropped-reported)
                    final.append(self.tag(gap))
                final.append(self.lifecycle('COLLECTOR_STOPPED'))
                self.journal.append(final, session=self.instance, finish=True)
        except (OSError, ValueError, sqlite3.Error):
            with self.guard: self.error = 'sonar_history_unavailable'
        finally:
            if lock is not None: os.close(lock)
