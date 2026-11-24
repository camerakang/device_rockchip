"""Automatic LAN discovery for the SDI encoder (AVMatrix SE2017).

Runs inside the root-owned ss07-network-sync watcher. When the configured
encoder IP is unreachable (e.g. after DHCP or switching to a new subnet),
probes ARP neighbors and the local /24 subnet on the encoder's RTSP port,
verifies SE2017 identity using the root-managed credentials (and optional
MAC address), and publishes the active host to /run/ss07-network/sdi-encoder.json.
"""
import base64
from concurrent.futures import ThreadPoolExecutor
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import subprocess

from .config import decode_json
from .release_restore import read_regular, safe_path, write_file
from .sdi import SDI_LIVE, load_encoder, resolve_sdi_url

ACTIVE_CONFIG = 'etc/ss07-active/system.json'
FALLBACK_CONFIG = 'etc/ss07/system.json'
CREDENTIALS = 'etc/ss07-credentials/sdi-encoder.json'


def _read_neigh():
    try:
        raw = subprocess.check_output(['/sbin/ip', '-j', '-4', 'neigh', 'show'],
                                      stderr=subprocess.DEVNULL, timeout=5)
        rows = json.loads(raw.decode('utf-8', 'replace'))
    except (OSError, ValueError, subprocess.SubprocessError):
        return {}
    result = {}
    for row in rows if isinstance(rows, list) else ():
        dst, mac = row.get('dst'), row.get('lladdr')
        state = row.get('state') or []
        if (isinstance(dst, str) and isinstance(mac, str)
                and 'FAILED' not in state and 'INCOMPLETE' not in state):
            result[dst] = mac.lower()
    return result


def _tcp_open(host, port, timeout=0.35):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def probe_se2017(host, credentials, rtsp_port=5554, http_port=80, timeout=1.5):
    """Return True only when host responds as SE2017 and accepts credentials."""
    if rtsp_port and not _tcp_open(host, rtsp_port, timeout=min(timeout, 0.8)):
        return False
    body = json.dumps({'sUserName': credentials['username'],
                       'sPassword': base64.b64encode(credentials['password'].encode()).decode()}).encode()
    conn = http.client.HTTPConnection(host, http_port, timeout=timeout)
    try:
        conn.request('PUT', '/cgi-bin/entry.cgi/system/login', body=body,
                     headers={'Content-Type': 'application/json', 'Authorization': 'Bearer undefined'})
        response = conn.getresponse()
        raw = response.read(16385)
        if len(raw) > 16384 or response.status != 200:
            return False
        token = None
        for name, value in response.getheaders():
            if name.lower() == 'set-cookie':
                match = re.match(r'\s*token=([^;\s]+)', value)
                if match: token = match[1]
        data = json.loads(raw.decode('utf-8', 'replace') or 'null')
        return isinstance(data, dict) and data.get('status', -1) >= 0 and bool(token)
    except (OSError, ValueError, http.client.HTTPException):
        return False
    finally:
        conn.close()


def local_scan_targets(interfaces, interface_name=None):
    """Return (own_ips, scan_hosts) bounded to at most one /24 per active interface."""
    own, subnets = set(), []
    for iface in interfaces:
        name = iface.get('ifname')
        if name == 'lo' or 'UP' not in iface.get('flags', []):
            continue
        if interface_name and name != interface_name:
            continue
        for addr in iface.get('addr_info', []):
            if (addr.get('family') != 'inet' or addr.get('scope') != 'global'
                    or addr.get('deprecated') or addr.get('tentative')
                    or addr.get('preferred_life_time') == 0):
                continue
            try:
                ip = ipaddress.IPv4Address(addr['local'])
            except (KeyError, ValueError):
                continue
            if int(ip) >> 24 == 0 or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
                continue
            own.add(str(ip))
            prefixlen = addr.get('prefixlen')
            eff = prefixlen if type(prefixlen) is int and 24 <= prefixlen <= 30 else 24
            net = ipaddress.IPv4Interface('%s/%d' % (ip, eff)).network
            if net not in subnets:
                subnets.append(net)
    hosts = []
    for net in subnets:
        for ip in net.hosts():
            text = str(ip)
            if text not in own:
                hosts.append(text)
    return own, hosts


