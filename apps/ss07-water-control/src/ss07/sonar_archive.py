"""Durable sonar JSONL parts on a pinned data volume and a rebuildable index.

The local diagnostic journal remains independent. A complete file proves a
closed, verified part, never that the acquisition had no gaps or lost samples.
"""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
import uuid
from urllib.parse import quote

from .catalog import RecordConflict, utc
from .config import decode_json
from .snapshots import DIRECTIONS, u64
from .sonar_core import validate_event
from .sonar_journal import encoded
from .timecode_model import IDENTITY
from .volume import VolumeError

APP_ID = 0x53535341
MAX_PART_BYTES = 8 * 1024 * 1024
MAX_PART_EVENTS = 8192
MAX_PARTS = 200000
MAX_INDEX_BYTES = 256 * 1024 * 1024
RELATIVE = re.compile(r'sonar/([A-Za-z0-9_-]{1,64})/([a-f0-9]{32})\.jsonl')
STATES = ('COMPLETE', 'INCOMPLETE', 'DELETING', 'DELETED', 'MISSING', 'INVALID')


def directory_names(fd,limit):
    result=[]
    with os.scandir(fd) as entries:
        for entry in entries:
            if len(result)>=limit:raise ValueError('archive_recovery_capacity')
            result.append(entry.name)
    return sorted(result)


def identity(volume, relative):
    if not isinstance(relative, str) or RELATIVE.fullmatch(relative) is None:
        raise ValueError('invalid_sonar_archive_path')
    if not isinstance(volume, str) or re.fullmatch(r'[A-Za-z0-9-]{1,64}', volume) is None:
        raise ValueError('invalid_archive_volume')
    return hashlib.sha256(('ss07-sonar:'+volume+':'+relative).encode()).hexdigest()


def valid_id(value):
    if not isinstance(value, str) or re.fullmatch(r'[a-f0-9]{64}', value) is None:
        raise ValueError('invalid_sonar_archive_id')
    return value


def key(sample):
    tag = sample['time_tag']
    return tag['source_epoch'], tag['mapping_id']


def validate_metadata(value):
    required = {'schema_version', 'archive_id', 'volume_uuid', 'relative_path', 'rk_boot_id',
        'collector_instance_id', 'evidence_kind', 'source_epoch', 'mapping_id', 'time_start_us', 'time_end_us',
        'start_utc', 'end_utc', 'first_monotonic_us', 'last_monotonic_us', 'event_count', 'directions',
        'known_lost_events', 'contains_gap', 'size_bytes', 'sha256', 'inode', 'mtime_ns', 'state', 'locked', 'lock_revision'}
    if (type(value) is not dict or set(value) != required or type(value['schema_version']) is not int
            or value['schema_version'] != 1): raise ValueError('invalid_archive_metadata')
    if identity(value['volume_uuid'], value['relative_path']) != value['archive_id']:
        raise ValueError('archive_identity_mismatch')
    for name in ('rk_boot_id', 'collector_instance_id'):
        if not isinstance(value[name], str) or IDENTITY.fullmatch(value[name]) is None: raise ValueError('invalid_archive_identity')
    if RELATIVE.fullmatch(value['relative_path'])[1] != value['collector_instance_id']: raise ValueError('archive_collector_path_mismatch')
    if value['evidence_kind'] not in ('device_observation', 'synthetic_test') or value['state'] not in STATES:
        raise ValueError('invalid_archive_state')
    if type(value['locked']) is not bool or type(value['contains_gap']) is not bool: raise ValueError('invalid_archive_flags')
    for name in ('size_bytes', 'inode', 'mtime_ns', 'lock_revision', 'known_lost_events', 'first_monotonic_us', 'last_monotonic_us'):
        u64(value[name])
    if not 1 <= u64(value['size_bytes']) <= MAX_PART_BYTES or u64(value['first_monotonic_us']) > u64(value['last_monotonic_us']):
        raise ValueError('invalid_archive_bounds')
    if type(value['event_count']) is not int or not 0 <= value['event_count'] <= MAX_PART_EVENTS:
        raise ValueError('invalid_archive_count')
    if value['state'] == 'COMPLETE' and value['event_count'] == 0: raise ValueError('empty_complete_archive')
    if not isinstance(value['sha256'], str) or re.fullmatch(r'[a-f0-9]{64}', value['sha256']) is None:
        raise ValueError('invalid_archive_hash')
    if type(value['directions']) is not list or len(value['directions']) > 5 or value['directions'] != [d for d in DIRECTIONS if d in value['directions']]:
        raise ValueError('invalid_archive_directions')
    for name in ('start_utc', 'end_utc'):
        if utc(value[name]) != value[name]: raise ValueError('noncanonical_archive_utc')
    if value['start_utc'] > value['end_utc']: raise ValueError('invalid_archive_utc_range')
    if value['source_epoch'] is None:
        if any(value[k] is not None for k in ('mapping_id', 'time_start_us', 'time_end_us')): raise ValueError('archive_epoch_required')
    else:
        if not u64(value['source_epoch']) or not isinstance(value['mapping_id'], str) or IDENTITY.fullmatch(value['mapping_id']) is None:
            raise ValueError('invalid_archive_mapping')
        if (value['time_start_us'] is None) != (value['time_end_us'] is None): raise ValueError('invalid_archive_external_range')
        if value['time_start_us'] is not None and u64(value['time_start_us']) > u64(value['time_end_us']):
            raise ValueError('invalid_archive_external_range')
    return value


