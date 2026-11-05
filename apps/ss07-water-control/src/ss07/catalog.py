"""SQLite recording index and durable sidecars; no media decoding in Python."""
import fcntl
import json
import os
import re
import sqlite3
import stat
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from .config import ConfigError, decode_json
from .mp4 import InvalidMP4, inspect as inspect_mp4
from .snapshots import u64
from .volume import VolumeError

APP_ID = 0x53533037
SCHEMA = 1
DATABASE_SCHEMA = 2
TIME_INDEX_SQL = '''CREATE TABLE time_index (
    recording_id TEXT NOT NULL REFERENCES recordings(recording_id) ON DELETE CASCADE,
    epoch TEXT NOT NULL, lower_us TEXT NOT NULL, upper_us TEXT NOT NULL,
    association TEXT NOT NULL,
    PRIMARY KEY(recording_id,epoch,lower_us,upper_us,association))'''
TIME_LOOKUP_SQL = 'CREATE INDEX time_index_lookup ON time_index(epoch,lower_us,upper_us)'
CHANNELS = ('front', 'back', 'left', 'right', 'down', 'sdi')
RELATIVE = re.compile(r'([a-f0-9]{32})/(front|back|left|right|down|sdi)/(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}-\d{6})\.mp4')
STATES = ('COMPLETE', 'INCOMPLETE', 'MISSING', 'INVALID', 'DELETING', 'DELETED')


class CatalogError(ValueError):
    pass


class RecordConflict(CatalogError):
    pass


class RecoveryLimit(RuntimeError):
    pass


def utc(value=None):
    if value is None:
        value = datetime.now(timezone.utc)
    if isinstance(value, str):
        if len(value) > 40:
            raise CatalogError('invalid_utc')
        try:
            value = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError as exc:
            raise CatalogError('invalid_utc') from exc
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise CatalogError('timezone_required')
    return value.astimezone(timezone.utc).isoformat(timespec='microseconds')


def recording_id(volume_uuid, relative):
    if not isinstance(relative, str) or RELATIVE.fullmatch(relative) is None:
        raise CatalogError('invalid_recording_path')
    return str(uuid.uuid5(uuid.NAMESPACE_URL, 'ss07:' + volume_uuid + ':' + relative))


def valid_id(value):
    if not isinstance(value, str) or len(value) != 36:
        raise CatalogError('invalid_recording_id')
    try:
        if str(uuid.UUID(value)) != value:
            raise ValueError()
    except ValueError as exc:
        raise CatalogError('invalid_recording_id') from exc
    return value


