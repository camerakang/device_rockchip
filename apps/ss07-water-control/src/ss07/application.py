"""Durable, bounded configuration application with an explicit recovery journal."""
import copy
import os
import re
import stat
import subprocess
import threading
import time
import uuid
from pathlib import Path

from .config import ConfigError, decode_json, load, revision
from .audit import Audit
from .config_runtime import ORDER, UNITS, acknowledgement, fingerprint, plan
from .persistence import ConfigStore, RevisionConflict, atomic_json
from .snapshots import SnapshotReader
from .worker import monotonic_us

TERMINAL = {'APPLIED', 'ROLLED_BACK', 'ROLLBACK_FAILED', 'FAILED'}
PHASES = TERMINAL | {'PREPARED', 'STOPPING', 'SWITCHING', 'STARTING', 'VERIFYING', 'ROLLING_BACK', 'RECOVERY_PENDING'}
RESULT_STATES = {'PENDING', 'STOPPING', 'STOPPED', 'STARTING', 'VERIFYING', 'VERIFIED', 'ERROR'}
UUID_PATTERN = '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'


def matches(value, pattern):
    return isinstance(value, str) and re.fullmatch(pattern, value) is not None


class ApplyError(ConfigError):
    pass


class InterruptedApply(BaseException):
    pass


def read_owned(path):
    fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise ApplyError('unsafe_application_journal')
        return decode_json(source.read(65537))


def journal(directory):
    value = read_owned(Path(directory) / 'transaction.json')
    fields = {'schema_version', 'operation_id', 'actor', 'phase', 'previous_revision', 'target_revision',
              'services', 'results', 'error', 'configuration_restored'}
    if (type(value) is not dict or set(value) != fields or type(value['schema_version']) is not int or value['schema_version'] != 1
            or not isinstance(value['phase'], str) or value['phase'] not in PHASES
            or not matches(value['operation_id'], UUID_PATTERN)
            or not matches(value['actor'], '[a-zA-Z0-9_.-]{1,64}')
            or any(not matches(value[k], '[0-9a-f]{64}') for k in ('previous_revision', 'target_revision'))
            or type(value['services']) is not list or value['services'] != [n for n in ORDER if n in value['services']]
            or type(value['results']) is not dict or set(value['results']) != set(value['services'])
            or (value['error'] is not None and not matches(value['error'], '[a-zA-Z0-9_]{1,96}'))
            or type(value['configuration_restored']) is not bool):
        raise ApplyError('invalid_application_journal')
    for result in value['results'].values():
        if (type(result) is not dict or set(result) not in ({'state', 'reason'}, {'state', 'reason', 'instance_id', 'service_revision'})
                or not isinstance(result['state'], str) or result['state'] not in RESULT_STATES
                or (result['reason'] is not None and not matches(result['reason'], '[a-zA-Z0-9_]{1,96}'))
                or (result.get('instance_id') is not None and not matches(result['instance_id'], UUID_PATTERN))
                or (result.get('service_revision') is not None and not matches(result['service_revision'], '[0-9a-f]{64}'))):
            raise ApplyError('invalid_application_journal')
    return value


def recover_before_start(active_path, directory):
    """Boot oneshot: restore the old file before ANY consumer starts; no unit actions."""
    try: value = journal(directory)
    except FileNotFoundError:
        load(active_path)
        return {'recovery_required': False}
    if value['phase'] in ('APPLIED', 'ROLLED_BACK', 'FAILED'):
        expected = value['target_revision'] if value['phase'] == 'APPLIED' else value['previous_revision']
        if revision(load(active_path)) != expected: raise ApplyError('active_configuration_drift')
        return {'recovery_required': False}
    previous = read_owned(Path(directory) / 'previous.json')
    if revision(previous) != value['previous_revision']: raise ApplyError('recovery_configuration_mismatch')
    # Validation includes all schema/binding constraints before any file switch.
    from .config import validate
    validate(previous)
    atomic_json(active_path, previous, mode=0o644)
    value.update(phase='RECOVERY_PENDING', error='interrupted_application', configuration_restored=True)
    atomic_json(Path(directory) / 'transaction.json', value)
    return {'recovery_required': True, 'operation_id': value['operation_id']}


class SystemdBackend:
    """Only fixed SS-07 unit names; never accepts commands or unit names from HTTP."""
    def __init__(self, state_dir):
        self.reader = SnapshotReader(state_dir, Path('/proc/sys/kernel/random/boot_id').read_text().strip())

    def action(self, action, service):
        if action not in ('stop', 'start') or service not in UNITS: raise ApplyError('invalid_service_action')
        command = ['/bin/systemctl', '--no-ask-password', action, UNITS[service]]
        try:
            result = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    timeout=25, env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C'})
        except (OSError, subprocess.TimeoutExpired) as exc: raise ApplyError('service_action_timeout') from exc
        if result.returncode: raise ApplyError('service_action_failed')

    def observe(self, service):
        return self.reader.read(service)