def public(value):
    validate_metadata(value)
    return {k: v for k, v in value.items() if k not in ('relative_path', 'inode', 'mtime_ns')}


def inspect_file(fd, volume, relative, visitor=None, recover=False):
    """Validate a bounded stream without loading the whole part into memory."""
    before = os.fstat(fd)
    if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or not 1 <= before.st_size <= MAX_PART_BYTES):
        raise VolumeError('invalid_archive_file')
    os.lseek(fd, 0, os.SEEK_SET)
    digest = hashlib.sha256(); seen = set(); count = lost = 0; directions = set(); gap = False
    first = last = None; lower = upper = None; start = end = None; interrupted = False
    with os.fdopen(os.dup(fd), 'rb') as stream:
        line = stream.readline(4097); digest.update(line)
        header = decode_json(line, max_bytes=4096)
        header_keys = {'ss07_sonar_archive', 'archive_id', 'volume_uuid', 'rk_boot_id', 'collector_instance_id',
                       'evidence_kind', 'created_at_utc', 'source_epoch', 'mapping_id'}
        if (not line.endswith(b'\n') or type(header) is not dict or set(header) != header_keys
                or type(header['ss07_sonar_archive']) is not int or header['ss07_sonar_archive'] != 1
                or header['archive_id'] != identity(volume.volume_uuid, relative) or header['volume_uuid'] != volume.volume_uuid
                or header['collector_instance_id'] != RELATIVE.fullmatch(relative)[1]
                or header['evidence_kind'] != volume.evidence_kind): raise ValueError('invalid_archive_header')
        for field in ('rk_boot_id', 'collector_instance_id'):
            if not isinstance(header[field], str) or IDENTITY.fullmatch(header[field]) is None: raise ValueError('invalid_archive_header_identity')
        for line in iter(lambda: stream.readline(4097), b''):
            digest.update(line)
            try:
                if interrupted or not line.endswith(b'\n') or len(line) > 4096 or count >= MAX_PART_EVENTS:
                    raise ValueError('truncated_or_oversize_archive_line')
                sample = decode_json(line, max_bytes=4096); validate_event(sample, tagged=True)
                if (any(sample[k] != header[k] for k in ('rk_boot_id', 'collector_instance_id', 'evidence_kind'))
                        or key(sample) != (header['source_epoch'], header['mapping_id']) or sample['sample_id'] in seen):
                    raise ValueError('archive_sample_identity_mismatch')
            except (ValueError, KeyError, TypeError, AttributeError):
                if not recover: raise
                interrupted = True
                continue
            seen.add(sample['sample_id']); count += 1
            local = u64(sample['event_monotonic_us']); timestamp = utc(sample['observed_at_utc'])
            first, last = local if first is None else min(first, local), local if last is None else max(last, local)
            start, end = timestamp if start is None else min(start, timestamp), timestamp if end is None else max(end, timestamp)
            external = sample['time_tag']['estimated_time_us']
            if external is not None:
                external = u64(external)
                lower, upper = external if lower is None else min(lower, external), external if upper is None else max(upper, external)
            if sample['direction'] is not None: directions.add(sample['direction'])
            lost += u64(sample['lost_events'])
            gap = gap or sample['kind'] == 'HISTORY_GAP' or sample['reason'] == 'unclean_previous_session'
            if visitor is not None: visitor(count, sample)
    after = os.fstat(fd)
    if (after.st_size, after.st_ino, after.st_mtime_ns) != (before.st_size, before.st_ino, before.st_mtime_ns):
        raise VolumeError('archive_changed_during_read')
    result = dict(schema_version=1, archive_id=header['archive_id'], volume_uuid=header['volume_uuid'], relative_path=relative,
        rk_boot_id=header['rk_boot_id'], collector_instance_id=header['collector_instance_id'], evidence_kind=header['evidence_kind'],
        source_epoch=header['source_epoch'], mapping_id=header['mapping_id'], time_start_us=None if lower is None else str(lower),
        time_end_us=None if upper is None else str(upper), start_utc=start or utc(header['created_at_utc']), end_utc=end or utc(header['created_at_utc']),
        first_monotonic_us=str(first or 0), last_monotonic_us=str(last or 0), event_count=count,
        directions=[d for d in DIRECTIONS if d in directions], known_lost_events=str(lost), contains_gap=gap or interrupted,
        size_bytes=str(before.st_size), sha256=digest.hexdigest(), inode=str(before.st_ino), mtime_ns=str(before.st_mtime_ns),
        state='INCOMPLETE' if recover else 'COMPLETE', locked=bool(recover), lock_revision='0')
    return validate_metadata(result)


