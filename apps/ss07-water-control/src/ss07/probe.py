"""Linux read-only inventory. Never opens a serial port or starts a media pipeline."""
import os
import platform
import selectors
import shutil
import signal
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path


def read(path):
    try:
        return Path(path).read_text().strip()
    except (OSError, UnicodeError):
        return None


def command(argv, timeout=4, limit=65536, env=None):
    if not shutil.which(argv[0]):
        return {"state": "missing_tool", "command": argv, "output": "", "returncode": None}
    data = bytearray()
    state = "ok"
    env = dict(os.environ if env is None else env, LC_ALL='C')
    try:
        with subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              env=env, start_new_session=True) as proc:
            with selectors.DefaultSelector() as selector:
                selector.register(proc.stdout, selectors.EVENT_READ)
                deadline = time.monotonic() + timeout
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        state = "timeout"
                        break
                    if not selector.select(remaining):
                        state = "timeout"
                        break
                    chunk = os.read(proc.stdout.fileno(), min(4096, limit + 1 - len(data)))
                    if not chunk:
                        break
                    data.extend(chunk)
                    if len(data) > limit:
                        state = "output_limit"
                        break
                if state != "ok":
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                try:
                    proc.wait(timeout=max(0.1, deadline-time.monotonic()))
                except subprocess.TimeoutExpired:
                    state = "timeout"
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    proc.wait()
                if state == "ok" and proc.returncode != 0:
                    state = "error"
                return {"state": state, "command": argv, "returncode": proc.returncode,
                        "output": data[:limit].decode(errors="replace")}
    except OSError as exc:
        return {"state": "error", "command": argv, "returncode": None, "output": str(exc)}


def links(pattern, node):
    return sorted(str(p) for p in Path('/dev').glob(pattern) if p.resolve() == node.resolve())


def device(node):
    query = command(['udevadm', 'info', '--query=property', '--name', str(node)])
    wanted = {'ID_SERIAL', 'ID_SERIAL_SHORT', 'ID_PATH', 'ID_VENDOR_ID', 'ID_MODEL_ID',
              'ID_V4L_CAPABILITIES', 'ID_USB_INTERFACE_NUM'}
    properties = dict(line.split('=', 1) for line in query['output'].splitlines()
                      if '=' in line and line.split('=', 1)[0] in wanted)
    return {'node': str(node), 'by_id': links('*/by-id/*', node),
            'by_path': links('*/by-path/*', node), 'properties': properties,
            'udev_state': query['state'],
            'sysfs_path': str((Path('/sys/class') / ('video4linux' if node.name.startswith('video') else 'tty') / node.name / 'device').resolve())}


def collect():
    video = []
    for node in sorted(Path('/dev').glob('video[0-9]*'))[:64]:
        entry = device(node)
        entry['capabilities'] = command(['v4l2-ctl', '--device', str(node), '--all'])
        entry['formats'] = command(['v4l2-ctl', '--device', str(node), '--list-formats-ext'])
        video.append(entry)
    serial = [device(node) for node in sorted(list(Path('/dev').glob('ttyUSB*')) +
                                             list(Path('/dev').glob('ttyACM*')))[:64]]
    # Private temporary registry avoids altering the user's GStreamer plugin cache.
    with tempfile.TemporaryDirectory(prefix='ss07-gst-') as tmp:
        env = dict(os.environ, GST_REGISTRY=str(Path(tmp)/'registry.bin'))
        media = {name: command(['gst-inspect-1.0', name], env=env) for name in
                 ['--version', 'v4l2src', 'h264parse', 'rtspclientsink', 'rtspsrc',
                  'mpph264enc', 'mppvideodec']}
    return {'schema_version': 1, 'evidence_kind': 'local_observation',
            'observed_at_utc': datetime.now(timezone.utc).isoformat(),
            'host': {'machine': platform.machine(), 'kernel': platform.release(),
                     'boot_id': read('/proc/sys/kernel/random/boot_id'),
                     'python': platform.python_version(), 'os_release': read('/etc/os-release'),
                     'device_tree_model': read('/proc/device-tree/model')},
            'video_devices': video, 'serial_devices': serial,
            'usb_topology': command(['lsusb', '-t']), 'gstreamer': media,
            'ffmpeg': command(['ffmpeg', '-version']),
            'packages': command(['dpkg-query', '-W', '-f=${Package}\t${Version}\t${Architecture}\n',
                                 'python3', 'libgstreamer1.0-0', 'librockchip-mpp1', 'librga2']),
            'mounts': command(['findmnt', '--json', '--output', 'TARGET,SOURCE,FSTYPE,OPTIONS']),
            'network': command(['ip', '-json', 'address', 'show']),
            'limits': ['No serial ports opened; no TX, baud changes or probing of vendor packets.',
                       'No RTSP connection or capture pipeline; plugin presence is not hardware validation.',
                       'Inventory is a point-in-time observation, not live health.',
                       'Device lists capped at 64 each; command output capped at 64 KiB, timeout 4 s.']}
