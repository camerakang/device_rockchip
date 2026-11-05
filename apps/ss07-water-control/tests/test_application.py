"""Fault-injected coordinator tests. Fake lifecycle backend is not device evidence."""
import copy
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from ss07.application import Application, ApplyError, SystemdBackend, journal, recover_before_start
from ss07.application_worker import dispatch
from ss07.maintenance import Operations
from ss07.config import ROOT, load, revision
from ss07.config_runtime import ORDER, acknowledgement, fingerprint, plan
from ss07.persistence import ConfigStore, RevisionConflict, atomic_json
from ss07.state import State
from ss07.worker import monotonic_us


class FakeLifecycle:
    def __init__(self, path):
        self.path, self.calls, self.running = path, [], {}
        self.fail_candidate = False
        self.fail_all = False
        self.stale_candidate = False
        self.block = False
        self.cancel = None

    def action(self, action, service):
        self.calls.append((action, service))
        if action == 'stop': self.running.pop(service, None)
        else:
            self.running[service] = [load(self.path), str(uuid.uuid4()), 0]
            if self.cancel is not None: self.cancel.set()

    def observe(self, service):
        if service not in self.running: return {'state': 'UNAVAILABLE'}
        config, identity, seq = self.running[service]; self.running[service][2] += 1
        broken = self.fail_all or self.block or (self.fail_candidate and config['device_id'] == 'candidate')
        state, payload = 'RUNNING', {'reason': None}
        if service.startswith('camera.'): state, payload = 'UNAVAILABLE', {'reason': 'camera_unconfigured'}
        if service == 'timecode': state, payload = 'UNAVAILABLE', {'reason': 'timecode_not_configured'}
        if service == 'storage': state, payload = 'UNAVAILABLE', {'gate': 'DISABLED'}
        if broken: state = 'ERROR'
        observed = 0 if self.stale_candidate and config['device_id'] == 'candidate' else monotonic_us()
        return dict(state=state, payload=payload, service_revision=fingerprint(config, service),
                    config_revision=revision(config), instance_id=identity, seq=str(seq), observed_monotonic_us=str(observed))


class ApplicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.desired, self.active = self.root / 'desired.json', self.root / 'active.json'
        self.old = load(ROOT / 'config/system.example.json')
        atomic_json(self.desired, self.old); atomic_json(self.active, self.old)
        self.backend = FakeLifecycle(self.active)
        self.app = Application(self.desired, self.active, self.root / 'journal', self.backend, timeout=.35, settle=0)
        self.new = copy.deepcopy(self.old); self.new['device_id'] = 'candidate'

    def submit(self, candidate=None):
        candidate = candidate or self.new
        ConfigStore(self.desired).update(candidate, revision(load(self.desired)))
        return self.app.submit(revision(candidate), revision(load(self.active)), 'operator')

    def finish(self):
        self.app.thread.join(timeout=5)
        self.assertFalse(self.app.thread.is_alive())
        return self.app.status()['transaction']

    def test_selective_apply_uses_observed_revision_and_keeps_recorder_running(self):
        value = self.submit(); final = self.finish()
        self.assertEqual(value['plan']['services'], ['api'])
        self.assertEqual(self.backend.calls, [('stop', 'api'), ('start', 'api')])
        self.assertEqual(final['phase'], 'APPLIED')
        self.assertEqual(load(self.active), self.new)
        self.assertFalse(value['hardware_verified'])
        self.assertEqual(final['results']['api']['state'], 'VERIFIED')
        self.assertEqual(final['results']['api']['service_revision'], fingerprint(self.new, 'api'))

    def test_native_rtsp_port_conflict_is_rejected_before_stopping_any_worker(self):
        self.app.rtsp_profile = dict(schema_version=1, enabled=True, port=8322)
        self.app.gateway = dict(schema_version=1, public_origin='https://127.0.0.1:8443', listen_ip='127.0.0.1',
                                ice_ip='127.0.0.1', certificate='/etc/ss07-tls/cert.pem', private_key='/etc/ss07-tls/key.pem')
        self.new['api']['port'] = 8322
        with self.assertRaisesRegex(ValueError, 'native RTSP and application ports overlap'): self.submit()
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(load(self.active), self.old)

    def test_health_failure_restores_runtime_without_overwriting_desired_edit(self):
        self.backend.fail_candidate = True
        self.submit(); final = self.finish()
        self.assertEqual(final['phase'], 'ROLLED_BACK')
        self.assertTrue(final['configuration_restored'])
        self.assertEqual(load(self.active), self.old)
        self.assertEqual(load(self.desired), self.new)

    def test_stale_ack_cannot_make_apply_succeed(self):
        self.backend.stale_candidate = True
        self.submit(); final = self.finish()
        self.assertEqual(final['phase'], 'ROLLED_BACK')
        self.assertEqual(final['error'], 'configuration_health_timeout')

    def test_failed_recovery_is_explicit_and_can_be_retried(self):
        self.backend.fail_all = True
        self.submit(); final = self.finish()
        self.assertEqual(final['phase'], 'ROLLBACK_FAILED')
        self.assertEqual(load(self.active), self.old)
        with self.assertRaisesRegex(ApplyError, 'recovery_required'):
            self.app.submit(revision(self.new), revision(self.old), 'operator')
        self.backend.fail_all = False
        self.app.recover(final['operation_id'])
        self.assertEqual(self.finish()['phase'], 'ROLLED_BACK')

    def test_conflict_and_unimplemented_protocol_cause_no_lifecycle_actions(self):
        with self.assertRaises(RevisionConflict):
            self.app.submit('0' * 64, revision(self.old), 'operator')
        unsupported = copy.deepcopy(self.old); unsupported['sonar']['nodes']['front']['address'] = '1'
        with self.assertRaisesRegex(ValueError, 'sonar_driver_unavailable'): self.submit(unsupported)
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(load(self.active), self.old)

    def test_only_one_job_and_later_desired_edits_remain_pending(self):
        self.backend.block = True
        self.submit()
        with self.assertRaisesRegex(ApplyError, 'busy'):
            self.app.submit(revision(self.new), revision(self.old), 'operator')
        later = copy.deepcopy(self.new); later['device_id'] = 'later-save'
        ConfigStore(self.desired).update(later, revision(self.new))
        self.backend.block = False
        self.assertEqual(self.finish()['phase'], 'APPLIED')
        self.assertEqual(load(self.active), self.new)
        state = State(self.new, config_path=self.desired, active_path=self.active).configuration()
        self.assertTrue(state['pending_apply'])
        self.assertEqual(state['active_revision'], revision(self.new))
        self.assertEqual(state['revision'], revision(later))

    def test_interruption_restores_before_consumers_start_then_verifies_recovery(self):
        self.backend.cancel = self.app.stop
        self.submit(); interrupted = self.finish()
        self.assertEqual(interrupted['phase'], 'STARTING')
        self.assertEqual(load(self.active), self.new)
        count = len(self.backend.calls)
        value = recover_before_start(self.active, self.root / 'journal')
        self.assertTrue(value['recovery_required'])
        self.assertEqual(load(self.active), self.old)
        self.assertEqual(len(self.backend.calls), count)
        self.backend.cancel = None; self.backend.running.clear()
        self.app = Application(self.desired, self.active, self.root / 'journal', self.backend, timeout=.35, settle=0)
        self.app.recover()
        self.assertEqual(self.finish()['phase'], 'ROLLED_BACK')

    def test_atomic_switch_failure_retains_previous_config_and_records_recovery(self):
        original = atomic_json
        failed = []
        def write(path, value, *args, **kwargs):
            if Path(path) == self.active and value == self.new and not failed:
                failed.append(True); raise OSError('injected ENOSPC')
            return original(path, value, *args, **kwargs)
        with patch('ss07.application.atomic_json', side_effect=write):
            self.submit(); final = self.finish()
        self.assertEqual(final['phase'], 'ROLLED_BACK')
        self.assertEqual(load(self.active), self.old)

    def test_recovery_rejects_corrupted_backup_without_service_actions(self):
        self.backend.cancel = self.app.stop
        self.submit(); self.finish()
        atomic_json(self.root / 'journal/previous.json', self.new)
        before = len(self.backend.calls)
        with self.assertRaisesRegex(ApplyError, 'mismatch'): recover_before_start(self.active, self.root / 'journal')
        self.assertEqual(len(self.backend.calls), before)

    def test_corrupt_transaction_fields_are_rejected_before_recovery(self):
        self.submit(); original = self.finish()
        variants = [dict(original, schema_version=True), dict(original, phase=[]), dict(original, operation_id='bad'),
                    dict(original, error={'not': 'an error code'}), dict(original, services=['api', 'api']),
                    dict(original, results={'api': {'state': [], 'reason': None}}),
                    dict(original, results={'api': {'state': 'VERIFIED', 'reason': None, 'arbitrary': 'field'}})]
        before = list(self.backend.calls)
        for value in variants:
            with self.subTest(value=value):
                atomic_json(self.root / 'journal/transaction.json', value)
                with self.assertRaisesRegex(ApplyError, 'invalid_application_journal'):
                    recover_before_start(self.active, self.root / 'journal')
        self.assertEqual(self.backend.calls, before)

    def test_active_file_drift_is_refused_instead_of_overwriting_it(self):
        self.submit(); self.finish()
        atomic_json(self.active, self.old)
        before = list(self.backend.calls)
        with self.assertRaisesRegex(ApplyError, 'active_configuration_drift'): self.app.recover()
        with self.assertRaisesRegex(ApplyError, 'active_configuration_drift'):
            self.app.submit(revision(self.new), revision(self.old), 'operator')
        self.assertEqual(self.backend.calls, before)

    def test_completed_operations_have_bounded_audit_records(self):
        self.submit(); self.finish()
        items = self.app.audit.history()['items']
        self.assertEqual([item['kind'] for item in items], ['CONFIG_APPLY', 'OPERATION_STARTED'])
        self.assertEqual(items[0]['revision'], revision(self.new))
        self.assertEqual(items[0]['operation_id'], journal(self.root / 'journal')['operation_id'])
        self.assertNotIn('candidate', items[0])

    def test_broker_preserves_request_identity_for_coordinator_and_audit(self):
        from unittest.mock import Mock
        client = Mock()
        identity = str(uuid.uuid4())
        actor = {'username': 'operator', 'role': 'maintainer'}
        payload = {'expected_revision': revision(self.old), 'expected_active_revision': revision(self.old)}
        client.request.return_value = {'request_id': identity, 'data': {'accepted': True, 'operation_id': identity}}
        broker = Operations(self.desired, None, self.root / 'maintenance.sqlite', self.active, client)
        result = broker.execute(dict(schema_version=1, request_id=identity, actor=actor, operation='config.apply', payload=payload))
        client.request.assert_called_once_with(actor, 'config.apply', payload, request_id=identity)
        self.assertEqual(result['data']['operation_id'], identity)
        self.assertEqual(broker.audit.history()['items'][0]['operation_id'], identity)

    def test_dependencies_do_not_restart_media_or_recording_for_api_listener(self):
        candidate = copy.deepcopy(self.old); candidate['api']['port'] = 18078
        self.assertEqual(plan(self.old, candidate)['services'], ['api', 'gateway'])
        candidate = copy.deepcopy(self.old); candidate['storage']['segment_seconds'] = 60
        self.assertEqual(plan(self.old, candidate)['services'], ['storage', 'maintenance', 'api'])
        candidate = copy.deepcopy(self.old); candidate['media'] = {'rtp_base_port': 19500}
        self.assertEqual(plan(self.old, candidate)['services'], ['media'] + ['camera.' + n for n in ('front','back','left','right','down')] + ['api'])

    def test_rpc_refuses_roles_unknown_operations_and_arbitrary_units(self):
        request = dict(schema_version=1, request_id=str(uuid.uuid4()), actor={'username': 'viewer', 'role': 'monitor'},
                       operation='config.apply', payload={'expected_revision': revision(self.old), 'expected_active_revision': revision(self.old)})
        self.assertEqual(dispatch(self.app, request)['error']['status'], 403)
        request['actor']['role'] = 'maintainer'; request['operation'] = 'systemctl.stop'
        self.assertEqual(dispatch(self.app, request)['error']['status'], 403)
        request['operation'] = 'config.apply'; request['payload']['unit'] = 'ssh.service'
        self.assertEqual(dispatch(self.app, request)['error']['status'], 422)
        with patch('ss07.application.subprocess.run') as command:
            with self.assertRaises(ApplyError): SystemdBackend(self.root).action('stop', 'ssh.service')
            command.assert_not_called()


if __name__ == '__main__': unittest.main()