def validate_metadata(data):
    keys = {'schema_version', 'recording_id', 'volume_uuid', 'relative_path', 'channel_id',
            'session_id', 'boot_id', 'start_utc', 'end_utc', 'duration_ms', 'size_bytes',
            'inode', 'mtime_ns', 'state', 'locked', 'lock_revision', 'evidence_kind',
            'create_monotonic_us', 'complete_monotonic_us', 'timestamp_basis', 'time_tags',
            'verification', 'reason'}
    if not isinstance(data, dict) or set(data) != keys or type(data['schema_version']) is not int or data['schema_version'] != SCHEMA:
        raise CatalogError('invalid_recording_metadata')
    volume = data['volume_uuid']
    if not isinstance(volume, str) or re.fullmatch(r'[A-Za-z0-9-]{1,64}', volume) is None:
        raise CatalogError('invalid_volume_uuid')
    match = RELATIVE.fullmatch(data['relative_path']) if isinstance(data['relative_path'], str) else None
    if (match is None or data['recording_id'] != recording_id(volume, data['relative_path'])
            or data['channel_id'] != match[2] or data['session_id'] != match[1]):
        raise CatalogError('recording_identity_mismatch')
    if not isinstance(data['boot_id'], str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', data['boot_id']):
        raise CatalogError('invalid_recording_boot')
    if utc(data['start_utc']) != data['start_utc'] or (data['end_utc'] is not None and utc(data['end_utc']) != data['end_utc']):
        raise CatalogError('noncanonical_recording_time')
    file_start = datetime.strptime(match[3], '%Y-%m-%d_%H-%M-%S-%f').replace(tzinfo=timezone.utc)
    if data['start_utc'] != utc(file_start):
        raise CatalogError('segment_filename_time_mismatch')
    if data['state'] not in ('COMPLETE', 'INCOMPLETE') or type(data['locked']) is not bool:
        raise CatalogError('invalid_recording_state')
    for name in ('size_bytes', 'inode', 'mtime_ns', 'lock_revision'):
        u64(data[name])
    if int(data['size_bytes']) > 2**63 - 1 or int(data['lock_revision']) > 2**63 - 1:
        raise CatalogError('recording_value_too_large')
    for name in ('create_monotonic_us', 'complete_monotonic_us'):
        if data[name] is not None:
            u64(data[name])
    if data['duration_ms'] is not None and (type(data['duration_ms']) is not int or not 1 <= data['duration_ms'] <= 86400000):
        raise CatalogError('invalid_recording_duration')
    if data['state'] == 'COMPLETE' and (data['duration_ms'] is None or data['end_utc'] is None
                                       or data['complete_monotonic_us'] is None):
        raise CatalogError('completion_evidence_missing')
    if data['state'] == 'COMPLETE' and data['end_utc'] != utc(file_start + timedelta(milliseconds=data['duration_ms'])):
        raise CatalogError('segment_duration_mismatch')
    if data['evidence_kind'] not in ('device_observation', 'synthetic_test'):
        raise CatalogError('invalid_recording_evidence')
    if data['timestamp_basis'] != 'segment_notification_not_exposure':
        raise CatalogError('invalid_timestamp_basis')
    if not isinstance(data['time_tags'], dict) or set(data['time_tags']) != {'start', 'end'}:
        raise CatalogError('invalid_time_tags')
    from .timecode_model import validate_tag
    for name, event, basis in (('start', 'create_monotonic_us', 'segment_create_notification'),
                               ('end', 'complete_monotonic_us', 'segment_complete_notification')):
        tag = data['time_tags'][name]
        if tag is None: continue  # Immutable pre-0.5 records and unknown crash remnants.
        validate_tag(tag)
        if (tag['rk_boot_id'] != data['boot_id'] or tag['evidence_kind'] != data['evidence_kind']
                or tag['local_monotonic_us'] != data[event] or tag['timestamp_basis'] != basis):
            raise CatalogError('time_tag_recording_identity_mismatch')
    if data['verification'] not in ('container_structure_only', 'not_verified'):
        raise CatalogError('invalid_verification')
    if data['reason'] is not None and (not isinstance(data['reason'], str) or len(data['reason']) > 128):
        raise CatalogError('invalid_recording_reason')
    raw = json.dumps(data, allow_nan=False, separators=(',', ':'))
    if len(raw.encode()) > 65536:
        raise CatalogError('recording_metadata_too_large')
    return data


def time_spans(data):
    start, end = data['time_tags']['start'], data['time_tags']['end']
    def usable(tag): return tag is not None and tag['quality'] == 'ESTIMATED'
    same = ('mapping_id', 'receiver_instance_id', 'source_epoch', 'rk_boot_id')
    if (usable(start) and usable(end) and start['source_valid'] and end['source_valid']
            and all(start[k] == end[k] for k in same)
            and u64(start['estimated_time_us']) <= u64(end['estimated_time_us'])):
        return [(start['source_epoch'], u64(start['estimated_time_us']), u64(end['estimated_time_us']), 'continuous_mapping_estimate')]
    return list({(tag['source_epoch'], u64(tag['estimated_time_us']), u64(tag['estimated_time_us']),
                  'endpoint_estimate' if tag['source_valid'] else 'holdover_endpoint')
                 for tag in (start, end) if usable(tag)})


class Catalog:
    def __init__(self, path):
        if sqlite3.sqlite_version_info < (3, 24, 0):
            raise CatalogError('SQLite >=3.24 is required for the recording catalog')
        self.path = Path(path).absolute()

    @contextmanager
    def locked(self):
        # This lock serializes file-sidecar and SQLite changes across CLI/workers.
        fd = os.open(str(self.path) + '.writer.lock', os.O_RDONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o640)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise CatalogError('invalid_catalog_lock')
            deadline = time.monotonic() + 2
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise CatalogError('catalog_writer_busy')
                    time.sleep(0.01)
            yield
        finally:
            os.close(fd)

    @contextmanager
    def connection(self, writable=False, legacy_write=False):
        fd = os.open(str(self.path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise CatalogError('invalid_catalog_file')
        finally:
            os.close(fd)
        uri = 'file:' + quote(str(self.path), safe='/') + ('?mode=rw' if writable else '?mode=ro')
        db = sqlite3.connect(uri, uri=True, timeout=2, isolation_level=None)
        try:
            db.row_factory = sqlite3.Row
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if db.execute('PRAGMA application_id').fetchone()[0] != APP_ID or version not in (1, DATABASE_SCHEMA):
                raise CatalogError('unsupported_catalog_schema')
            if writable:
                if version != DATABASE_SCHEMA and not legacy_write:
                    raise CatalogError('catalog_schema_upgrade_required')
                db.execute('PRAGMA synchronous=FULL')
                db.execute('PRAGMA foreign_keys=ON')
                # max_page_count is connection-local, not a durable DB setting.
                page_size = db.execute('PRAGMA page_size').fetchone()[0]
                limit = 1073741824 // page_size
                if db.execute('PRAGMA max_page_count=' + str(limit)).fetchone()[0] > limit:
                    raise CatalogError('catalog_capacity_reached')
            else:
                db.execute('PRAGMA query_only=ON')
            yield db
        finally:
            db.close()

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.locked():
            try:
                fd = os.open(str(self.path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640)
            except FileExistsError:
                with self.connection() as existing:
                    if existing.execute('PRAGMA user_version').fetchone()[0] != DATABASE_SCHEMA:
                        raise CatalogError('catalog_schema_upgrade_required')
                    return
            os.close(fd)
            db = sqlite3.connect(str(self.path), isolation_level=None)
            try:
                db.execute('PRAGMA journal_mode=DELETE')
                db.execute('PRAGMA synchronous=FULL')
                db.execute('PRAGMA max_page_count=262144')
                db.executescript('''BEGIN IMMEDIATE;
                    CREATE TABLE recordings (
                      rownum INTEGER PRIMARY KEY AUTOINCREMENT,
                      recording_id TEXT NOT NULL UNIQUE,
                      volume_uuid TEXT NOT NULL,
                      relative_path TEXT NOT NULL,
                      channel_id TEXT NOT NULL,
                      start_utc TEXT NOT NULL,
                      end_utc TEXT,
                      state TEXT NOT NULL,
                      locked INTEGER NOT NULL,
                      lock_revision INTEGER NOT NULL,
                      metadata TEXT NOT NULL,
                      UNIQUE(volume_uuid,relative_path));
                    CREATE INDEX recordings_time ON recordings(channel_id,start_utc);
                    CREATE INDEX recordings_cleanup ON recordings(volume_uuid,state,locked,start_utc);
                    CREATE TABLE events (
                      rownum INTEGER PRIMARY KEY AUTOINCREMENT,
                      observed_utc TEXT NOT NULL, kind TEXT NOT NULL, detail TEXT NOT NULL);
                    PRAGMA application_id=1397960759;
                    ''' + TIME_INDEX_SQL + ';' + TIME_LOOKUP_SQL + ';PRAGMA user_version=2; COMMIT;')
            finally:
                db.close()
            with self.connection():
                pass

    def migrate_v1(self, backup_path):
        """Explicit offline upgrade: preserve a checked SQLite backup before changes."""
        backup = Path(backup_path).absolute()
        with self.locked(), self.connection(writable=True, legacy_write=True) as db:
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if version == DATABASE_SCHEMA:
                return dict(migrated=False, catalog_schema=DATABASE_SCHEMA, backup=None)
            fd = os.open(str(backup), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640)
            os.close(fd)
            saved = sqlite3.connect(str(backup))
            try:
                db.backup(saved)
                if saved.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise CatalogError('catalog_backup_integrity_failed')
            finally:
                saved.close()
            fd = os.open(str(backup), os.O_RDONLY | os.O_NOFOLLOW)
            try: os.fsync(fd)
            finally: os.close(fd)
            directory = os.open(str(backup.parent), os.O_RDONLY | os.O_DIRECTORY)
            try: os.fsync(directory)
            finally: os.close(directory)
            db.execute('BEGIN IMMEDIATE')
            try:
                db.execute(TIME_INDEX_SQL); db.execute(TIME_LOOKUP_SQL)
                for row in db.execute('SELECT metadata FROM recordings'):
                    self._index_time(db, validate_metadata(decode_json(row[0])))
                db.execute('PRAGMA user_version=2')
                db.execute('COMMIT')
            except Exception:
                db.execute('ROLLBACK')
                raise
            return dict(migrated=True, catalog_schema=DATABASE_SCHEMA, backup=str(backup))

    @staticmethod
    def _index_time(db, data):
        db.execute('DELETE FROM time_index WHERE recording_id=?', (data['recording_id'],))
        db.executemany('INSERT INTO time_index(recording_id,epoch,lower_us,upper_us,association) VALUES(?,?,?,?,?)',
            [(data['recording_id'], epoch, '%020d' % lower, '%020d' % upper, association)
             for epoch, lower, upper, association in time_spans(data)])

    @staticmethod
    def _upsert(db, data, state=None):
        validate_metadata(data)
        state = state or data['state']
        if state not in STATES:
            raise CatalogError('invalid_index_state')
        raw = json.dumps(data, allow_nan=False, separators=(',', ':'))
        if (db.execute('SELECT 1 FROM recordings WHERE recording_id=?', (data['recording_id'],)).fetchone() is None
                and db.execute('SELECT count(*) FROM recordings').fetchone()[0] >= 200000):
            raise CatalogError('catalog_capacity_reached')
        db.execute('''INSERT INTO recordings(recording_id,volume_uuid,relative_path,channel_id,
            start_utc,end_utc,state,locked,lock_revision,metadata) VALUES(?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(recording_id) DO UPDATE SET state=excluded.state,locked=excluded.locked,
            lock_revision=excluded.lock_revision,metadata=excluded.metadata,end_utc=excluded.end_utc''',
            (data['recording_id'], data['volume_uuid'], data['relative_path'], data['channel_id'],
             data['start_utc'], data['end_utc'], state, int(data['locked']), int(data['lock_revision']), raw))
        Catalog._index_time(db, data)

    def import_metadata(self, data, state=None):
        with self.locked(), self.connection(writable=True) as db:
            db.execute('BEGIN IMMEDIATE')
            self._upsert(db, data, state)
            db.execute('COMMIT')

    def get(self, record_id):
        valid_id(record_id)
        with self.connection() as db:
            row = db.execute('SELECT * FROM recordings WHERE recording_id=?', (record_id,)).fetchone()
        if row is None:
            raise KeyError(record_id)
        return dict(row)

    def list(self, channel=None, start=None, end=None, limit=50, cursor=None,
             epoch=None, time_start_us=None, time_end_us=None):
        if channel is not None and channel not in CHANNELS:
            raise CatalogError('invalid_recording_channel')
        if type(limit) is not int or not 1 <= limit <= 200:
            raise CatalogError('invalid_page_limit')
        start, end = utc(start) if start else None, utc(end) if end else None
        if start and end and start >= end:
            raise CatalogError('invalid_time_window')
        terms, values = ["state!='DELETED'"], []
        if epoch is None and (time_start_us is not None or time_end_us is not None):
            raise CatalogError('timecode_query_requires_epoch')
        if epoch is not None:
            if not u64(epoch): raise CatalogError('invalid_timecode_epoch')
            lower = u64(time_start_us) if time_start_us is not None else 0
            upper = u64(time_end_us) if time_end_us is not None else 2**64
            if lower >= upper: raise CatalogError('invalid_timecode_window')
            terms.append('EXISTS (SELECT 1 FROM time_index t WHERE t.recording_id=recordings.recording_id AND t.epoch=? AND t.upper_us>=? AND t.lower_us<?)')
            values.extend((epoch, '%020d' % lower, '%020d' % upper))
        for column, value in [('channel_id', channel)]:
            if value is not None:
                terms.append(column + '=?'); values.append(value)
        if start:
            terms.append('(end_utc IS NULL OR end_utc>=?)'); values.append(start)
        if end:
            terms.append('start_utc<?'); values.append(end)
        if cursor is not None:
            value = u64(cursor)
            if not 1 <= value <= 2**63 - 1:
                raise CatalogError('invalid_page_cursor')
            terms.append('rownum<?'); values.append(value)
        with self.connection() as db:
            if epoch is not None and db.execute('PRAGMA user_version').fetchone()[0] != DATABASE_SCHEMA:
                raise CatalogError('catalog_schema_upgrade_required')
            rows = db.execute('SELECT * FROM recordings WHERE ' + ' AND '.join(terms)
                              + ' ORDER BY rownum DESC LIMIT ?', values + [limit + 1]).fetchall()
        return {'items': [public_record(dict(row)) for row in rows[:limit]],
                'next_cursor': str(rows[limit - 1]['rownum']) if len(rows) > limit else None}

    def event(self, kind, detail):
        if not re.fullmatch(r'[A-Z_]{1,40}', kind) or len(detail) > 256:
            raise CatalogError('invalid_recording_event')
        with self.locked(), self.connection(writable=True) as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('INSERT INTO events(observed_utc,kind,detail) VALUES(?,?,?)', (utc(), kind, detail))
            db.execute('DELETE FROM events WHERE rownum NOT IN (SELECT rownum FROM events ORDER BY rownum DESC LIMIT 10000)')
            db.execute('COMMIT')

    def events(self, limit=50):
        with self.connection() as db:
            return [dict(r) for r in db.execute('SELECT observed_utc,kind,detail FROM events ORDER BY rownum DESC LIMIT ?',
                                              (min(max(limit, 1), 200),))]


def public_record(row):
    data = validate_metadata(decode_json(row['metadata']))
    keys = ('recording_id', 'channel_id', 'start_utc', 'end_utc', 'duration_ms', 'size_bytes',
            'locked', 'lock_revision', 'evidence_kind', 'timestamp_basis', 'time_tags', 'verification', 'reason')
    result = {key: data[key] for key in keys}
    result.update(complete=row['state'] == 'COMPLETE', state=row['state'],
                  availability='unverified' if row['state'] == 'COMPLETE' else row['state'].lower(),
                  timecode_association=sorted({span[3] for span in time_spans(data)}) or ['unavailable'])
    return result


class RecordStore:
    def __init__(self, catalog, volume):
        self.catalog, self.volume = catalog, volume

    def _file(self, metadata):
        if metadata['volume_uuid'] != self.volume.volume_uuid:
            raise VolumeError('recording_volume_offline')
        fd = self.volume.open(metadata['relative_path'])
        info = os.fstat(fd)
        if (str(info.st_ino) != metadata['inode'] or str(info.st_size) != metadata['size_bytes']
                or str(info.st_mtime_ns) != metadata['mtime_ns']):
            os.close(fd)
            raise VolumeError('recording_file_changed')
        return fd

    def finalize(self, relative, session, duration_ms, created_us, completed_us, time_tags=None):
        match = RELATIVE.fullmatch(relative)
        if not match or match[1] != session['session_id'] or type(duration_ms) is not int or not 1 <= duration_ms <= 86400000:
            raise CatalogError('invalid_completion')
        start = datetime.strptime(match[3], '%Y-%m-%d_%H-%M-%S-%f').replace(tzinfo=timezone.utc)
        with self.catalog.locked(), self.catalog.connection(writable=True) as db:
            fd = self.volume.open(relative)
            try:
                verification = inspect_mp4(fd)
                os.fsync(fd)
                info = os.fstat(fd)
            finally:
                os.close(fd)
            meta = dict(schema_version=SCHEMA, recording_id=recording_id(self.volume.volume_uuid, relative),
                volume_uuid=self.volume.volume_uuid, relative_path=relative, channel_id=match[2],
                session_id=match[1], boot_id=session['boot_id'], start_utc=utc(start),
                end_utc=utc(start + timedelta(milliseconds=duration_ms)), duration_ms=duration_ms,
                size_bytes=str(info.st_size), inode=str(info.st_ino), mtime_ns=str(info.st_mtime_ns),
                state='COMPLETE', locked=False, lock_revision='0', evidence_kind=self.volume.evidence_kind,
                create_monotonic_us=created_us, complete_monotonic_us=completed_us,
                timestamp_basis='segment_notification_not_exposure', time_tags=time_tags or {'start': None, 'end': None},
                verification=verification['verification'], reason='timecode_not_associated')
            validate_metadata(meta)
            associations = {span[3] for span in time_spans(meta)}
            if associations:
                meta['reason'] = 'timecode_holdover' if 'holdover_endpoint' in associations else 'timecode_estimated_uncalibrated'
            previous = db.execute('SELECT metadata FROM recordings WHERE recording_id=?', (meta['recording_id'],)).fetchone()
            try:
                old = validate_metadata(self.volume.read_json(relative + '.json'))
            except FileNotFoundError:
                old = validate_metadata(decode_json(previous['metadata'])) if previous else None
            if old:
                if old['relative_path'] != relative or old['volume_uuid'] != self.volume.volume_uuid:
                    raise CatalogError('sidecar_identity_mismatch')
                # Duplicate hooks cannot undo a lock or reinterpret a completed file.
                if old['state'] == 'COMPLETE':
                    check = self._file(old); os.close(check)
                    db.execute('BEGIN IMMEDIATE')
                    self.catalog._upsert(db, old)
                    db.execute('COMMIT')
                    return old
                if old['inode'] != meta['inode'] or old['relative_path'] != relative:
                    raise CatalogError('existing_recording_changed')
                meta.update(locked=old['locked'], lock_revision=old['lock_revision'])
            self.volume.write_json(relative + '.json', meta)
            db.execute('BEGIN IMMEDIATE')
            self.catalog._upsert(db, meta)
            db.execute('COMMIT')
            return meta

    def open_recording(self, record_id):
        row = self.catalog.get(record_id)
        if row['state'] != 'COMPLETE':
            raise VolumeError('recording_not_complete')
        metadata = validate_metadata(decode_json(row['metadata']))
        return self._file(metadata), metadata

    def set_lock(self, record_id, locked, expected_revision):
        valid_id(record_id)
        if type(locked) is not bool:
            raise CatalogError('boolean_lock_required')
        expected = u64(expected_revision)
        with self.catalog.locked(), self.catalog.connection(writable=True) as db:
            row = db.execute('SELECT * FROM recordings WHERE recording_id=?', (record_id,)).fetchone()
            if row is None:
                raise KeyError(record_id)
            if row['state'] not in ('COMPLETE', 'INCOMPLETE'):
                raise RecordConflict('recording_not_lockable')
            meta = validate_metadata(decode_json(row['metadata']))
            if int(meta['lock_revision']) != expected:
                raise RecordConflict('lock_revision_conflict')
            fd = self._file(meta); os.close(fd)
            sidecar = self.volume.read_json(meta['relative_path'] + '.json')
            if validate_metadata(sidecar) != meta:
                raise RecordConflict('sidecar_recovery_required')
            if meta['locked'] != locked:
                if expected >= 2**63 - 1:
                    raise RecordConflict('lock_revision_exhausted')
                meta.update(locked=locked, lock_revision=str(expected + 1))
                self.volume.write_json(meta['relative_path'] + '.json', meta)
                db.execute('BEGIN IMMEDIATE')
                self.catalog._upsert(db, meta)
                db.execute('COMMIT')
            return public_record(dict(row, metadata=json.dumps(meta)))

    def cleanup(self, reserve_bytes, retention_days=None, batch=64):
        """Delete finalized, unlocked owned segments only; interrupted deletes recover."""
        if type(reserve_bytes) is not int or reserve_bytes < 0 or not 1 <= batch <= 128:
            raise CatalogError('invalid_cleanup_policy')
        cutoff = utc(datetime.now(timezone.utc) - timedelta(days=retention_days)) if retention_days else None
        deleted, refused = 0, 0
        with self.catalog.locked(), self.catalog.connection(writable=True) as db:
            candidates = db.execute('''SELECT * FROM recordings WHERE volume_uuid=? AND state='COMPLETE'
                AND locked=0 ORDER BY start_utc,recording_id LIMIT ?''', (self.volume.volume_uuid, batch)).fetchall()
            for row in candidates:
                free, _ = self.volume.space()
                if free >= reserve_bytes and (cutoff is None or row['start_utc'] >= cutoff):
                    break
                meta = validate_metadata(decode_json(row['metadata']))
                try:
                    sidecar = validate_metadata(self.volume.read_json(meta['relative_path'] + '.json'))
                    if sidecar != meta or sidecar['locked']:
                        raise CatalogError('sidecar_recovery_required')
                    fd = self._file(meta); os.close(fd)
                    db.execute('BEGIN IMMEDIATE')
                    db.execute("UPDATE recordings SET state='DELETING' WHERE recording_id=?", (meta['recording_id'],))
                    db.execute('COMMIT')
                    self.volume.unlink(meta['relative_path'])
                    self.volume.unlink(meta['relative_path'] + '.json')
                    db.execute('BEGIN IMMEDIATE')
                    db.execute("UPDATE recordings SET state='DELETED' WHERE recording_id=?", (meta['recording_id'],))
                    db.execute('COMMIT')
                    deleted += 1
                except (OSError, ValueError):
                    refused += 1
                    # Do not let the same unsafe oldest batch starve cleanup of
                    # later, verified files. Recovery can restore this index row.
                    db.execute("UPDATE recordings SET state='INVALID' WHERE recording_id=? AND state='COMPLETE'",
                               (row['recording_id'],))
            # Bound obsolete index rows; never discard extant or locked records.
            db.execute("DELETE FROM recordings WHERE state='DELETED' AND rownum NOT IN (SELECT rownum FROM recordings WHERE state='DELETED' ORDER BY rownum DESC LIMIT 10000)")
        return dict(deleted=deleted, refused=refused, free_bytes=str(self.volume.space()[0]))

    def recover(self, maximum=200000):
        """Rebuild completed records from durable sidecars; do not infer completion."""
        result = dict(restored=0, incomplete=0, rejected=0, missing=0)
        seen = set()
        inspected = 0
        with os.scandir(self.volume.fd) as sessions:
            for session in sessions:
                if not re.fullmatch(r'[a-f0-9]{32}', session.name) or not session.is_dir(follow_symlinks=False):
                    continue
                try:
                    session_meta = self.volume.read_json(session.name + '/session.json')
                    if (session_meta.get('session_id') != session.name
                            or session_meta.get('volume_uuid') != self.volume.volume_uuid):
                        raise CatalogError('session_identity_mismatch')
                    for channel in CHANNELS:
                        with self.volume.directory(session.name + '/' + channel) as directory, os.scandir(directory) as files:
                            for entry in files:
                                if not entry.name.endswith('.mp4'):
                                    continue
                                inspected += 1
                                if inspected > maximum:
                                    raise RecoveryLimit('recovery_scan_limit')
                                relative = session.name + '/' + channel + '/' + entry.name
                                try:
                                    match = RELATIVE.fullmatch(relative)
                                    if match is None:
                                        raise CatalogError('invalid_segment_filename')
                                    fd = self.volume.open(relative)
                                    try:
                                        info = os.fstat(fd)
                                    finally:
                                        os.close(fd)
                                    try:
                                        meta = validate_metadata(self.volume.read_json(relative + '.json'))
                                        if meta['relative_path'] != relative:
                                            raise CatalogError('sidecar_identity_mismatch')
                                        check = self._file(meta)
                                        try:
                                            if meta['state'] == 'COMPLETE':
                                                inspect_mp4(check)
                                        finally:
                                            os.close(check)
                                    except FileNotFoundError:
                                        meta = dict(schema_version=SCHEMA, recording_id=recording_id(self.volume.volume_uuid, relative),
                                            volume_uuid=self.volume.volume_uuid, relative_path=relative, channel_id=channel,
                                            session_id=session.name, boot_id=session_meta['boot_id'],
                                            start_utc=utc(datetime.strptime(match[3], '%Y-%m-%d_%H-%M-%S-%f').replace(tzinfo=timezone.utc)),
                                            end_utc=None, duration_ms=None, size_bytes=str(info.st_size), inode=str(info.st_ino),
                                            mtime_ns=str(info.st_mtime_ns), state='INCOMPLETE', locked=True, lock_revision='0',
                                            evidence_kind=session_meta['evidence_kind'], create_monotonic_us=None,
                                            complete_monotonic_us=None, timestamp_basis='segment_notification_not_exposure',
                                            time_tags={'start': None, 'end': None}, verification='not_verified',
                                            reason='completion_notification_missing')
                                        self.volume.write_json(relative + '.json', meta)
                                    self.catalog.import_metadata(meta)
                                    seen.add(meta['recording_id'])
                                    result['restored' if meta['state'] == 'COMPLETE' else 'incomplete'] += 1
                                except (OSError, ValueError, KeyError):
                                    result['rejected'] += 1
                                    if RELATIVE.fullmatch(relative):
                                        with self.catalog.locked(), self.catalog.connection(writable=True) as db:
                                            db.execute("UPDATE recordings SET state='INVALID' WHERE recording_id=?",
                                                       (recording_id(self.volume.volume_uuid, relative),))
                except (OSError, ValueError, KeyError):
                    result['rejected'] += 1
        # Only examine this mounted volume; an absent different data disk is not missing data.
        with self.catalog.locked(), self.catalog.connection(writable=True) as db:
            rows = db.execute("SELECT recording_id,relative_path,state FROM recordings WHERE volume_uuid=? AND state!='DELETED'",
                              (self.volume.volume_uuid,))
            for index, row in enumerate(rows):
                if index >= maximum:
                    raise RecoveryLimit('catalog_recovery_limit')
                if row['recording_id'] in seen:
                    continue
                try:
                    fd = self.volume.open(row['relative_path']); os.close(fd)
                except FileNotFoundError:
                    db.execute("UPDATE recordings SET state='MISSING' WHERE recording_id=?", (row['recording_id'],))
                    result['missing'] += 1
        return result
