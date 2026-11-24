"""Root-side DHCP reconciliation. Consumers only read the published snapshot."""
import grp
import ipaddress
import json
from pathlib import Path
import re
import stat
import subprocess
import time
import tempfile
import secrets

from .config import ConfigError, decode_json
from .release_restore import read_regular, safe_path, write_file

CONFIG = 'etc/ss07-gateway/network.json'
LIVE = 'run/ss07-network/effective.json'
CERT = '/etc/ss07-tls/server.crt'
KEY = '/etc/ss07-tls/server.key'
UNITS = ['ss07-media.service', 'ss07-api.service', 'ss07-gateway.service', 'ss07-application.service']


def policy(raw):
    if (type(raw) is not dict or type(raw.get('schema_version')) is not int or raw['schema_version'] != 2
            or set(raw) - {'schema_version', 'interface', 'port'}):
        raise ConfigError('invalid adaptive gateway policy')
    name, port = raw.get('interface', 'eth0'), raw.get('port', 8443)
    if (not isinstance(name, str) or not re.fullmatch('[a-zA-Z0-9][a-zA-Z0-9_.-]{0,14}', name)
            or name == 'lo' or type(port) is not int or not 1024 <= port <= 65535):
        raise ConfigError('invalid gateway interface or port')
    return name, port


def select_ip(interfaces, name):
    for iface in interfaces:
        if iface.get('ifname') != name or 'UP' not in iface.get('flags', []): continue
        for addr in iface.get('addr_info', []):
            if (addr.get('family') != 'inet' or addr.get('scope') != 'global'
                    or addr.get('deprecated') or addr.get('tentative') or addr.get('preferred_life_time') == 0):
                continue
            ip = ipaddress.IPv4Address(addr['local'])
            if int(ip) >> 24 != 0 and not (ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified):
                return str(ip)
    return None


def resolved(ip, port):
    from .gateway import validate
    return validate(dict(schema_version=1, public_origin='https://%s:%s' % (ip, port),
                         listen_ip=ip, ice_ip=ip, certificate=CERT, private_key=KEY))


def owned(path, uid, *, directory=False, group=None, mode=None):
    s = path.lstat()
    if (not (stat.S_ISDIR(s.st_mode) if directory else stat.S_ISREG(s.st_mode) and s.st_nlink == 1)
            or s.st_uid != uid or s.st_mode & 0o022 or (group is not None and s.st_gid != group)
            or (mode is not None and stat.S_IMODE(s.st_mode) != mode)):
        raise ConfigError('unsafe network provisioning: ' + str(path))


def issuer(directory, uid):
    """Stable per-board issuer, never copied into a release or exposed by HTTP."""
    key, cert = directory / 'network-ca.key', directory / 'network-ca.crt'
    if not key.exists():
        if cert.exists(): raise ConfigError('network CA key missing; restore backup')
        material = subprocess.check_output(['/usr/bin/openssl','genpkey','-algorithm','RSA',
            '-pkeyopt','rsa_keygen_bits:3072'], stderr=subprocess.DEVNULL, timeout=30)
        write_file(key, material, 0o600)
    owned(key, uid, mode=0o600)
    if not cert.exists():
        material = subprocess.check_output(['/usr/bin/openssl','req','-new','-x509','-sha256',
            '-days','3650','-key',str(key),'-subj','/CN=SS-07 network CA',
            '-addext','basicConstraints=critical,CA:TRUE,pathlen:0',
            '-addext','keyUsage=critical,keyCertSign,cRLSign'], timeout=10)
        write_file(cert, material, 0o644)
    owned(cert, uid)
    return key, cert


def issue(key, ip, ca_key, ca_cert):
    with tempfile.TemporaryDirectory(prefix='ss07-leaf-') as tmp:
        extensions = Path(tmp) / 'extensions'
        extensions.write_text('basicConstraints=critical,CA:FALSE\nextendedKeyUsage=serverAuth\n'
                              'subjectAltName=IP:%s,IP:127.0.0.1\n' % ip)
        csr = subprocess.check_output(['/usr/bin/openssl','req','-new','-key',str(key),
                                       '-subj','/CN=SS-07'], timeout=10)
        return subprocess.check_output(['/usr/bin/openssl','x509','-req','-sha256','-days','365',
            '-CA',str(ca_cert),'-CAkey',str(ca_key),'-set_serial',str(secrets.randbits(128) or 1),
            '-extfile',str(extensions)], input=csr, timeout=10, stderr=subprocess.DEVNULL)


