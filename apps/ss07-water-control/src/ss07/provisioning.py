"""Read-only checks of explicit local provisioning. No generated production secrets."""
import grp
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import ssl
import stat
import subprocess
import tempfile
from urllib.parse import urlsplit

from .accounts import validate_accounts
from .config import decode_json, revision, validate
from .gateway import render, validate as validate_gateway
from .release_restore import read_regular, safe_path


class ProvisionError(ValueError):
    pass


def validate_tls(certificate, key, hostname):
    """Verify key pairing, server purpose, SAN and dates, not client/browser trust."""
    with tempfile.TemporaryDirectory(prefix='ss07-tls-check-') as temporary:
        certpath, keypath = Path(temporary) / 'certificate.pem', Path(temporary) / 'private.key'
        for path, raw in ((certpath, certificate), (keypath, key)):
            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as stream: stream.write(raw)
        try:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(str(certpath), str(keypath), password=lambda: b'')
            env = dict(PATH='/usr/sbin:/usr/bin:/sbin:/bin', LC_ALL='C', OPENSSL_CONF='/dev/null')
            san = subprocess.check_output(['/usr/bin/openssl', 'x509', '-in', str(certpath), '-noout', '-ext', 'subjectAltName'],
                                          stderr=subprocess.DEVNULL, timeout=5, text=True, env=env)
            try: ipaddress.ip_address(hostname); kind = '-verify_ip'; present = 'IP Address:' in san
            except ValueError: kind = '-verify_hostname'; present = 'DNS:' in san
            if not present: raise ProvisionError('tls_subject_alt_name_required')
            subprocess.run(['/usr/bin/openssl', 'verify', '-no-CAfile', '-no-CApath', '-trusted', str(certpath),
                            '-partial_chain', '-purpose', 'sslserver', kind, hostname, str(certpath)],
                           check=True, timeout=5, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
        except (ssl.SSLError, subprocess.SubprocessError) as exc:
            raise ProvisionError('tls_certificate_key_name_or_validity_invalid') from exc


def check(root=Path('/'), groups=None, addresses=None):
    """Tests inject IDs/addresses; production uses installed users and local interfaces."""
    root = Path(root)
    groups = groups if groups is not None else {name: grp.getgrnam(name).gr_gid for name in ('ss07-web', 'ss07-gateway')}
    desired = validate(decode_json(read_regular(safe_path(root, 'etc/ss07/system.json'))))
    active_path = safe_path(root, 'etc/ss07-active/system.json')
    active = validate(decode_json(read_regular(active_path)))
    if revision(desired) != revision(active): raise ProvisionError('pending_configuration_before_activation')
    if active_path.stat().st_uid != os.geteuid() or active_path.stat().st_mode & 0o022:
        raise ProvisionError('active_configuration_permissions')
    accounts_path = safe_path(root, 'etc/ss07-credentials/accounts.json')
    accounts = validate_accounts(decode_json(read_regular(accounts_path)))
    gateway = validate_gateway(decode_json(read_regular(safe_path(root, 'etc/ss07-gateway/network.json'))))
    render(active, gateway)  # Includes port conflict validation.
    from .rtsp_access import load as load_rtsp, check_ports
    rtsp = load_rtsp(safe_path(root, 'etc/ss07-gateway/rtsp.json'))
    check_ports(active, gateway, rtsp)
    certificate = safe_path(root, gateway['certificate'].lstrip('/'))
    key = safe_path(root, gateway['private_key'].lstrip('/'))
    for path in (safe_path(root, 'etc/ss07-gateway/network.json'), certificate):
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise ProvisionError('gateway_or_certificate_permissions')
    for path, group in ((accounts_path, groups['ss07-web']), (key, groups['ss07-gateway'])):
        info = path.lstat()
        if info.st_uid != os.geteuid() or info.st_gid != group or stat.S_IMODE(info.st_mode) != 0o640:
            raise ProvisionError('credential_owner_group_or_mode_invalid')
        # The service group needs traversal through each parent. No writable
        # public parent can replace a credential after this check.
        parent = path.parent
        while parent != root:
            info = parent.lstat()
            if (not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o022
                    or not (info.st_mode & 0o001 or info.st_gid == group and info.st_mode & 0o010)):
                raise ProvisionError('credential_directory_permissions')
            parent = parent.parent
    cert_raw, key_raw = read_regular(certificate, 131072), read_regular(key, 32768)
    validate_tls(cert_raw, key_raw, urlsplit(gateway['public_origin']).hostname)
    if addresses is None:
        result = subprocess.check_output(['/sbin/ip', '-json', '-4', 'address', 'show'], timeout=5)
        addresses = {entry['local'] for interface in json.loads(result) for entry in interface.get('addr_info', []) if entry.get('family') == 'inet'}
    if gateway['listen_ip'] not in addresses or gateway['ice_ip'] not in addresses:
        raise ProvisionError('gateway_address_not_on_local_interface')
    return dict(ready=True, config_revision=revision(active), public_origin=gateway['public_origin'],
                account_count=len(accounts['users']), certificate_sha256=hashlib.sha256(cert_raw).hexdigest(),
                recording_enabled=active['storage'].get('recording_enabled', False),
                native_rtsp_enabled=rtsp['enabled'],
                client_certificate_trust_verified=False, hardware_verified=False)
