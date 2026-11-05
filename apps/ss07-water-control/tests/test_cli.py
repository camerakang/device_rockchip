import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from urllib.request import urlopen

from ss07.config import ROOT, load
from ss07.state import State
from ss07.timecode_journal import Journal
from ss07.worker import Worker


class CLITests(unittest.TestCase):
    def command(self, *args):
        return subprocess.run([sys.executable, '-m', 'ss07', *args],
                              capture_output=True, text=True, timeout=10)

    def test_config_lifecycle_and_exit_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'system.json'
            result = self.command('init-config', '--output', str(path))
            self.assertEqual(result.returncode, 0, result.stderr)
            revision = json.loads(result.stdout)['revision']
            candidate = load(path)
            candidate['device_id'] = 'new-device'
            replacement = Path(tmp) / 'candidate.json'
            replacement.write_text(json.dumps(candidate))
            args = ('update-config', '--config', str(path), '--candidate', str(replacement),
                    '--expected-revision', revision)
            self.assertEqual(self.command(*args).returncode, 0)
            self.assertEqual(self.command(*args).returncode, 3)
            self.assertEqual(self.command('init-config', '--output', str(path)).returncode, 3)
            self.assertTrue(path.with_name('system.json.previous').exists())

    def test_staged_app_runs_and_sigterm_stops_cleanly(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / 'rootfs'
            subprocess.run([sys.executable, str(ROOT / 'packaging/stage-rootfs.py'), '--destdir', str(dest)],
                           check=True, capture_output=True, timeout=10)
            self.assertFalse((dest / 'data').exists())
            self.assertFalse((dest / 'etc/systemd/system/multi-user.target.wants').exists())
            config = load(dest / 'etc/ss07/system.json')
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                config['api']['port'] = sock.getsockname()[1]
            path = Path(tmp) / 'runtime.json'
            path.write_text(json.dumps(config))
            env = dict(os.environ, SS07_PYTHON=sys.executable)
            proc = subprocess.Popen([str(dest / 'opt/ss07/current/tools/ss07'), 'serve', '--config', str(path)],
                                    cwd=tmp, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                import selectors
                with selectors.DefaultSelector() as selector:
                    selector.register(proc.stdout, selectors.EVENT_READ)
                    self.assertTrue(selector.select(timeout=5), 'startup timeout')
                self.assertIn(b'listening', proc.stdout.readline())
                with urlopen('http://127.0.0.1:{}/healthz'.format(config['api']['port']), timeout=2) as response:
                    self.assertFalse(json.load(response)['data']['hardware_ready'])
                proc.send_signal(signal.SIGTERM)
                _, stderr = proc.communicate(timeout=5)
                self.assertEqual(proc.returncode, 0, stderr.decode())
            finally:
                if proc.poll() is None: proc.kill()
                proc.communicate(timeout=5)

    def test_timecode_worker_unconfigured_lifecycle_and_exclusive_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory, journal = Path(tmp) / 'state', Path(tmp) / 'timecode.sqlite'
            config = load(ROOT / 'config/board-rk3588.example.json')
            path = Path(tmp) / 'config.json'; path.write_text(json.dumps(config))
            args = ('timecode-worker', '--config', str(path), '--state-dir', str(directory),
                    '--timecode-journal', str(journal))
            proc = subprocess.Popen([sys.executable, '-m', 'ss07', *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            state = State(config, state_dir=directory, timecode_journal_path=journal)
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    status = state.snapshot()['timecode']
                    if status['reason'] == 'timecode_not_configured' and status['journal']['error'] is None: break
                    time.sleep(.02)
                else: self.fail('timecode worker unavailable after startup')
                self.assertEqual(status['quality'], 'UNAVAILABLE')
                self.assertFalse(status['serial_connected'])
                self.assertFalse(status['source_valid'])
                self.assertEqual(status['software_tx_bytes'], '0')
                self.assertEqual(self.command(*args).returncode, 2)
                self.assertTrue(Journal(journal).history()['items'])
                proc.send_signal(signal.SIGTERM)
                _, stderr = proc.communicate(timeout=5)
                self.assertEqual(proc.returncode, 0, stderr.decode())
                self.assertFalse((directory / 'timecode.sock').exists())
                self.assertEqual(json.loads((directory / 'timecode.json').read_text())['state'], 'STOPPED')
            finally:
                if proc.poll() is None: proc.kill()
                proc.communicate(timeout=5)

    def test_catalog_migration_excludes_active_recorder(self):
        with tempfile.TemporaryDirectory() as tmp:
            catalog, backup = Path(tmp) / 'catalog.sqlite', Path(tmp) / 'before.sqlite'
            with Worker(Path(tmp) / 'state', 'storage'):
                result = self.command('migrate-catalog', '--catalog', str(catalog), '--backup', str(backup),
                                      '--state-dir', str(Path(tmp) / 'state'))
            self.assertEqual(result.returncode, 2)
            self.assertFalse(catalog.exists())
            self.assertFalse(backup.exists())


if __name__ == '__main__': unittest.main()
