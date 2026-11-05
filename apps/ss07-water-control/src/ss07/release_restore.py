"""Standalone boot recovery. Installed outside current; imports only the standard library."""
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
from urllib.parse import quote

LEGACY_UNIT_NAMES = ('ss07-api.service', 'ss07-application.service', 'ss07-camera@.service',
              'ss07-config-recover.service', 'ss07-gateway.service', 'ss07-maintenance.service',
              'ss07-media.service', 'ss07-storage.service', 'ss07-timecode.service', 'ss07.target')
UNIT_NAMES = LEGACY_UNIT_NAMES + ('ss07-sonar.service',)
# file-N backups are referenced by their historical position. Append new files
# after the entire old table, never between old units and configuration files.
FILES = tuple('usr/lib/systemd/system/' + n for n in LEGACY_UNIT_NAMES) + (
    'usr/lib/sysusers.d/ss07.conf', 'usr/lib/tmpfiles.d/ss07.conf',
    'etc/ss07/system.json', 'etc/ss07-active/system.json', 'var/lib/ss07-deploy/installed.json',
    'usr/lib/systemd/system/ss07-sonar.service',
    'var/lib/ss07-apply/transaction.json', 'var/lib/ss07-apply/previous.json', 'var/lib/ss07-apply/candidate.json')
RUN_UNITS = tuple(n for n in LEGACY_UNIT_NAMES if '@' not in n) + tuple('ss07-camera@' + d + '.service' for d in ('front', 'back', 'left', 'right', 'down')) + ('ss07-sonar.service',)
LEGACY_DATABASE_KINDS = ('catalog', 'timecode_journal', 'audit')
DATABASE_KINDS = LEGACY_DATABASE_KINDS + ('sonar_journal', 'sonar_archive_catalog')
DATABASES = {
    'var/lib/ss07/catalog.sqlite': ('catalog', 0x53533037),
    'var/lib/ss07/timecode/journal.sqlite': ('timecode_journal', 0x53535443),
    'var/lib/ss07/maintenance.sqlite': ('audit', 0x53534155),
    'var/lib/ss07-api/access.sqlite': ('audit', 0x53534155),
    'var/lib/ss07-apply/audit.sqlite': ('audit', 0x53534155),
    'var/lib/ss07-media/access.sqlite': ('audit', 0x53534155),
    'var/lib/ss07/sonar/samples.sqlite': ('sonar_journal', 0x5353534e),
    'var/lib/ss07/sonar/archives.sqlite': ('sonar_archive_catalog', 0x53535341)}
PHASES = {'PREPARED', 'STOPPING', 'SWITCHING', 'STARTING', 'VERIFYING', 'COMMITTED',
          'ROLLING_BACK', 'RECOVERY_PENDING', 'ROLLED_BACK', 'ROLLBACK_FAILED'}
UUID = r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'
RELEASE = r'releases/(?:0|[1-9][0-9]{0,3})\.(?:0|[1-9][0-9]{0,3})\.(?:0|[1-9][0-9]{0,3})'


class RestoreError(ValueError):
    pass


def safe_path(root, name):
    if not name or name.startswith('/') or any(p in ('', '.', '..') for p in name.split('/')):
        raise RestoreError('invalid_managed_path')
    path = Path(root)
    for part in name.split('/')[:-1]:
        path = path / part
        try:
            info = path.lstat()
            if not stat.S_ISDIR(info.st_mode): raise RestoreError('managed_parent_is_not_directory')
        except FileNotFoundError: pass
    return Path(root) / name


def read_regular(path, limit=65536, private=False):
    fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as source:
        info = os.fstat(source.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit
                or (private and (info.st_uid != os.geteuid() or info.st_mode & 0o077))):
            raise RestoreError('unsafe_release_state')
        raw = source.read(limit + 1)
        if len(raw) > limit: raise RestoreError('release_state_too_large')
        return raw


def fsync_directory(path):
    fd = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try: os.fsync(fd)
    finally: os.close(fd)


def write_file(path, raw, mode=0o600, uid=None, gid=None):
    path = Path(path)
    if path.is_symlink(): raise RestoreError('managed_file_is_symlink')
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix='.' + path.name + '-', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'wb') as stream:
            if uid is not None or gid is not None: os.fchown(stream.fileno(), -1 if uid is None else uid, -1 if gid is None else gid)
            os.fchmod(stream.fileno(), mode); stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        os.replace(temp, str(path)); fsync_directory(path.parent)
    finally:
        if os.path.lexists(temp): os.unlink(temp)


def save(path, value):
    raw = (json.dumps(value, allow_nan=False, indent=2) + '\n').encode()
    if len(raw) > 65536: raise RestoreError('release_state_too_large')
    write_file(path, raw)