def sync(root, interfaces, gid, uid=0):
    """Return effective address, or None while DHCP is pending. Never replace the private key."""
    from .provisioning import validate_tls, ProvisionError
    cfg = safe_path(root, CONFIG)
    live = safe_path(root, LIVE)
    for parent in (cfg.parent.parent, cfg.parent, live.parent.parent):
        owned(parent, uid, directory=True)
    owned(cfg, uid)
    raw = decode_json(read_regular(cfg, 4096))
    if not isinstance(raw, dict): raise ConfigError('invalid gateway policy')
    if raw.get('schema_version') == 1:
        from .gateway import validate
        validate(raw)
        if live.exists():
            owned(live.parent, uid, directory=True)
            live.unlink()
        return 'static'
    name, port = policy(raw)
    live.parent.mkdir(mode=0o755, exist_ok=True)
    owned(live.parent, uid, directory=True)
    ip = select_ip(interfaces, name)
    if ip is None:
        if live.exists(): live.unlink()
        return None
    effective = resolved(ip, port)
    cert, key = (safe_path(root, p[1:]) for p in (CERT, KEY))
    owned(cert.parent, uid, directory=True)
    owned(key, uid, group=gid, mode=0o640)
    owned(cert, uid)
    keyraw = read_regular(key, 32768)
    ca_key, ca_cert = issuer(cert.parent, uid)
    def verify(blob):
        for addr in (ip, '127.0.0.1'):
            validate_tls(blob, keyraw, addr)
        subprocess.run(['/usr/bin/openssl','verify','-CAfile',str(ca_cert),'-purpose','sslserver'],
                       input=blob, check=True, timeout=5, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        verify(read_regular(cert, 131072))
    except (ProvisionError, subprocess.CalledProcessError):
        blob = issue(key, ip, ca_key, ca_cert)
        verify(blob)
        write_file(cert, blob, 0o644, uid, cert.stat().st_gid)
    snapshot = json.dumps({'interface': name, 'gateway': effective}, sort_keys=True).encode()
    if not live.exists() or read_regular(live, 4096) != snapshot:
        write_file(live, snapshot, 0o644)
    return ip


def watch():
    root = Path('/')
    previous = object()
    next_check = 0
    while True:
        try:
            interfaces = json.loads(subprocess.check_output(['/sbin/ip','-j','-4','addr','show'], timeout=5))
            # Only run expensive TLS checks when address/policy or TLS files change.
            def signature():
                raw = decode_json(read_regular(root / CONFIG, 4096))
                if not isinstance(raw, dict): raise ConfigError('invalid gateway policy')
                target = select_ip(interfaces, policy(raw)[0]) if raw.get('schema_version') != 1 else 'static'
                def version(path):
                    try: s = path.lstat()
                    except FileNotFoundError: return None
                    return s.st_ino, s.st_ctime_ns
                return target, tuple(version(root / n) for n in (CONFIG,CERT[1:],KEY[1:],
                        'etc/ss07-tls/network-ca.key','etc/ss07-tls/network-ca.crt',LIVE))
            current = signature()
            if current != previous or time.monotonic() >= next_check:
                address = sync(root, interfaces, grp.getgrnam('ss07-gateway').gr_gid)
                after = signature()
                if (current != previous or after != current) and (address != 'static'
                        or isinstance(previous, tuple) and previous[0] != 'static'):
                    # Include units left failed by binding an obsolete static IP.
                    # Never start consumers on a staged, not yet activated board.
                    active = subprocess.run(['/bin/systemctl','is-active','--quiet','ss07.target']).returncode == 0
                    if Path('/var/lib/ss07-deploy/in-progress').exists():
                        time.sleep(3)
                        continue
                    if active and address is not None:
                        subprocess.run(['/bin/systemctl','reset-failed',*UNITS], timeout=10, check=True)
                    action = 'restart' if active and address is not None else 'try-restart'
                    subprocess.run(['/bin/systemctl',action,*UNITS], timeout=90, check=True)
                previous = after
                next_check = time.monotonic() + 3600
                print('gateway address:', address or 'awaiting DHCP', flush=True)
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            print('network sync:', str(exc), flush=True)
        time.sleep(3)