class Application:
    def __init__(self, desired_path, active_path, directory, backend, gateway=None, timeout=25, settle=2, stop=None, rtsp_profile=None):
        self.desired, self.active, self.directory = Path(desired_path), Path(active_path), Path(directory)
        self.backend, self.gateway = backend, gateway
        self.rtsp_profile = rtsp_profile
        self.timeout, self.settle = timeout, settle
        self.stop = stop or threading.Event()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.audit = Audit(self.directory / 'audit.sqlite'); self.audit.initialize()
        self.guard = threading.RLock()
        self.thread = None
        self.current = None
        self.fatal = None
        try: self.current = journal(self.directory)
        except FileNotFoundError: pass
        self.path = self.directory / 'transaction.json'

    def status(self):
        with self.guard:
            return {'transaction': copy.deepcopy(self.current), 'busy': self.thread is not None and self.thread.is_alive(),
                    'fatal': self.fatal, 'hardware_verified': False}

    def _save(self, **values):
        with self.guard:
            candidate = dict(self.current, **values)
            atomic_json(self.path, candidate)
            self.current = candidate

    def _result(self, service, state, reason=None, observed=None):
        with self.guard:
            values = copy.deepcopy(self.current['results'])
            values[service] = {'state': state, 'reason': reason}
            if observed is not None:
                values[service].update(instance_id=observed.get('instance_id'),
                                       service_revision=observed.get('service_revision'))
            self._save(results=values)

    def preview(self, expected_revision, expected_active_revision):
        for value in (expected_revision, expected_active_revision):
            if not isinstance(value, str) or re.fullmatch('[0-9a-f]{64}', value) is None:
                raise ApplyError('invalid_configuration_revision')
        with ConfigStore(self.desired).locked():
            candidate = load(self.desired)
            if revision(candidate) != expected_revision: raise RevisionConflict('desired_configuration_changed')
        previous = load(self.active)
        if revision(previous) != expected_active_revision: raise RevisionConflict('active_configuration_changed')
        if self.gateway is not None:
            from .gateway import render
            render(candidate, self.gateway)
        if self.rtsp_profile is not None:
            from .rtsp_access import check_ports
            check_ports(candidate, self.gateway, self.rtsp_profile)
        return previous, candidate, plan(previous, candidate)

    def submit(self, expected_revision, expected_active_revision, actor, identity=None):
        with self.guard:
            if self.thread is not None and self.thread.is_alive(): raise ApplyError('configuration_apply_busy')
            if self.fatal: raise ApplyError('configuration_recovery_required')
            if self.current and self.current['phase'] not in TERMINAL: raise ApplyError('configuration_recovery_required')
            if self.current and self.current['phase'] == 'ROLLBACK_FAILED': raise ApplyError('configuration_recovery_required')
            if self.current:
                expected = self.current['target_revision'] if self.current['phase'] == 'APPLIED' else self.current['previous_revision']
                if revision(load(self.active)) != expected: raise ApplyError('active_configuration_drift')
            previous, candidate, planned = self.preview(expected_revision, expected_active_revision)
            if not planned['services']:
                return dict(unchanged=True, active_revision=expected_active_revision, hardware_verified=False)
            identity = identity or str(uuid.uuid4())
            if str(uuid.UUID(identity)) != identity: raise ApplyError('invalid_operation_id')
            if not isinstance(actor, str) or re.fullmatch('[a-zA-Z0-9_.-]{1,64}', actor) is None:
                raise ApplyError('invalid_actor')
            self.audit.append('OPERATION_STARTED', operation_id=identity, username=actor,
                              revision=expected_revision, outcome='config.apply')
            atomic_json(self.directory / 'previous.json', previous)
            atomic_json(self.directory / 'candidate.json', candidate)
            value = dict(schema_version=1, operation_id=identity, actor=actor, phase='PREPARED',
                         previous_revision=expected_active_revision, target_revision=expected_revision,
                         services=planned['services'], results={n: {'state': 'PENDING', 'reason': None} for n in planned['services']},
                         error=None, configuration_restored=False)
            atomic_json(self.path, value)
            self.current = value
            self.thread = threading.Thread(target=self._execute, args=(previous, candidate), daemon=True)
            self.thread.start()
            return dict(operation_id=identity, accepted=True, plan=planned, hardware_verified=False)

    def _stop(self, services):
        # API closes its old WHEP grants before media endpoints are changed.
        order = ('api', 'gateway', 'maintenance', 'storage', 'sonar', 'timecode') + tuple(reversed(ORDER[1:6])) + ('media',)
        for service in order:
            if service in services:
                self._result(service, 'STOPPING')
                self._action('stop', service)
                self._result(service, 'STOPPED')

    def _action(self, action, service):
        if self.stop.is_set(): raise InterruptedApply()
        self.backend.action(action, service)
        if self.stop.is_set(): raise InterruptedApply()

    def _start(self, services, best_effort=False):
        started = {}
        for service in ORDER:
            if service in services:
                self._result(service, 'STARTING')
                started[service] = monotonic_us()
                try: self._action('start', service)
                except (ApplyError, OSError):
                    if not best_effort: raise
                    self._result(service, 'ERROR', 'rollback_start_failed')
        return started

    def _verify(self, config, services, started):
        deadline, stable = time.monotonic() + self.timeout, {}
        while time.monotonic() < deadline:
            if self.stop.is_set(): raise InterruptedApply()
            ready = True
            for service in services:
                observed = self.backend.observe(service)
                ok, reason = acknowledgement(config, service, observed, started[service])
                if ok:
                    record = stable.get(service)
                    # Require a continuing process and fresh distinct heartbeat, not a stale success file.
                    if record is None or record[0] != observed.get('instance_id'):
                        stable[service] = (observed.get('instance_id'), time.monotonic(), observed.get('seq'))
                    record = stable[service]
                    ok = time.monotonic() - record[1] >= self.settle and observed.get('seq') != record[2]
                else: stable.pop(service, None)
                ready = ready and ok
                previous = self.current['results'][service]
                result = 'VERIFIED' if ok else 'VERIFYING'
                if previous['state'] != result or previous['reason'] != reason:
                    self._result(service, result, reason, observed)
            if ready:
                if revision(load(self.active)) != revision(config): raise ApplyError('active_configuration_drift')
                return
            time.sleep(.1)
        raise ApplyError('configuration_health_timeout')

    def _rollback(self, previous, error):
        services = self.current['services']
        self._save(phase='ROLLING_BACK', error=error)
        # Restore the confirmed file even when a stop fails; subsequent automatic
        # service restarts must load the previous settings, never the rejected ones.
        for service in reversed(services):
            try: self._action('stop', service)
            except (ApplyError, OSError): self._result(service, 'ERROR', 'rollback_stop_failed')
        atomic_json(self.active, previous, mode=0o644)
        self._save(configuration_restored=True)
        started = self._start(services, best_effort=True)
        self._verify(previous, services, started)
        self.audit.append('CONFIG_ROLLBACK', operation_id=self.current['operation_id'], username=self.current['actor'],
                          revision=self.current['previous_revision'], outcome='restored')
        self._save(phase='ROLLED_BACK')

    def _execute(self, previous, candidate):
        try:
            self._save(phase='STOPPING')
            self._stop(self.current['services'])
            self._save(phase='SWITCHING')
            atomic_json(self.active, candidate, mode=0o644)
            self._save(phase='STARTING')
            started = self._start(self.current['services'])
            self._save(phase='VERIFYING')
            self._verify(candidate, self.current['services'], started)
            self.audit.append('CONFIG_APPLY', operation_id=self.current['operation_id'], username=self.current['actor'],
                              revision=self.current['target_revision'], outcome='verified')
            self._save(phase='APPLIED')
        except InterruptedApply:
            return  # The durable journal is recovered on restart; never restart units during shutdown.
        except Exception as exc:
            reason = str(exc) if isinstance(exc, ApplyError) else 'configuration_io_failure'
            try: self._rollback(previous, reason)
            except InterruptedApply: return
            except Exception:
                try: self._save(phase='ROLLBACK_FAILED', error='configuration_recovery_required')
                except Exception: self.fatal = 'configuration_journal_unwritable'

    def recover(self, expected_operation_id=None):
        with self.guard:
            if self.thread is not None and self.thread.is_alive(): raise ApplyError('configuration_apply_busy')
            if self.current is None:
                load(self.active)
                return {'recovery_required': False}
            if expected_operation_id is not None and expected_operation_id != self.current['operation_id']:
                raise RevisionConflict('configuration_operation_changed')
            if self.current['phase'] in ('APPLIED', 'ROLLED_BACK', 'FAILED'):
                expected = self.current['target_revision'] if self.current['phase'] == 'APPLIED' else self.current['previous_revision']
                if revision(load(self.active)) != expected: raise ApplyError('active_configuration_drift')
                return {'recovery_required': False}
            previous = read_owned(self.directory / 'previous.json')
            from .config import validate
            validate(previous)
            if revision(previous) != self.current['previous_revision']: raise ApplyError('recovery_configuration_mismatch')
            candidate = read_owned(self.directory / 'candidate.json')
            if revision(candidate) != self.current['target_revision'] or self.current['services'] != plan(previous, candidate)['services']:
                raise ApplyError('recovery_service_plan_mismatch')
            def recovery():
                try:
                    self._rollback(previous, 'interrupted_application')
                    self.fatal = None
                except InterruptedApply:
                    return
                except Exception:
                    try: self._save(phase='ROLLBACK_FAILED', error='configuration_recovery_required')
                    except Exception: self.fatal = 'configuration_journal_unwritable'
            self.thread = threading.Thread(target=recovery, daemon=True)
            self.thread.start()
            return {'recovery_required': True, 'operation_id': self.current['operation_id']}