def load(path):
    def unique(pairs):
        result = {}
        for k, v in pairs:
            if k in result: raise RestoreError('duplicate_release_state_field')
            result[k] = v
        return result
    return json.loads(read_regular(path, private=True), object_pairs_hook=unique,
                      parse_constant=lambda _: (_ for _ in ()).throw(RestoreError('invalid_release_state_number')))


def transaction(root):
    value = load(safe_path(root, 'var/lib/ss07-deploy/transaction.json'))
    fields = {'schema_version', 'operation_id', 'phase', 'previous', 'candidate', 'files', 'running_before',
              'data_contract', 'baseline', 'error', 'files_restored', 'service_health_verified'}
    if (type(value) is not dict or set(value) != fields or type(value['schema_version']) is not int or value['schema_version'] != 1
            or not isinstance(value['phase'], str) or value['phase'] not in PHASES
            or not isinstance(value['operation_id'], str) or re.fullmatch(UUID, value['operation_id']) is None
            or (value['previous'] is not None and (not isinstance(value['previous'], str) or re.fullmatch(RELEASE, value['previous']) is None))
            or not isinstance(value['candidate'], str) or re.fullmatch(RELEASE, value['candidate']) is None
            or type(value['files']) is not dict or set(value['files']) - set(FILES)
            or type(value['running_before']) is not list or value['running_before'] != [n for n in RUN_UNITS if n in value['running_before']]
            or type(value['data_contract']) is not dict
            or set(value['data_contract']) not in (set(LEGACY_DATABASE_KINDS), set(DATABASE_KINDS))
            or type(value['baseline']) is not dict
            or any(type(n) is not int or not 1 <= n <= 1000 for n in value['data_contract'].values())
            or type(value['files_restored']) is not bool or type(value['service_health_verified']) is not bool
            or (value['error'] is not None and (not isinstance(value['error'], str) or re.fullmatch('[a-z_]{1,96}', value['error']) is None))):
        raise RestoreError('invalid_release_transaction')
    for name, item in value['files'].items():
        if (type(item) is not dict or set(item) != {'previous_sha256', 'new_sha256', 'mode', 'uid', 'gid'}
                or any(item[k] is not None and (not isinstance(item[k], str) or re.fullmatch('[0-9a-f]{64}', item[k]) is None)
                       for k in ('previous_sha256', 'new_sha256'))
                or type(item['mode']) is not int or not 0 <= item['mode'] <= 0o777
                or any(type(item[k]) is not int or not 0 <= item[k] < 2**32-1 for k in ('uid', 'gid'))):
            raise RestoreError('invalid_release_file_backup')
    return value


def check_data(root, contract):
    if (type(contract) is not dict or set(contract) not in (set(LEGACY_DATABASE_KINDS), set(DATABASE_KINDS))
            or any(type(v) is not int or not 1 <= v <= 1000 for v in contract.values())):
        raise RestoreError('invalid_recovery_data_contract')
    result = {}
    for name, (kind, app_id) in DATABASES.items():
        # A pre-sonar transaction never opened these files; retain them untouched.
        # This compatibility is for interrupted-journal recovery, not permission
        # to install or roll back across different application data contracts.
        if kind not in contract: continue
        path = safe_path(root, name)
        try:
            fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError: continue
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode): raise RestoreError('database_not_regular')
        finally: os.close(fd)
        db = sqlite3.connect('file:' + quote(str(path), safe='/') + '?mode=ro', uri=True, timeout=2)
        try:
            db.execute('PRAGMA query_only=ON')
            identity = db.execute('PRAGMA application_id').fetchone()[0]
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if identity != app_id or version != contract[kind]: raise RestoreError('offline_database_migration_required')
            result[name] = version
        finally: db.close()
    return result


def verify_release(root, link):
    if not isinstance(link, str) or re.fullmatch(RELEASE, link) is None: raise RestoreError('invalid_release_pointer')
    directory = safe_path(root, 'opt/ss07/' + link)
    if directory.is_symlink() or not directory.is_dir(): raise RestoreError('previous_release_missing')
    metadata = load(directory / '.ss07-release.json')
    if (type(metadata) is not dict or set(metadata) != {'schema_version', 'version', 'data', 'files'}
            or type(metadata['schema_version']) is not int or metadata['schema_version'] != 1
            or metadata['version'] != link.split('/')[1] or type(metadata['files']) is not dict
            or not 1 <= len(metadata['files']) <= 1024 or type(metadata['data']) is not dict):
        raise RestoreError('invalid_release_receipt')
    for name, expected in metadata['files'].items():
        if (not isinstance(name, str) or name == '.ss07-release.json' or not isinstance(expected, str)
                or re.fullmatch('[0-9a-f]{64}', expected) is None): raise RestoreError('invalid_release_receipt')
        path = safe_path(directory, name)
        fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
                raise RestoreError('unsafe_release_file')
            actual = hashlib.sha256()
            for block in iter(lambda: source.read(1024 * 1024), b''): actual.update(block)
            if actual.hexdigest() != expected: raise RestoreError('previous_release_digest_mismatch')
    actual_files = set()
    for path in directory.rglob('*'):
        if path.is_symlink(): raise RestoreError('release_contains_symlink')
        if path.is_file(): actual_files.add(str(path.relative_to(directory)))
    if actual_files != set(metadata['files']) | {'.ss07-release.json'}: raise RestoreError('release_file_set_changed')
    return metadata


