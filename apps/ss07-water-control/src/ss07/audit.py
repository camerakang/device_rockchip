"""Small bounded audit journal; only allowlisted fields, never request bodies."""
import json
import os
import re
import sqlite3
import stat
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from .config import decode_json
from .snapshots import u64

APP_ID = 0x53534155
FIELDS = {'username', 'operation_id', 'recording_id', 'revision', 'locked', 'outcome', 'role'}
KINDS = {'LOGIN_OK', 'LOGIN_FAILED', 'LOGOUT', 'CONFIG_UPDATE', 'CONFIG_APPLY', 'CONFIG_ROLLBACK', 'RECORD_LOCK', 'OPERATION_STARTED', 'OPERATION_FAILED'}
KINDS.update({'RTSP_ACCESS_START', 'RTSP_LOGIN_OK', 'RTSP_LOGIN_FAILED', 'RTSP_REVOKE', 'DIRECT_ACCESS'})


class AuditUnavailable(OSError):
    pass


class Audit:
    def __init__(self, path):
        self.path = Path(path).absolute()
        self.lock = threading.RLock()

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try: self.path.lstat()
        except FileNotFoundError: pass
        else:
            self.connect().close(); return
        fd, temporary = tempfile.mkstemp(prefix='.ss07-audit-', suffix='.sqlite', dir=str(self.path.parent))
        try:
            os.fchmod(fd, 0o640)
            db = sqlite3.connect(temporary, isolation_level=None)
            try:
                db.execute('PRAGMA journal_mode=DELETE')
                db.execute('PRAGMA synchronous=FULL')
                db.executescript('BEGIN IMMEDIATE; CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, data TEXT NOT NULL);'
                                 ' PRAGMA application_id=%d; PRAGMA user_version=1; COMMIT;' % APP_ID)
            finally: db.close()
            os.fsync(fd)
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

    def connect(self, writable=False):
        fd = os.open(str(self.path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode): raise AuditUnavailable('invalid audit file')
        finally: os.close(fd)
        db = sqlite3.connect('file:' + quote(str(self.path), safe='/') + ('?mode=rw' if writable else '?mode=ro'),
                             uri=True, isolation_level=None, timeout=.5)
        try:
            if db.execute('PRAGMA application_id').fetchone()[0] != APP_ID or db.execute('PRAGMA user_version').fetchone()[0] != 1:
                raise AuditUnavailable('unsupported audit schema')
            if writable:
                db.execute('PRAGMA synchronous=FULL')
                limit = 16777216 // db.execute('PRAGMA page_size').fetchone()[0]
                if db.execute('PRAGMA max_page_count=' + str(limit)).fetchone()[0] > limit: raise AuditUnavailable('audit capacity')
            else: db.execute('PRAGMA query_only=ON')
            return db
        except Exception:
            db.close(); raise

    def append(self, kind, **fields):
        if kind not in KINDS or set(fields) - FIELDS: raise ValueError('invalid audit event')
        for key, value in fields.items():
            if key == 'locked':
                if type(value) is not bool: raise ValueError('invalid audit bool')
            elif not isinstance(value, str) or re.fullmatch(r'[a-zA-Z0-9_.:-]{1,96}', value) is None:
                raise ValueError('invalid audit field')
        raw = json.dumps(dict(schema_version=1, kind=kind, observed_at_utc=datetime.now(timezone.utc).isoformat(), **fields),
                         separators=(',', ':'))
        if len(raw.encode()) > 1024: raise ValueError('audit event too large')
        with self.lock:
            db = self.connect(writable=True)
            try:
                db.execute('BEGIN IMMEDIATE')
                db.execute('DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT 4095)')
                db.execute('INSERT INTO events(data) VALUES(?)', (raw,)); db.execute('COMMIT')
            finally: db.close()

    def history(self, limit=50, cursor=None):
        if type(limit) is not int or not 1 <= limit <= 100: raise ValueError('invalid audit limit')
        if cursor is not None and not 1 <= u64(cursor) <= 2**63-1: raise ValueError('invalid audit cursor')
        db = self.connect()
        try:
            rows = db.execute('SELECT id,data FROM events' + (' WHERE id<?' if cursor else '') +
                              ' ORDER BY id DESC LIMIT ?', ([int(cursor)] if cursor else []) + [limit + 1]).fetchall()
            return dict(items=[dict(event_id=str(identity), **decode_json(raw, max_bytes=1024)) for identity, raw in rows[:limit]],
                        next_cursor=str(rows[limit-1][0]) if len(rows) > limit else None,
                        retention='latest_4096_events', complete_history=False)
        finally: db.close()
