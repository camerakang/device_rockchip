"""Explicit HTTPS/ICE provisioning, kept outside the mutable device configuration."""
import ipaddress
import os
import re
import subprocess
import tempfile
import time
from contextlib import ExitStack
from pathlib import Path
from urllib.parse import urlsplit

from .auth import public_origin
from .config import ConfigError, decode_json
from .worker import Worker, terminate

VERSION = 'v2.11.7'


def validate(value):
    if (type(value) is not dict or set(value) != {'schema_version', 'public_origin', 'listen_ip', 'ice_ip', 'certificate', 'private_key'}
            or type(value['schema_version']) is not int or value['schema_version'] != 1):
        raise ConfigError('invalid gateway configuration')
    origin = public_origin(value['public_origin'])
    if origin != value['public_origin'] or not 1024 <= (urlsplit(origin).port or 443) <= 65535:
        raise ConfigError('gateway requires a canonical HTTPS origin with an unprivileged explicit port')
    for key in ('listen_ip', 'ice_ip'):
        address = ipaddress.IPv4Address(value[key])
        if address.is_unspecified or address.is_multicast or address.is_reserved or str(address) != value[key]:
            raise ConfigError('gateway requires explicit IPv4 interface addresses')
    for key in ('certificate', 'private_key'):
        path = value[key]
        if (not isinstance(path, str) or len(path) > 512 or re.fullmatch(r'/[a-zA-Z0-9_./-]+', path) is None
                or '..' in Path(path).parts or str(Path(path)) != path):
            raise ConfigError('invalid TLS file path')
    if value['certificate'] == value['private_key']: raise ConfigError('TLS certificate and key must be separate')
    return value


def load(path):
    with open(path, 'rb') as source:
        return validate(decode_json(source.read(4097), max_bytes=4096))


def render(config, gateway):
    validate(gateway)
    from .media import settings
    port = urlsplit(gateway['public_origin']).port
    media = settings(config)
    if port in [config['api']['port']] + [v for k, v in media.items() if k != 'rtp_base_port'] + list(range(media['rtp_base_port'], media['rtp_base_port'] + 10)):
        raise ConfigError('gateway and application ports overlap')
    # All substitutions are strictly validated literals, never request values.
    return '''{
    admin off
    persist_config off
    auto_https off
    log {
        level ERROR
    }
    servers {
        protocols h1 h2
        max_header_size 16KB
        timeouts {
            read_header 3s
            read_body 5s
            idle 30s
        }
    }
}
%s {
    bind %s
    tls %s %s {
        protocols tls1.2 tls1.3
    }
    request_body {
        max_size 65536
    }
    reverse_proxy 127.0.0.1:%d {
        header_up X-SS07-Peer {remote_host}
        flush_interval -1
        transport http {
            dial_timeout 1s
            response_header_timeout 4s
        }
    }
}
''' % (gateway['public_origin'], gateway['listen_ip'], gateway['certificate'], gateway['private_key'], config['api']['port'])


def owns_listener(pid, port):
    """Check the native child's own socket inodes, not an unrelated listener."""
    try:
        directory = Path('/proc') / str(pid)
        inodes = set()
        for fd in (directory / 'fd').iterdir():
            try: target = os.readlink(str(fd))
            except FileNotFoundError: continue  # Short-lived HTTP connections close during a sample.
            if target.startswith('socket:[') and target.endswith(']'): inodes.add(target[8:-1])
        for name in ('tcp', 'tcp6'):
            with (directory / 'net' / name).open('r') as source: lines = source.read(65537)
            if len(lines) > 65536: return False
            for line in lines.splitlines()[1:]:
                fields = line.split()
                if len(fields) > 9 and fields[3] == '0A' and fields[9] in inodes and int(fields[1].rsplit(':', 1)[1], 16) == port:
                    return True
    except (OSError, ValueError): return False
    return False


def run(config, gateway, binary, stop, state_dir=None):
    check = subprocess.run([str(binary), 'version'], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True, timeout=5)
    if check.stdout.decode().split(' ', 1)[0].strip() != VERSION: raise ConfigError('Caddy version must be ' + VERSION)
    with ExitStack() as stack:
        worker = stack.enter_context(Worker(state_dir, 'gateway', stop, config=config)) if state_dir else None
        tmp = stack.enter_context(tempfile.TemporaryDirectory(prefix='ss07-gateway-'))
        path = Path(tmp) / 'Caddyfile'
        path.write_text(render(config, gateway))
        os.chmod(str(path), 0o600)
        proc = subprocess.Popen([str(binary), 'run', '--config', str(path), '--adapter', 'caddyfile'], start_new_session=True)
        try:
            next_publish = 0
            while not stop.wait(.2):
                if proc.poll() is not None: raise ConfigError('HTTPS gateway exited')
                if worker and time.monotonic() >= next_publish:
                    ready = owns_listener(proc.pid, urlsplit(gateway['public_origin']).port)
                    worker.emit('RUNNING' if ready else 'STARTING', {'reason': None if ready else 'https_not_listening'})
                    next_publish = time.monotonic() + 1
        finally: terminate(proc)