def discover(current_host, credentials, interfaces, *, rtsp_port=5554, interface_name=None,
             probe=probe_se2017, tcp_open=_tcp_open, read_neigh=_read_neigh):
    """Find the SE2017 encoder IP on the local subnet. Returns (host, mac) or (None, None)."""
    expected_mac = credentials.get('mac')

    def matches_mac(host, neigh):
        if not expected_mac:
            return True
        mac = neigh.get(host)
        if mac is None:
            neigh.update(read_neigh())
            mac = neigh.get(host)
        return mac == expected_mac

    neigh = read_neigh()
    # 1. Fast path: current host is already reachable and valid.
    if current_host and probe(current_host, credentials, rtsp_port=rtsp_port):
        if matches_mac(current_host, neigh):
            return current_host, neigh.get(current_host)

    # 2. ARP neighbor fast path (exact MAC match first, then any known neighbor).
    own, scan_hosts = local_scan_targets(interfaces, interface_name=interface_name)
    scan_set = set(scan_hosts)
    if expected_mac:
        for ip, mac in neigh.items():
            if mac == expected_mac and ip not in own and probe(ip, credentials, rtsp_port=rtsp_port):
                return ip, mac

    # 3. Bounded concurrent TCP probe on local /24 subnet(s).
    candidates = []
    if scan_hosts:
        with ThreadPoolExecutor(max_workers=min(48, len(scan_hosts))) as pool:
            results = pool.map(lambda ip: (ip, tcp_open(ip, rtsp_port)), scan_hosts)
            candidates = [ip for ip, ok in results if ok]

    verified = []
    for ip in candidates:
        if probe(ip, credentials, rtsp_port=rtsp_port):
            if matches_mac(ip, neigh):
                verified.append((ip, neigh.get(ip)))
    if len(verified) == 1:
        return verified[0]
    if len(verified) > 1:
        print('sdi discovery: multiple encoders matched (%s); set mac in sdi-encoder.json'
              % ', '.join(ip for ip, _ in verified), flush=True)
    return None, None


def _configured_sdi_url(root):
    for rel in (ACTIVE_CONFIG, FALLBACK_CONFIG):
        path = safe_path(root, rel)
        if not path.is_file():
            continue
        try:
            data = decode_json(read_regular(path, 262144), max_bytes=262144)
            url = data.get('sdi', {}).get('rtsp_url')
            if isinstance(url, str) and url:
                return url
        except (OSError, ValueError):
            continue
    return None


def sync_sdi(root, interfaces, interface_name=None, uid=0, *,
             probe=probe_se2017, tcp_open=_tcp_open, read_neigh=_read_neigh):
    """Reconcile /run/ss07-network/sdi-encoder.json. Return (host, changed)."""
    from urllib.parse import urlsplit
    configured = _configured_sdi_url(root)
    if not configured:
        return None, False
    cred_path = safe_path(root, CREDENTIALS)
    try:
        credentials = load_encoder(cred_path)
    except (OSError, ValueError):
        return None, False
    if credentials is None:
        return None, False
    live = safe_path(root, SDI_LIVE.lstrip('/'))
    live.parent.mkdir(mode=0o755, exist_ok=True)
    effective_url = resolve_sdi_url(configured, live_path=live)
    parsed = urlsplit(effective_url)
    current_host = parsed.hostname
    rtsp_port = parsed.port or 5554
    found_host, found_mac = discover(
        current_host, credentials, interfaces, rtsp_port=rtsp_port, interface_name=interface_name,
        probe=probe, tcp_open=tcp_open, read_neigh=read_neigh,
    )
    if found_host is None:
        return None, False
    record = {'host': found_host}
    if found_mac:
        record['mac'] = found_mac
    payload = (json.dumps(record, sort_keys=True) + '\n').encode()
    changed = True
    try:
        if live.exists() and read_regular(live, 4096) == payload:
            changed = False
    except (OSError, ValueError):
        changed = True
    if changed:
        write_file(live, payload, 0o644, uid, 0 if uid == 0 else os.getegid())
    return found_host, changed