class Catalog:
    def __init__(self, path): self.path = Path(path).absolute()

    @contextmanager
    def exclusive(self):
        """Offline maintenance also excludes a writer using another state dir."""
        self.path.parent.mkdir(parents=True,exist_ok=True)
        fd=os.open(str(self.path)+'.owner.lock',os.O_RDONLY|os.O_CREAT|os.O_NOFOLLOW|os.O_NONBLOCK,0o640)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):raise ValueError('invalid_archive_owner_lock')
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            yield
        finally:os.close(fd)

    @contextmanager
    def locked(self):
        fd = os.open(str(self.path)+'.lock', os.O_RDONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o640)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode): raise ValueError('invalid_archive_catalog_lock')
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        finally: os.close(fd)

    @contextmanager
    def connection(self, writable=False):
        fd = os.open(str(self.path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1: raise ValueError('invalid_archive_catalog')
        finally: os.close(fd)
        db = sqlite3.connect('file:'+quote(str(self.path), safe='/')+('?mode=rw' if writable else '?mode=ro'),
                             uri=True, timeout=.2, isolation_level=None)
        try:
            if db.execute('PRAGMA application_id').fetchone()[0] != APP_ID or db.execute('PRAGMA user_version').fetchone()[0] != 1:
                raise sqlite3.DatabaseError('unsupported_archive_catalog_schema')
            if writable:
                db.execute('PRAGMA synchronous=FULL')
                limit = MAX_INDEX_BYTES // db.execute('PRAGMA page_size').fetchone()[0]
                if db.execute('PRAGMA max_page_count='+str(limit)).fetchone()[0] > limit: raise ValueError('archive_index_capacity')
            else: db.execute('PRAGMA query_only=ON')
            yield db
        finally: db.close()

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.locked():
            if self.path.exists() or self.path.is_symlink():
                with self.connection(): pass
                return
            fd, temporary = tempfile.mkstemp(prefix='.sonar-archive-', dir=str(self.path.parent))
            try:
                os.fchmod(fd, 0o640)
                with sqlite3.connect(temporary) as db:
                    db.execute('PRAGMA journal_mode=DELETE'); db.execute('PRAGMA synchronous=FULL')
                    db.executescript('''CREATE TABLE parts(id TEXT PRIMARY KEY, volume TEXT NOT NULL, path TEXT NOT NULL,
                        start TEXT NOT NULL, epoch TEXT, lower_us TEXT, upper_us TEXT, directions INTEGER NOT NULL,
                        state TEXT NOT NULL, locked INTEGER NOT NULL, data TEXT NOT NULL, UNIQUE(volume,path));
                        CREATE INDEX part_time ON parts(epoch,lower_us,upper_us);
                        CREATE INDEX part_order ON parts(start,id);
                        PRAGMA application_id=1397969729; PRAGMA user_version=1;''')
                os.fsync(fd); os.link(temporary, str(self.path), follow_symlinks=False)
                parent = os.open(str(self.path.parent), os.O_RDONLY | os.O_DIRECTORY)
                try: os.fsync(parent)
                finally: os.close(parent)
            finally:
                os.close(fd); os.unlink(temporary)

    def put(self, data):
        validate_metadata(data)
        mask = sum(1 << DIRECTIONS.index(d) for d in data['directions'])
        row = (data['archive_id'], data['volume_uuid'], data['relative_path'], data['start_utc'], data['source_epoch'],
               data['time_start_us'].zfill(20) if data['time_start_us'] is not None else None,
               data['time_end_us'].zfill(20) if data['time_end_us'] is not None else None, mask, data['state'], int(data['locked']),
               json.dumps(data, separators=(',', ':'), allow_nan=False))
        with self.locked(), self.connection(True) as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT COUNT(*) FROM parts').fetchone()[0] >= MAX_PARTS and not db.execute('SELECT 1 FROM parts WHERE id=?', row[:1]).fetchone():
                raise ValueError('archive_index_capacity')
            db.execute('INSERT INTO parts VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET '
                       'start=excluded.start,epoch=excluded.epoch,lower_us=excluded.lower_us,upper_us=excluded.upper_us,'
                       'directions=excluded.directions,state=excluded.state,locked=excluded.locked,data=excluded.data', row)
            deleted = db.execute("SELECT COUNT(*) FROM parts WHERE state='DELETED'").fetchone()[0]
            if deleted>10000:
                db.execute("DELETE FROM parts WHERE id IN (SELECT id FROM parts WHERE state='DELETED' ORDER BY start,id LIMIT ?)", (deleted-10000,))
            db.execute('COMMIT')

    def get(self, archive_id):
        valid_id(archive_id)
        with self.connection() as db:
            row = db.execute('SELECT data FROM parts WHERE id=?', (archive_id,)).fetchone()
            if row is None: raise KeyError('sonar_archive_not_found')
            data = validate_metadata(decode_json(row[0]))
            if data['archive_id'] != archive_id: raise ValueError('archive_index_identity_mismatch')
            return data

    def list(self, limit=50, cursor=None, direction=None, source_epoch=None, start_us=None, end_us=None, volume_uuid=None):
        if type(limit) is not int or not 1 <= limit <= 200: raise ValueError('invalid_archive_limit')
        terms, args = ["state != 'DELETED'"], []
        if cursor is not None:
            valid_id(cursor); previous = self.get(cursor)
            terms.append('(start<? OR (start=? AND id<?))'); args += [previous['start_utc'], previous['start_utc'], cursor]
        if direction is not None:
            if direction not in DIRECTIONS: raise ValueError('invalid_sonar_direction')
            terms.append('(directions & ?) != 0'); args.append(1 << DIRECTIONS.index(direction))
        if source_epoch is not None:
            if not u64(source_epoch): raise ValueError('invalid_archive_epoch')
            terms.append('epoch=?'); args.append(source_epoch)
        if (start_us is not None or end_us is not None) and source_epoch is None: raise ValueError('archive_epoch_required')
        if start_us is not None and end_us is not None and u64(start_us) > u64(end_us): raise ValueError('reversed_archive_time')
        for name, operator, value in (('upper_us', '>=', start_us), ('lower_us', '<=', end_us)):
            if value is not None: u64(value); terms.append(name+operator+'?'); args.append(value.zfill(20))
        if volume_uuid is not None: terms.append('volume=?'); args.append(volume_uuid)
        with self.connection() as db:
            rows = db.execute('SELECT data FROM parts WHERE '+' AND '.join(terms)+' ORDER BY start DESC,id DESC LIMIT ?', args+[limit+1]).fetchall()
            items = [public(validate_metadata(decode_json(row[0]))) for row in rows[:limit]]
        return dict(items=items, next_cursor=items[-1]['archive_id'] if len(rows)>limit else None)


class Store:
    def __init__(self, catalog, volume): self.catalog, self.volume = catalog, volume

    def open(self, archive_id, visitor=None):
        data = self.catalog.get(archive_id)
        if data['volume_uuid'] != self.volume.volume_uuid or data['state'] != 'COMPLETE': raise VolumeError('archive_not_complete_or_wrong_volume')
        self.volume.require_valid()
        try:sidecar = validate_metadata(self.volume.read_json(data['relative_path']+'.json'))
        except ValueError as exc:raise VolumeError('invalid_archive_metadata') from exc
        if sidecar != data: raise VolumeError('archive_metadata_mismatch')
        fd = self.volume.open(data['relative_path'])
        try:
            actual = inspect_file(fd, self.volume, data['relative_path'], visitor)
            expected = dict(data, locked=False, lock_revision='0')
            if actual != expected: raise VolumeError('archive_content_mismatch')
            self.volume.require_valid(); os.lseek(fd, 0, os.SEEK_SET)
            return fd, data
        except ValueError as exc:
            os.close(fd);raise VolumeError('invalid_archive_content') from exc
        except Exception: os.close(fd); raise

    def set_lock(self, archive_id, locked, expected_revision):
        if type(locked) is not bool: raise ValueError('invalid_archive_lock')
        u64(expected_revision)
        # The caller also holds the sonar worker lock for offline maintenance.
        data = self.catalog.get(archive_id)
        if data['volume_uuid'] != self.volume.volume_uuid: raise VolumeError('archive_wrong_volume')
        if data['state'] not in ('COMPLETE', 'INCOMPLETE'): raise VolumeError('archive_cannot_be_locked')
        sidecar = validate_metadata(self.volume.read_json(data['relative_path']+'.json'))
        if sidecar != data: raise VolumeError('archive_metadata_mismatch')
        if data['lock_revision'] != expected_revision: raise RecordConflict('archive_lock_revision_conflict')
        updated = dict(data, locked=locked, lock_revision=str(u64(expected_revision)+1))
        validate_metadata(updated); self.volume.require_valid()
        self.volume.write_json(data['relative_path']+'.json', updated)
        self.catalog.put(updated)
        return public(updated)

    def _delete(self, data):
        if data['locked'] or data['state'] not in ('COMPLETE', 'DELETING'): raise VolumeError('archive_delete_refused')
        self.volume.require_valid()
        sidecar = validate_metadata(self.volume.read_json(data['relative_path']+'.json'))
        if sidecar != data: raise VolumeError('archive_metadata_mismatch')
        try: fd = self.volume.open(data['relative_path'])
        except FileNotFoundError:
            if data['state'] != 'DELETING': raise
        else:
            try:
                actual=inspect_file(fd,self.volume,data['relative_path'])
                if actual != dict(data,state='COMPLETE',locked=False,lock_revision='0'):
                    raise VolumeError('archive_delete_content_mismatch')
            finally: os.close(fd)
            deleting=dict(data,state='DELETING')
            # Persist deletion intent on the volume first, so rebuilding a lost
            # local index cannot resurrect a half-deleted part as complete.
            self.volume.write_json(data['relative_path']+'.json',deleting)
            self.catalog.put(deleting)
            self.volume.require_valid(); self.volume.unlink(data['relative_path'])
        self.volume.require_valid(); self.volume.unlink(data['relative_path']+'.json')
        self.catalog.put(dict(data,state='DELETED'))

    def prune(self, reserve_bytes, retention_days=None, now=None):
        from datetime import datetime, timedelta, timezone
        if type(reserve_bytes) is not int or reserve_bytes<0: raise ValueError('invalid_archive_reserve')
        if retention_days is not None and (type(retention_days) is not int or not 1<=retention_days<=3650):
            raise ValueError('invalid_archive_retention')
        cutoff=None if retention_days is None else utc((now or datetime.now(timezone.utc))-timedelta(days=retention_days))
        removed=failed=0
        with self.catalog.connection() as db:
            rows=db.execute("SELECT data FROM parts WHERE volume=? AND state='COMPLETE' AND locked=0 ORDER BY start,id LIMIT 64",
                            (self.volume.volume_uuid,)).fetchall()
        for row in rows:
            data=validate_metadata(decode_json(row[0])); free,_=self.volume.space()
            if free>=reserve_bytes and (cutoff is None or data['end_utc']>=cutoff): continue
            try: self._delete(data); removed+=1
            except (OSError,ValueError,sqlite3.Error):
                failed+=1
                self.volume.require_valid()
                # A damaged oldest part must not prevent every later safe part
                # from ever being considered in subsequent bounded batches.
                current=self.catalog.get(data['archive_id'])
                if current['state']=='COMPLETE':self.catalog.put(dict(current,state='INVALID'))
        free,_=self.volume.space()
        return dict(removed=removed,failures=failed,free_bytes=str(free),space_available=free>=reserve_bytes)

    def recover(self):
        self.volume.require_valid(); count = failures = 0
        try:
            with self.volume.directory('sonar') as root: sessions = directory_names(root,MAX_PARTS)
        except FileNotFoundError: return dict(recovered=0, failures=0)
        if len(sessions)>MAX_PARTS: raise ValueError('archive_recovery_capacity')
        for session in sessions:
            if IDENTITY.fullmatch(session) is None: continue
            with self.volume.directory('sonar/'+session) as parent:
                names=set()
                for name in directory_names(parent,MAX_PARTS*2):
                    candidate=name[:-5] if name.endswith('.jsonl.json') else name
                    if RELATIVE.fullmatch('sonar/'+session+'/'+candidate): names.add(candidate)
                for name in sorted(names):
                    relative = 'sonar/'+session+'/'+name
                    count += 1
                    if count>MAX_PARTS: raise ValueError('archive_recovery_capacity')
                    fd = None;sidecar=None
                    try:
                        try: sidecar = validate_metadata(self.volume.read_json(relative+'.json'))
                        except FileNotFoundError: sidecar = None
                        if sidecar is not None and sidecar['state']=='DELETING':
                            self._delete(sidecar)
                            continue
                        try: fd = self.volume.open(relative)
                        except FileNotFoundError:
                            if sidecar is None: raise
                            self.catalog.put(dict(sidecar,state='MISSING'))
                            continue
                        if sidecar is None:
                            data = inspect_file(fd, self.volume, relative, recover=True)
                            self.volume.write_json(relative+'.json', data)
                        else:
                            actual = inspect_file(fd, self.volume, relative, recover=sidecar['state'] != 'COMPLETE')
                            expected = dict(sidecar, state=actual['state'], locked=actual['locked'], lock_revision='0')
                            if actual != expected: raise VolumeError('archive_recovery_content_mismatch')
                            data = sidecar
                        self.catalog.put(data)
                    except (OSError, ValueError, sqlite3.Error):
                        failures += 1
                        self.volume.require_valid()
                        previous=sidecar
                        if previous is None:
                            try:previous=self.catalog.get(identity(self.volume.volume_uuid,relative))
                            except KeyError:pass
                        if previous is not None:self.catalog.put(dict(previous,state='INVALID'))
                    finally:
                        if fd is not None: os.close(fd)
        return dict(recovered=count-failures, failures=failures)


class Writer:
    def __init__(self, catalog, volume, segment_seconds=300):
        if type(segment_seconds) is not int or not 2<=segment_seconds<=3600: raise ValueError('invalid_archive_duration')
        self.catalog, self.volume, self.segment_us = catalog, volume, segment_seconds*1000000
        self.fd = None; self.relative = None; self.count = self.size = 0
        self.started = None; self.current_key = None; self.samples = set()

    def _start(self, sample):
        folder = 'sonar/'+sample['collector_instance_id']
        self.relative = folder+'/'+uuid.uuid4().hex+'.jsonl'
        with self.volume.directory(folder, create=True) as parent:
            self.fd = os.open(self.relative.rsplit('/',1)[1], os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_NONBLOCK, 0o640, dir_fd=parent)
            try: self.volume._check_fd(self.fd); os.fsync(parent)
            except Exception: os.close(self.fd); self.fd=None; raise
        header = dict(ss07_sonar_archive=1, archive_id=identity(self.volume.volume_uuid, self.relative),
            volume_uuid=self.volume.volume_uuid, rk_boot_id=sample['rk_boot_id'], collector_instance_id=sample['collector_instance_id'],
            evidence_kind=sample['evidence_kind'], created_at_utc=utc(sample['observed_at_utc']),
            source_epoch=key(sample)[0], mapping_id=key(sample)[1])
        self.header = header; self.started=u64(sample['event_monotonic_us']); self.current_key=key(sample)
        self.count=self.size=0; self.samples=set()
        self._write((json.dumps(header, separators=(',', ':'), allow_nan=False)+'\n').encode())

    def _write(self, raw):
        self.volume.require_valid()
        done=0
        while done<len(raw):
            written=os.write(self.fd, raw[done:])
            if written<=0: raise OSError('short_archive_write')
            done+=written
        self.size+=done

    def append(self, samples):
        if type(samples) is not list or not 1<=len(samples)<=32: raise ValueError('invalid_archive_batch')
        prepared=[]
        for sample in samples:
            raw=(encoded(sample)+'\n').encode()
            if len(raw)>4096 or sample['evidence_kind']!=self.volume.evidence_kind: raise ValueError('archive_evidence_mismatch')
            prepared.append((sample, raw))
        self.volume.require_valid()
        for sample, raw in prepared:
            local=u64(sample['event_monotonic_us'])
            if self.fd is not None and (key(sample)!=self.current_key or self.count>=MAX_PART_EVENTS
                    or self.size+len(raw)>MAX_PART_BYTES or local-self.started>=self.segment_us
                    or any(sample[k]!=self.header[k] for k in ('rk_boot_id','collector_instance_id'))):
                self.finish()
            if self.fd is None: self._start(sample)
            if sample['sample_id'] in self.samples: raise ValueError('duplicate_archive_sample')
            self._write(raw); self.samples.add(sample['sample_id']); self.count+=1
        os.fsync(self.fd); self.volume.require_valid()

    def finish(self):
        if self.fd is None: return None
        try:
            self.volume.require_valid(); os.fsync(self.fd)
            data=inspect_file(self.fd,self.volume,self.relative)
            self.volume.write_json(self.relative+'.json',data)
            self.catalog.put(data)
            return data
        finally: os.close(self.fd); self.fd=None

    def abort(self):
        if self.fd is not None: os.close(self.fd); self.fd=None