def restore(root, value=None):
    """Restore code pointer/templates only. Never roll back or discard later recordings/DB rows."""
    root = Path(root); value = value or transaction(root)
    pointer = safe_path(root, 'opt/ss07/current')
    current = os.readlink(str(pointer)) if pointer.is_symlink() else None
    if pointer.exists() and not pointer.is_symlink(): raise RestoreError('invalid_current_pointer')
    if current not in (value['previous'], value['candidate'], None): raise RestoreError('active_release_drift')
    if value['previous'] is not None: verify_release(root, value['previous'])
    directory = safe_path(root, 'var/lib/ss07-deploy/transactions/' + value['operation_id'])
    # Validate all recovery inputs before touching any file.
    contents = {}
    for name, item in value['files'].items():
        if item['previous_sha256'] is not None:
            backup = directory / ('file-' + str(FILES.index(name)))
            raw = read_regular(backup, private=True)
            if hashlib.sha256(raw).hexdigest() != item['previous_sha256']: raise RestoreError('release_backup_digest_mismatch')
            contents[name] = raw
        target = safe_path(root, name)
        try: actual = hashlib.sha256(read_regular(target)).hexdigest()
        except FileNotFoundError: actual = None
        if actual not in (item['previous_sha256'], item['new_sha256'], None): raise RestoreError('managed_file_drift')
    state_path = safe_path(root, 'var/lib/ss07-deploy/transaction.json')
    value.update(phase='ROLLING_BACK', service_health_verified=False); save(state_path, value)
    for name, item in value['files'].items():
        path = safe_path(root, name)
        if name in contents: write_file(path, contents[name], item['mode'], item['uid'], item['gid'])
        else:
            try: path.unlink(); fsync_directory(path.parent)
            except FileNotFoundError: pass
    pointer.parent.mkdir(parents=True, exist_ok=True)
    if value['previous'] is None:
        try: pointer.unlink()
        except FileNotFoundError: pass
    else:
        temporary = pointer.with_name('.restore-' + value['operation_id'])
        try: temporary.symlink_to(value['previous']); os.replace(str(temporary), str(pointer))
        finally:
            if temporary.is_symlink(): temporary.unlink()
    fsync_directory(pointer.parent)
    value['files_restored'] = True; save(state_path, value)
    # A schema change cannot be undone by selecting older Python files. Keep
    # consumers gated until an explicit offline data recovery/migration occurs.
    check_data(root, value['data_contract'])
    value['phase'] = 'RECOVERY_PENDING'; save(state_path, value)
    gate = safe_path(root, 'var/lib/ss07-deploy/in-progress')
    try: gate.unlink(); fsync_directory(gate.parent)
    except FileNotFoundError: pass
    return value


def boot(root=Path('/')):
    # Activation is an explicit separate operation. A power loss before its
    # health acknowledgement must not silently enable the product on next boot.
    activation_path = safe_path(root, 'var/lib/ss07-deploy/activation.json')
    try: activation = load(activation_path)
    except FileNotFoundError: activation = None
    if activation is not None:
        if (type(activation) is not dict or set(activation) != {'schema_version', 'operation_id', 'phase', 'marker_sha256', 'error'}
                or activation['schema_version'] != 1 or re.fullmatch(UUID, str(activation['operation_id'])) is None
                or re.fullmatch('[0-9a-f]{64}', str(activation['marker_sha256'])) is None
                or activation['phase'] not in ('PREPARED', 'STARTING', 'APPLIED', 'FAILED')):
            raise RestoreError('invalid_activation_transaction')
        if activation['phase'] in ('PREPARED', 'STARTING'):
            marker = safe_path(root, 'etc/ss07-provisioned')
            try:
                if hashlib.sha256(read_regular(marker)).hexdigest() != activation['marker_sha256']:
                    raise RestoreError('provisioning_marker_drift')
                marker.unlink(); fsync_directory(marker.parent)
            except FileNotFoundError: pass
            activation.update(phase='FAILED', error='activation_interrupted'); save(activation_path, activation)
    try: value = transaction(root)
    except FileNotFoundError: return {'recovery_required': False}
    if value['phase'] in ('COMMITTED', 'ROLLED_BACK'):
        return {'recovery_required': False}
    restore(root, value)
    return {'recovery_required': True, 'files_restored': True, 'service_health_verified': False}


if __name__ == '__main__':
    if os.geteuid() != 0: raise SystemExit('root required')
    try: print(json.dumps(boot()))
    except (OSError, ValueError, sqlite3.Error): raise SystemExit('SS-07 release recovery failed; consumers remain gated')
