#!/usr/bin/env python3
"""Six real native/media recorders with synthetic video and an explicit test volume."""
import argparse
import hashlib
import http.client
import importlib.util
import json
import os
import select
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter, deque
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from ss07.api import Server
from ss07.catalog import Catalog, CatalogError, RecordStore
from ss07.media import CHANNELS, MediaObserver, get_paths, render, rtp_port
from ss07.recorder import RecorderSession, verify_binary
from ss07.state import State
from ss07.volume import DirectoryVolume, open_directory
from ss07.worker import terminate

spec = importlib.util.spec_from_file_location('media_fixture', ROOT / 'tools/test-media.py')
fixture = importlib.util.module_from_spec(spec); spec.loader.exec_module(fixture)


class TimecodeSource:
    """Test-only 100 Hz PTY source; never creates a device-observation snapshot."""
    EPOCH = 2**63 + 37

    def __init__(self, root):
        sys.path.insert(0, str(ROOT / 'tests'))
        from test_timecode_runtime import RuntimeFixture
        from test_timecode import wire
        root.mkdir()
        self.sockets = tempfile.TemporaryDirectory(prefix='ss07-timecode-')
        self.runtime, self.wire = RuntimeFixture(root, socket_dir=self.sockets.name), wire
        self.stop, self.lock = threading.Event(), threading.Lock()
        self.paused, self.epoch, self.sequence = False, self.EPOCH, 0
        self.errors = []
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        try:
            while not self.stop.is_set():
                with self.lock:
                    if not self.paused:
                        source_us = 2**63 + 1000000 + self.sequence * 10000
                        os.write(self.runtime.master, self.wire(self.epoch, self.sequence, source_us))
                        self.sequence += 1
                self.stop.wait(.01)
        except Exception as exc:
            self.errors.append(repr(exc))

    def start(self):
        self.runtime.__enter__()
        self.thread.start()
        fixture.wait_for(lambda: int(self.runtime.status()['accepted_samples']) > 0)

    def pause(self):
        with self.lock: self.paused = True

    def new_epoch(self):
        with self.lock:
            self.epoch += 1
            self.sequence, self.paused = 0, False

    def close(self):
        self.stop.set(); self.thread.join(timeout=2)
        transmitted = bool(select.select([self.runtime.master], [], [], .02)[0])
        self.runtime.__exit__(None, None, None)
        self.sockets.cleanup()
        assert not self.errors, self.errors
        assert not transmitted, 'receiver transmitted or echoed bytes on the PTY'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--camera-test', type=Path, required=True)
    parser.add_argument('--mediamtx', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--chromium', type=Path)
    parser.add_argument('--timecode-pty', action='store_true', help='explicit synthetic read-only timecode integration')
    args = parser.parse_args()
    if args.output.exists():
        parser.error('output must be a new directory')
    args.output.mkdir(parents=True)
    verify_binary(args.mediamtx)
    root = args.output / 'recordings'; root.mkdir()
    volume = DirectoryVolume(open_directory(root), 'integration-fixture', 'synthetic_test')
    catalog = Catalog(args.output / 'catalog.sqlite'); catalog.initialize()
    evidence = dict(evidence_kind='synthetic_test', hardware_verified=False,
                    started_at_utc=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), result='failed')
    procs, logs = [], []
    config, upstream = fixture.configuration(), fixture.configuration()
    config['storage']['segment_seconds'] = 2
    config['sdi']['rtsp_url'] = 'rtsp://127.0.0.1:%d/front' % upstream['media']['rtsp_port']
    def start(name, argv):
        out = open(args.output / (name + '.log'), 'wb'); logs.append(out)
        proc = subprocess.Popen([str(arg) for arg in argv], stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
        procs.append(proc); return proc
    def source(name, port):
        return start(name, [args.camera_test, '--device', 'synthetic-test', '--width', '640', '--height', '360',
                           '--fps-num', '25', '--fps-den', '1', '--port', port])
    def counts():
        return Counter(item['channel_id'] for item in catalog.list(limit=200)['items'] if item['complete'])
    observer = MediaObserver()
    def six_ready():
        return all(stream['ready'] for stream in observer.observe(get_paths(config))['streams'].values())
    session = timecode = None
    hooks = deque(maxlen=512)
    original_event = RecorderSession._event
    def traced_event(recorder, raw):
        hooks.append(raw[:2049].decode(errors='replace'))
        return original_event(recorder, raw)
    hook_patch = patch.object(RecorderSession, '_event', traced_event)
    hook_patch.start()
    try:
        if args.timecode_pty:
            timecode = TimecodeSource(args.output / 'timecode-test')
            timecode.start()
        boot_id = timecode.runtime.boot if timecode else 'integration-boot'
        client = timecode.runtime.client if timecode else None
        with tempfile.TemporaryDirectory(prefix='ss07-record-integration-') as tmp:
            up = render(upstream); up['paths'] = {'front': up['paths']['front']}
            for name, data in [('upstream', up), ('main', render(config))]:
                path = Path(tmp) / (name + '.json'); path.write_text(json.dumps(data))
                start(name, [args.mediamtx, path])
            fixture.wait_for(lambda: get_paths(config))
            cameras = {ch: source('camera-' + ch, rtp_port(config, ch)) for ch in CHANNELS[:-1]}
            source('sdi-source', rtp_port(upstream, 'front'))
            fixture.wait_for(six_ready)
            with RecorderSession(config, catalog, volume, args.mediamtx, boot_id, timecode_client=client) as session:
                def recording_six():
                    session.tick()
                    observed = counts()
                    return observed if all(observed[ch] >= 2 for ch in CHANNELS) else None
                evidence['six_channel_segments'] = dict(fixture.wait_for(recording_six, seconds=25))
                if timecode:
                    previous = catalog.list(epoch=str(timecode.EPOCH), limit=200)['items']
                    assert previous and all(item['time_tags'] is not None for item in previous)
                    preserved = {item['recording_id']: item['time_tags'] for item in previous}
                    timecode.pause()
                    baseline = counts()
                    def lost_source_recording():
                        session.tick()
                        current = counts()
                        unavailable = {item['channel_id'] for item in catalog.list(limit=200)['items']
                            if item['complete'] and item['time_tags'] and
                            not item['time_tags']['end']['source_valid']}
                        return all(current[ch] > baseline[ch] and ch in unavailable for ch in CHANNELS)
                    fixture.wait_for(lost_source_recording, seconds=12)
                    timecode.new_epoch()
                    def new_epoch_recording():
                        session.tick()
                        current = catalog.list(epoch=str(timecode.EPOCH + 1), limit=200)['items']
                        return current if set(item['channel_id'] for item in current if item['complete']) == set(CHANNELS) else None
                    fixture.wait_for(new_epoch_recording, seconds=12)
                    for item in catalog.list(epoch=str(timecode.EPOCH), limit=200)['items']:
                        if item['recording_id'] in preserved:
                            assert item['time_tags'] == preserved[item['recording_id']], 'old time tag changed'
                        spans = item['timecode_association']
                        tags = item['time_tags']
                        if tags['start']['source_epoch'] != tags['end']['source_epoch']:
                            assert 'continuous_mapping_estimate' not in spans
                    evidence['timecode'] = dict(evidence_kind='synthetic_test', transport='PTY',
                        hardware_verified=False, epochs=[str(timecode.EPOCH), str(timecode.EPOCH + 1)],
                        six_channel_association='passed', source_loss_preserves_six_recorders='passed',
                        epoch_isolation='passed', persisted_tags_immutable='passed',
                        precision='USB receive estimate; no SOF or exposure calibration')
                terminate(cameras['left'])
                baseline = counts()
                def remaining_five():
                    session.tick()
                    current = counts()
                    active = observer.observe(get_paths(config))['streams']
                    return (not active['left']['ready'] and
                            all(current[ch] > baseline[ch] for ch in CHANNELS if ch != 'left'))
                fixture.wait_for(remaining_five, seconds=15)
                evidence['isolated_source_loss'] = 'passed'
                cameras['left'] = source('camera-left-recovered', rtp_port(config, 'left'))
                previous_left = counts()['left']
                def recovered_left():
                    session.tick()
                    return counts()['left'] > previous_left and six_ready()
                fixture.wait_for(recovered_left, seconds=15)
                evidence['source_recovery'] = 'passed'
            fixture.wait_for(six_ready)
            evidence['recorder_stop_preserves_monitoring'] = 'passed'
            evidence['recovery'] = RecordStore(catalog, volume).recover()
            assert evidence['recovery']['rejected'] == 0
            evidence['completed_counts'] = dict(counts())
            crash_config = json.loads(json.dumps(config))
            crash_config['storage']['segment_seconds'] = 30
            try:
                with RecorderSession(crash_config, catalog, volume, args.mediamtx, boot_id,
                                     timecode_client=client) as interrupted:
                    def open_partial_files():
                        interrupted.tick()
                        return len(interrupted.status()['channels_recording']) == 6
                    fixture.wait_for(open_partial_files, seconds=15)
                    interrupted.process.kill()
                    interrupted.process.wait(timeout=3)
                    interrupted.tick(timeout=0)
                    raise AssertionError('unexpectedly accepted dead recorder')
            except CatalogError as exc:
                assert str(exc) == 'record_process_exited', str(exc)
            after_crash = RecordStore(catalog, volume).recover()
            assert after_crash['incomplete'] == 6 and after_crash['rejected'] == 0, after_crash
            assert dict(counts()) == evidence['completed_counts']
            fixture.wait_for(six_ready)
            evidence['forced_recorder_exit'] = dict(result='passed', recovery=after_crash,
                monitoring='uninterrupted', incomplete_files='retained_and_locked')
            selected = next(item for item in catalog.list(limit=200)['items'] if item['complete'] and item['channel_id'] == 'front')
            fd, metadata = RecordStore(catalog, volume).open_recording(selected['recording_id'])
            with os.fdopen(fd, 'rb') as stream:
                raw = stream.read()
            def lease(*_args, **_kwargs):
                return DirectoryVolume(open_directory(root), 'integration-fixture', 'synthetic_test')
            # Only this isolated fixture replaces the hardware mount gate; there
            # is no configuration or production CLI option that can do so.
            with patch('ss07.recording_http.MountLease.acquire', side_effect=lease), \
                    Server(('127.0.0.1', config['api']['port']), State(config, catalog_path=catalog.path)) as server:
                thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
                try:
                    conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
                    route = '/api/v1/recordings/' + selected['recording_id'] + '/content'
                    conn.request('GET', route + '?download=1'); response = conn.getresponse()
                    download = response.read(); assert response.status == 200 and download == raw
                    conn.close()
                    evidence['export_sha256'] = hashlib.sha256(download).hexdigest()
                    conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
                    conn.request('GET', route, headers={'Range': 'bytes=11-333'})
                    response = conn.getresponse(); assert response.status == 206 and response.read() == raw[11:334]
                    conn.close(); evidence['http_range_export'] = 'passed'
                    if timecode:
                        for epoch in (timecode.EPOCH, timecode.EPOCH + 1):
                            conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
                            conn.request('GET', '/api/v1/recordings?epoch=%d&time_start_us=%d' % (epoch, 2**63))
                            response = conn.getresponse(); body = json.loads(response.read())
                            assert response.status == 200 and body['data']['items'], body
                            assert all(any(tag['source_epoch'] == str(epoch) for tag in item['time_tags'].values())
                                       for item in body['data']['items'])
                            conn.close()
                        conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
                        conn.request('GET', '/api/v1/recordings?time_start_us=1')
                        response = conn.getresponse(); response.read(); assert response.status == 422
                        conn.close(); evidence['timecode']['http_epoch_query'] = 'passed'
                    if args.chromium:
                        subprocess.run(['node', str(ROOT / 'tools/test-browser-recording.cjs'), str(args.chromium),
                                        'http://127.0.0.1:%d' % server.server_port, str(args.output)] +
                                       (['timecode'] if timecode else []), check=True, timeout=50)
                        evidence['browser'] = json.loads((args.output / 'browser-recording.json').read_text())
                finally:
                    server.shutdown(); thread.join(timeout=5)
            evidence['result'] = 'passed'
    finally:
        hook_patch.stop()
        (args.output / 'hook-events.json').write_text(json.dumps(list(hooks), indent=2) + '\n')
        if session:
            (args.output / 'recorder-tail.log').write_text(''.join(session.recent_logs))
        volume.close()
        for proc in reversed(procs): terminate(proc)
        for log in logs: log.close()
        if timecode:
            timecode.close()
            if 'timecode' in evidence: evidence['timecode']['pty_software_zero_tx'] = 'passed'
        (args.output / 'result.json').write_text(json.dumps(evidence, indent=2) + '\n')
    print(json.dumps(evidence, indent=2))


if __name__ == '__main__':
    main()
