"""Local application installation transactions; recordings and compatible DB rows are retained."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen
import uuid

from .config import load as load_config
from .config_runtime import ORDER, UNITS, acknowledgement, fingerprint
from .gateway import load as load_gateway
from .release_contract import DATA, DEFAULT_FILES, family, runtime_units, system_files
from .release_migration import plan as migration_plan, require_unchanged
from .release_package import PackageError, digest, rename_new, unpack, validate_manifest
from .release_restore import (DATABASES, DATABASE_KINDS, FILES, RUN_UNITS, RestoreError, check_data, fsync_directory, load,
                              read_regular, restore, safe_path, save, transaction, verify_release, write_file)
from .snapshots import SnapshotReader

STOP_ORDER = ('ss07-application.service', 'ss07-api.service', 'ss07-gateway.service', 'ss07-maintenance.service',
              'ss07-storage.service', 'ss07-sonar.service', 'ss07-timecode.service') + tuple('ss07-camera@' + d + '.service' for d in ('down', 'right', 'left', 'back', 'front')) + (
              'ss07-media.service', 'ss07.target', 'ss07-config-recover.service')
BOOTSTRAP_UNIT = '''[Unit]
Description=SS-07 restore an interrupted application installation
After=local-fs.target
Before=ss07.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/bin/python3 /opt/ss07/recovery/restore.py
TimeoutStartSec=90
Environment=PYTHONDONTWRITEBYTECODE=1
UMask=0077
'''
BOOTSTRAP_DROPIN = '''[Unit]
Requires=ss07-release-recover.service
After=ss07-release-recover.service
ConditionPathExists=!/var/lib/ss07-deploy/in-progress
ConditionPathExists=/etc/ss07-provisioned
'''


class InstallError(ValueError):
    pass


def write_bootstrap(root):
    """Shared by packaging and the local installer, without any service actions."""
    from . import release_restore
    write_file(safe_path(root, 'opt/ss07/recovery/restore.py'), Path(release_restore.__file__).read_bytes(), 0o644)
    write_file(safe_path(root, 'usr/lib/systemd/system/ss07-release-recover.service'), BOOTSTRAP_UNIT.encode(), 0o644)
    for name in RUN_UNITS:
        if name == 'ss07.target': continue
        write_file(safe_path(root, 'etc/systemd/system/' + name + '.d/00-ss07-release.conf'), BOOTSTRAP_DROPIN.encode(), 0o644)


class SystemBackend:
    def __init__(self):
        self.reader = SnapshotReader('/run/ss07', Path('/proc/sys/kernel/random/boot_id').read_text().strip())
        self.confirmations = None
        self.gateway_path = Path('/etc/ss07-gateway/network.json')

    def command(self, action, units=()):
        if (action not in ('start', 'stop', 'reset-failed', 'daemon-reload', 'enable')
                or any(n not in RUN_UNITS + ('ss07-release-recover.service',) for n in units)
                or action == 'enable' and tuple(units) != ('ss07.target',)):
            raise InstallError('invalid_lifecycle_action')
        subprocess.run(['/bin/systemctl', '--no-ask-password', action] + list(units), check=True,
                       timeout=90, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def running(self):
        active = []
        for name in RUN_UNITS:
            result = subprocess.run(['/bin/systemctl', 'is-active', name], capture_output=True, text=True, timeout=5)
            if result.stdout.strip() in ('active', 'activating', 'reloading', 'deactivating'): active.append(name)
        return active

    def observe(self): return {name: self.reader.read(name) for name in ORDER + ('configuration',)}

    def prepare_users(self):
        subprocess.run(['/usr/bin/systemd-sysusers', '/usr/lib/sysusers.d/ss07.conf'], check=True, timeout=30)
        subprocess.run(['/usr/bin/systemd-tmpfiles', '--create', '/usr/lib/tmpfiles.d/ss07.conf'], check=True, timeout=30)

    def preflight(self, directory, root):
        if os.geteuid() != 0 or platform.machine() != 'aarch64': raise InstallError('root_on_arm64_required')
        env = dict(PATH='/usr/sbin:/usr/bin:/sbin:/bin', LANG='C.UTF-8',
                   PYTHONDONTWRITEBYTECODE='1', PYTHONPATH=str(directory / 'src'))
        value = subprocess.check_output([sys.executable, '-m', 'ss07', '--version'], env=env, timeout=10, text=True).strip()
        if value != directory.name: raise InstallError('application_runtime_version_mismatch')
        for name in DEFAULT_FILES:
            path = safe_path(root, name)
            if path.exists():
                subprocess.run([sys.executable, '-m', 'ss07', 'validate', '--config', str(path)], env=env,
                               check=True, timeout=10, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for name, flag in (('ss07-camera', '--version'), ('mediamtx', '--version'), ('caddy', 'version')):
            subprocess.run([str(directory / 'bin' / name), flag], check=True, timeout=10, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for name in ('v4l2src', 'h264parse', 'queue', 'capsfilter', 'rtph264pay', 'udpsink'):
            subprocess.run(['/usr/bin/gst-inspect-1.0', name], check=True, timeout=10, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def healthy(self, version, config, started, baseline):
        units = baseline.get('_running_units', [])
        supported = baseline.get('_supported_services', ORDER)
        required = [name for name in ORDER if name in supported and (UNITS[name] in units or 'ss07.target' in units)]
        if 'ss07-application.service' in units or 'ss07.target' in units: required.append('configuration')
        if 'api' in required:
            # The authenticated API validates Host even on its loopback socket.
            # Use the configured public authority, as the HTTPS proxy does.
            authority = urlsplit(load_gateway(self.gateway_path)['public_origin']).netloc
            request = Request('http://127.0.0.1:%d/healthz' % config['api']['port'], headers={'Host': authority})
            with urlopen(request, timeout=2) as response:
                data = json.loads(response.read(4097))['data']
            if data.get('app_version') != version: return False
        statuses = self.observe()
        self.last_degraded = {}
        for name in required:
            state = statuses[name]
            if int(state.get('observed_monotonic_us', '-1')) < started: return False
            if name == 'configuration':
                if state['state'] != 'RUNNING' or state.get('payload', {}).get('busy') or state.get('payload', {}).get('fatal'): return False
                continue
            if state.get('service_revision') != fingerprint(config, name): return False
            good, _ = acknowledgement(config, name, state, started)
            if good: continue
            if baseline.get('_activation') and ((name.startswith('camera.') or name in ('timecode', 'sonar', 'storage'))
                    and state['state'] in ('ERROR', 'UNAVAILABLE') or name == 'media' and state['state'] == 'RUNNING'):
                self.last_degraded[name] = state.get('payload', {}).get('reason') or 'hardware_not_ready'
                continue
            # Existing missing hardware must remain visibly degraded, without
            # making an otherwise functioning software upgrade impossible.
            previous = baseline.get(name, {})
            was_good, _ = acknowledgement(config, name, previous)
            if (was_good or previous.get('state') != state['state'] or state['state'] not in ('RUNNING', 'UNAVAILABLE', 'ERROR')
                    or previous.get('payload', {}).get('reason') != state.get('payload', {}).get('reason')): return False
        # A single newly written snapshot is not proof that a restarted worker
        # remains alive. Require advancing heartbeats from the same instances.
        samples = {name: (statuses[name]['instance_id'], int(statuses[name]['seq'])) for name in required}
        key = (version, started)
        before = getattr(self, 'confirmations', None)
        if before is None or before[0] != key or any(before[1].get(n, (None,))[0] != row[0] for n, row in samples.items()):
            self.confirmations = (key, samples)
            return not required
        if any(row[1] <= before[1][n][1] for n, row in samples.items()): return False
        return True


class Installer:
    def __init__(self, root=Path('/'), backend=None, timeout=45, settle=2):
        self.root = Path(root).resolve()
        self.backend = backend or SystemBackend()
        self.timeout, self.settle = timeout, settle
        self.state = safe_path(self.root, 'var/lib/ss07-deploy')
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = self.state.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise InstallError('unsafe_installation_directory')
        self.fd = None

    def __enter__(self):
        self.fd = os.open(str(self.state / 'lock'), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(self.fd).st_mode): raise InstallError('invalid_installation_lock')
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except Exception:
            os.close(self.fd); self.fd = None; raise
        return self

    def __exit__(self, *args):
        if self.fd is not None: os.close(self.fd); self.fd = None

    def _require_lock(self):
        if self.fd is None: raise InstallError('installation_lock_required')

    def _current(self):
        pointer = safe_path(self.root, 'opt/ss07/current')
        if not os.path.lexists(str(pointer)): return None, None
        if not pointer.is_symlink(): raise InstallError('invalid_current_pointer')
        link = os.readlink(str(pointer))
        metadata = verify_release(self.root, link)
        installed = load(self.state / 'installed.json')
        if (type(installed) is not dict or installed.get('schema_version') != 1
                or installed.get('version') != metadata['version'] or installed.get('data') != metadata['data']
                or re.fullmatch('[0-9a-f]{64}', str(installed.get('archive_sha256'))) is None
                or (installed.get('previous_sha256') is not None and re.fullmatch('[0-9a-f]{64}', str(installed['previous_sha256'])) is None)):
            raise InstallError('installed_release_receipt_mismatch')
        return link, metadata

    def _idle(self):
        if (self.state / 'in-progress').exists(): raise InstallError('installation_gate_requires_recovery')
        try: value = transaction(self.root)
        except FileNotFoundError: return
        if value['phase'] not in ('COMMITTED', 'ROLLED_BACK'): raise InstallError('installation_recovery_required')

    def _import(self, archive, checksum):
        directory = safe_path(self.root, 'var/lib/ss07-deploy/imports/' + checksum)
        if directory.is_symlink(): raise InstallError('unsafe_import_directory')
        if not directory.exists(): unpack(archive, checksum, directory)
        receipt = json.loads(read_regular(directory / 'SS07-IMPORT.json'))
        manifest = json.loads(read_regular(directory / 'SS07-MANIFEST.json', limit=1048576))
        deployment = validate_manifest(manifest, checksum)
        if (receipt.get('archive_sha256') != checksum or receipt.get('manifest_sha256') != digest(directory / 'SS07-MANIFEST.json')
                or receipt.get('deployment') != deployment): raise InstallError('import_receipt_mismatch')
        for name, expected in manifest['files'].items():
            path = safe_path(directory, name)
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
                raise InstallError('unsafe_imported_file')
            if digest(path) != expected: raise InstallError('imported_file_changed')
        actual = set()
        for path in directory.rglob('*'):
            name = str(path.relative_to(directory))
            if path.is_symlink():
                if name != 'opt/ss07/current' or os.readlink(str(path)) != 'releases/' + manifest['version']:
                    raise InstallError('unsafe_imported_link')
            elif path.is_file(): actual.add(name)
            elif not path.is_dir(): raise InstallError('unsafe_imported_file')
        if actual != set(manifest['files']) | {'SS07-MANIFEST.json', 'SS07-IMPORT.json'}: raise InstallError('imported_file_set_changed')
        return directory, manifest, deployment

    def _release(self, imported, manifest, deployment):
        version = manifest['version']; relative = 'opt/ss07/releases/' + version
        target = safe_path(self.root, relative)
        expected = {name[len(relative) + 1:]: checksum for name, checksum in manifest['files'].items() if name.startswith(relative + '/')}
        metadata = dict(schema_version=1, version=version, data=deployment['data'], files=expected)
        if target.exists():
            if verify_release(self.root, 'releases/' + version) != metadata: raise InstallError('release_version_already_has_different_contents')
            return target
        target.parent.mkdir(parents=True, exist_ok=True)
        needed = sum((imported / relative / name).stat().st_size for name in expected)
        if shutil.disk_usage(str(target.parent)).free < needed + 268435456: raise InstallError('insufficient_release_space')
        with tempfile.TemporaryDirectory(prefix='.new-', dir=str(target.parent)) as temp:
            directory = Path(temp) / version
            shutil.copytree(imported / relative, directory)
            for path in directory.rglob('*'):
                if path.is_symlink(): raise InstallError('release_symlink_not_allowed')
                if path.is_dir(): path.chmod(0o755)
                else:
                    path.chmod(0o755 if path.stat().st_mode & 0o111 else 0o644)
                    with path.open('rb') as stream: os.fsync(stream.fileno())
            directory.chmod(0o755); save(directory / '.ss07-release.json', metadata)
            for path in [n for n in directory.rglob('*') if n.is_dir()] + [directory]: fsync_directory(path)
            rename_new(directory, target); fsync_directory(target.parent)
        verify_release(self.root, 'releases/' + version)
        return target

    def _bootstrap(self, activate=True):
        # This helper remains available when current points to a failed version.
        write_bootstrap(self.root)
        if activate:
            self.backend.command('daemon-reload')
            self.backend.command('start', ['ss07-release-recover.service'])

    def _backup_databases(self, directory):
        records = {}
        for index, (name, (kind, identity)) in enumerate(DATABASES.items()):
            path = safe_path(self.root, name)
            if not path.exists(): continue
            info = path.stat()
            if shutil.disk_usage(str(directory)).free < info.st_size + 134217728: raise InstallError('insufficient_backup_space')
            destination = directory / ('database-' + str(index) + '.sqlite')
            source = sqlite3.connect('file:' + quote(str(path), safe='/') + '?mode=ro', uri=True, timeout=2)
            output = sqlite3.connect(str(destination), timeout=2)
            deadline = time.monotonic() + 30
            def progress(*_):
                if time.monotonic() > deadline: raise InstallError('database_backup_timeout')
                if shutil.disk_usage(str(directory)).free < 134217728: raise InstallError('insufficient_backup_space')
            try:
                source.backup(output, pages=128, progress=progress)
                if output.execute('PRAGMA quick_check').fetchall() != [('ok',)]: raise InstallError('database_backup_invalid')
            finally: source.close(); output.close()
            destination.chmod(0o600)
            with destination.open('rb') as stream: os.fsync(stream.fileno())
            records[name] = dict(file=destination.name, sha256=digest(destination), mode=stat.S_IMODE(info.st_mode), uid=info.st_uid, gid=info.st_gid)
        save(directory / 'databases.json', records)

    def _wait(self, version, config, started, baseline):
        deadline, stable = time.monotonic() + self.timeout, None
        while time.monotonic() < deadline:
            try: good = self.backend.healthy(version, config, started, baseline)
            except (OSError, ValueError, KeyError): good = False
            if good:
                if stable is None: stable = time.monotonic()
                if time.monotonic() - stable >= self.settle: return
            else: stable = None
            time.sleep(.2)
        raise InstallError('release_health_timeout')

    def install(self, archive, checksum):
        self._require_lock(); self._idle()
        if not isinstance(checksum, str) or re.fullmatch('[0-9a-f]{64}', checksum) is None: raise InstallError('invalid_archive_checksum')
        previous, old_metadata = self._current()
        # No automatic pruning of versions or data backups. A bounded number
        # of retained installations prevents unattended growth; offline cleanup
        # must preserve the current/previous versions and unresolved journal.
        histories = self.state / 'transactions'
        if histories.exists() and len(list(histories.iterdir())) >= 32: raise InstallError('installation_history_cleanup_required')
        if shutil.disk_usage(str(self.state)).free < 512 * 1024 * 1024: raise InstallError('insufficient_installation_space')
        imported, manifest, deployment = self._import(archive, checksum)
        migration = migration_plan(self.root, old_metadata['data'] if old_metadata else None, deployment['data'])
        if previous == 'releases/' + manifest['version']:
            self._release(imported, manifest, deployment)
            if load(self.state / 'installed.json')['archive_sha256'] != checksum: raise InstallError('same_version_different_archive')
            return dict(unchanged=True, version=manifest['version'])
        # Old consumers leave sonar files untouched; the new manager still
        # checks and backs them up, including during a supported downgrade.
        data_contract = {k: deployment['data'].get(k, DATA[k]) for k in DATABASE_KINDS}
        check_data(self.root, data_contract)
        candidate = self._release(imported, manifest, deployment)
        if migration['name'] == 'none': self.backend.preflight(candidate, self.root)
        else:
            # Validate the target's projected configuration without changing
            # either live configuration file before the transaction is durable.
            with tempfile.TemporaryDirectory(prefix='.preflight-', dir=str(self.state)) as temporary:
                for name in DEFAULT_FILES:
                    raw = migration['updates'].get(name, migration['originals'][name])
                    write_file(Path(temporary) / name, raw, 0o644)
                self.backend.preflight(candidate, Path(temporary))
        running, observations = self.backend.running(), self.backend.observe()
        previous_units = runtime_units(old_metadata['data']) if old_metadata else runtime_units(deployment['data'])
        target_units = runtime_units(deployment['data'])
        starting = [n for n in RUN_UNITS if n in target_units and
                    (n in running or n == 'ss07-sonar.service' and 'ss07.target' in running)]
        baseline = {'_running_units': running,
                    '_supported_services': [n for n in ORDER if UNITS[n] in previous_units]}
        for name in ORDER + ('configuration',):
            observed = observations.get(name, {})
            payload = observed.get('payload', {})
            baseline[name] = dict(state=observed.get('state'), service_revision=observed.get('service_revision'),
                observed_monotonic_us=observed.get('observed_monotonic_us', '0'),
                payload={k: payload[k] for k in ('reason', 'capture_state', 'publish_state', 'gate') if k in payload})
            if name == 'media': baseline[name]['payload']['streams'] = {'sdi': {'ready': payload.get('streams', {}).get('sdi', {}).get('ready')}}
        if not previous and running: raise InstallError('unregistered_running_services')
        self._bootstrap()
        identity = str(uuid.uuid4()); directory = self.state / 'transactions' / identity
        directory.mkdir(parents=True, mode=0o700)
        replacements = {name: (imported / name).read_bytes() for name in system_files(deployment['data'])}
        if old_metadata and family(old_metadata['data']) == 'sonar' and family(deployment['data']) == 'legacy':
            replacements['usr/lib/systemd/system/ss07-sonar.service'] = None
        for name in DEFAULT_FILES:
            if not os.path.lexists(str(safe_path(self.root, name))): replacements[name] = (imported / name).read_bytes()
        replacements.update(migration['updates'])
        prior = load(self.state / 'installed.json') if previous else None
        installed = dict(schema_version=1, version=manifest['version'], archive_sha256=checksum, data=deployment['data'],
                         previous_sha256=prior['archive_sha256'] if prior else None)
        replacements['var/lib/ss07-deploy/installed.json'] = (json.dumps(installed, indent=2) + '\n').encode()
        files = {}
        require_unchanged(self.root, migration['originals'])
        for name, new in replacements.items():
            path = safe_path(self.root, name)
            try:
                raw = read_regular(path); info = path.stat()
                write_file(directory / ('file-' + str(FILES.index(name))), raw)
                old_hash, mode, uid, gid = hashlib.sha256(raw).hexdigest(), stat.S_IMODE(info.st_mode), info.st_uid, info.st_gid
            except FileNotFoundError: old_hash, mode, uid, gid = None, 0o644, os.geteuid(), os.getegid()
            files[name] = dict(previous_sha256=old_hash, new_sha256=hashlib.sha256(new).hexdigest() if new is not None else None,
                              mode=mode, uid=uid, gid=gid)
        save(directory / 'migration.json', dict(schema_version=1, name=migration['name'],
            previous=old_metadata['data'] if old_metadata else None, target=deployment['data'],
            history_archived=migration['history_archived'], changed_files=sorted(migration['updates']),
            input_sha256={n: hashlib.sha256(raw).hexdigest() for n, raw in migration['originals'].items()}))
        value = dict(schema_version=1, operation_id=identity, phase='PREPARED', previous=previous, candidate='releases/' + manifest['version'],
                     files=files, running_before=running, data_contract=data_contract, baseline=baseline,
                     error=None, files_restored=False, service_health_verified=False)
        path = self.state / 'transaction.json'; save(path, value)
        write_file(self.state / 'in-progress', identity.encode())
        try:
            value['phase'] = 'STOPPING'; save(path, value)
            if previous:
                self.backend.command('stop', ['ss07-application.service'])
                try:
                    state = json.loads(read_regular(safe_path(self.root, 'var/lib/ss07-apply/transaction.json')))
                    if state.get('phase') not in ('APPLIED', 'ROLLED_BACK', 'FAILED'): raise InstallError('configuration_recovery_required')
                except FileNotFoundError: pass
                self.backend.command('stop', self._available(STOP_ORDER[1:]))
            check_data(self.root, data_contract); self._backup_databases(directory)
            require_unchanged(self.root, migration['originals'])
            value['phase'] = 'SWITCHING'; save(path, value)
            for name, raw in replacements.items():
                target = safe_path(self.root, name)
                if raw is None:
                    try: target.unlink(); fsync_directory(target.parent)
                    except FileNotFoundError: pass
                elif name in migration['updates']:
                    item = files[name]
                    write_file(target, raw, item['mode'], item['uid'], item['gid'])
                else: write_file(target, raw, 0o600 if name.endswith('installed.json') else 0o644)
            pointer = safe_path(self.root, 'opt/ss07/current'); temporary = pointer.with_name('.switch-' + identity)
            try:
                temporary.symlink_to(value['candidate']); os.replace(str(temporary), str(pointer)); fsync_directory(pointer.parent)
            finally:
                if temporary.is_symlink(): temporary.unlink()
            self.backend.prepare_users(); self.backend.command('daemon-reload')
            value['phase'] = 'STARTING'; save(path, value)
            (self.state / 'in-progress').unlink(); fsync_directory(self.state)
            if running:
                # Bootstrap sees an unfinished transaction on a fresh boot. In
                # this already-running manager the recovery oneshot must first
                # have been started before PREPARED was written.
                started = time.monotonic_ns() // 1000
                self.backend.command('reset-failed', starting); self.backend.command('start', starting)
                value['phase'] = 'VERIFYING'; save(path, value)
                target_baseline = dict(baseline, _running_units=starting,
                    _supported_services=[n for n in ORDER if UNITS[n] in target_units])
                self._wait(manifest['version'], load_config(safe_path(self.root, 'etc/ss07-active/system.json')), started, target_baseline)
                value['service_health_verified'] = True
            value['phase'] = 'COMMITTED'; save(path, value)
            return dict(version=manifest['version'], operation_id=identity, activated=bool(running),
                        software_health_verified=bool(running), hardware_verified=False, migration=migration['name'])
        except BaseException:
            # Interruptions leave a durable recovery path even if stopping or
            # filesystem repair fails. The caller cannot claim a completed install.
            try:
                self.recover(identity)
            except BaseException:
                failed = transaction(self.root); failed.update(phase='ROLLBACK_FAILED', service_health_verified=False, error='installation_recovery_required')
                save(path, failed)
            raise

    def _available(self, names):
        return [n for n in names if safe_path(self.root, 'usr/lib/systemd/system/' +
            ('ss07-camera@.service' if n.startswith('ss07-camera@') else n)).is_file()]

    def recover(self, expected_operation_id):
        self._require_lock()
        value = transaction(self.root)
        if value['operation_id'] != expected_operation_id: raise InstallError('installation_operation_changed')
        if value['phase'] in ('COMMITTED', 'ROLLED_BACK'): return dict(recovery_required=False)
        write_file(self.state / 'in-progress', expected_operation_id.encode())
        available = self._available(STOP_ORDER)
        stop_error = None
        try:
            if available: self.backend.command('stop', available)
        except (OSError, ValueError, subprocess.SubprocessError) as exc: stop_error = exc
        if stop_error is not None:
            raise InstallError('service_stop_not_confirmed') from stop_error
        restored = restore(self.root)
        self.backend.command('daemon-reload')
        running = value['running_before']
        if running:
            started = time.monotonic_ns() // 1000
            self.backend.command('reset-failed', running); self.backend.command('start', running)
            self._wait(value['previous'].split('/')[1], load_config(safe_path(self.root, 'etc/ss07-active/system.json')),
                       started, value['baseline'])
        restored.update(phase='ROLLED_BACK', service_health_verified=bool(running), error='installation_failed')
        save(self.state / 'transaction.json', restored)
        return dict(recovery_required=True, files_restored=True, software_health_verified=bool(running), hardware_verified=False)

    def rollback(self, expected_version):
        """Start a new same-contract transaction, preserving current data/config."""
        self._require_lock(); self._idle()
        previous, metadata = self._current()
        if metadata is None or metadata['version'] != expected_version: raise InstallError('installed_version_changed')
        checksum = load(self.state / 'installed.json').get('previous_sha256')
        if checksum is None: raise InstallError('previous_package_unavailable')
        # The import cache is revalidated by install; no package execution or
        # external download is required to select the previously verified code.
        if not (self.state / 'imports' / checksum).is_dir(): raise InstallError('previous_package_unavailable')
        return self.install(None, checksum)

    def register_image(self, imported):
        """Factory builder only: register an already-copied, verified new rootfs."""
        self._require_lock()
        if (self.state / 'installed.json').exists() or (self.state / 'transaction.json').exists():
            raise InstallError('image_already_registered')
        receipt = json.loads(read_regular(Path(imported) / 'SS07-IMPORT.json'))
        checksum = receipt.get('archive_sha256')
        if re.fullmatch('[0-9a-f]{64}', str(checksum)) is None: raise InstallError('invalid_archive_checksum')
        cache = self.state / 'imports' / checksum
        cache.parent.mkdir(parents=True, exist_ok=True)
        # The input is the builder's bounded unpack result, never a live root.
        # Preserve it for rollback after the first field upgrade.
        if not cache.exists():
            with tempfile.TemporaryDirectory(prefix='.factory-', dir=str(cache.parent)) as temp:
                copied = Path(temp) / 'import'; shutil.copytree(imported, copied, symlinks=True)
                copied.chmod(0o700); rename_new(copied, cache); fsync_directory(cache.parent)
        _, manifest, deployment = self._import(None, checksum)
        family(deployment['data'])
        version = manifest['version']; link = 'releases/' + version
        pointer = safe_path(self.root, 'opt/ss07/current')
        if not pointer.is_symlink() or os.readlink(str(pointer)) != link: raise InstallError('factory_pointer_mismatch')
        relative = 'opt/ss07/' + link + '/'; directory = safe_path(self.root, relative[:-1])
        expected = {name[len(relative):]: value for name, value in manifest['files'].items() if name.startswith(relative)}
        actual = {str(p.relative_to(directory)) for p in directory.rglob('*') if p.is_file()}
        if actual != set(expected) or any(p.is_symlink() for p in directory.rglob('*')): raise InstallError('factory_file_set_mismatch')
        for name, expected_digest in manifest['files'].items():
            if digest(safe_path(self.root, name)) != expected_digest: raise InstallError('factory_payload_mismatch')
        save(directory / '.ss07-release.json', dict(schema_version=1, version=version, data=deployment['data'], files=expected))
        verify_release(self.root, link)
        save(self.state / 'installed.json', dict(schema_version=1, version=version, archive_sha256=checksum,
                                               previous_sha256=None, data=deployment['data']))
        self._bootstrap(activate=False)
        return dict(version=version, factory_registered=True, activated=False, hardware_verified=False)

    def status(self):
        integrity_error = None
        try: current, metadata = self._current()
        except (OSError, ValueError) as exc:
            current, metadata = None, None
            integrity_error = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        try: value = transaction(self.root)
        except FileNotFoundError: value = None
        try: activation = load(self.state / 'activation.json')
        except FileNotFoundError: activation = None
        return dict(installed_version=metadata['version'] if metadata else None, current=current, activation=activation,
                    installed_integrity_error=integrity_error,
                    provisioned=safe_path(self.root, 'etc/ss07-provisioned').is_file(),
                    transaction={k: value[k] for k in ('operation_id', 'phase', 'previous', 'candidate', 'files_restored',
                        'service_health_verified', 'error')} if value else None,
                    running_units=self.backend.running(), hardware_verified=False)

    def activate(self):
        """First activation only; initialized secrets/configuration must already exist."""
        from .provisioning import check
        self._require_lock(); self._idle()
        current, metadata = self._current()
        if current is None: raise InstallError('application_not_installed')
        marker = safe_path(self.root, 'etc/ss07-provisioned')
        if os.path.lexists(str(marker)): raise InstallError('already_provisioned')
        if set(self.backend.running()) - {'ss07.target', 'ss07-config-recover.service'}:
            raise InstallError('services_running_before_activation')
        readiness = check(self.root)
        self.backend.preflight(safe_path(self.root, 'opt/ss07/' + current), self.root)
        self._bootstrap()
        identity = str(uuid.uuid4())
        raw = (json.dumps(dict(schema_version=1, operation_id=identity, version=metadata['version'],
                               config_revision=readiness['config_revision'])) + '\n').encode()
        value = dict(schema_version=1, operation_id=identity, phase='PREPARED',
                     marker_sha256=hashlib.sha256(raw).hexdigest(), error=None)
        state = self.state / 'activation.json'; save(state, value)
        try:
            write_file(marker, raw, 0o644)
            value['phase'] = 'STARTING'; save(state, value)
            started = time.monotonic_ns() // 1000
            units = runtime_units(metadata['data'])
            self.backend.command('reset-failed', units); self.backend.command('start', units)
            self._wait(metadata['version'], load_config(safe_path(self.root, 'etc/ss07-active/system.json')),
                       started, {'_running_units': list(units), '_activation': True,
                                 '_supported_services': [n for n in ORDER if UNITS[n] in units]})
            self.backend.command('enable', ['ss07.target'])
            value['phase'] = 'APPLIED'; save(state, value)
            return dict(activated=True, version=metadata['version'], software_health_verified=True,
                        degraded_services=getattr(self.backend, 'last_degraded', {}), hardware_verified=False)
        except BaseException:
            # Removing the marker prevents automatic restarts even if stopping
            # fails; the error remains visible and is not reported as success.
            if marker.exists() and hashlib.sha256(read_regular(marker)).hexdigest() == value['marker_sha256']:
                marker.unlink(); fsync_directory(marker.parent)
            value.update(phase='FAILED', error='activation_failed'); save(state, value)
            self.backend.command('stop', self._available(STOP_ORDER))
            raise
