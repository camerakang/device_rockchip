"""Dependency-free CLI; persistent changes are explicit local commands."""
import argparse
import json
import pwd
import signal
import sqlite3
import sys
import threading

from . import __version__
from .config import ConfigError, ROOT, decode_json, load, revision
from .persistence import ConfigStore, RevisionConflict
from .catalog import RecordConflict


def evidence(path):
    # Probe output contains command listings, so it has a separate 8 MiB bound.
    with open(path, 'rb') as stream:
        raw = stream.read(8 * 1024 * 1024 + 1)
    if len(raw) > 8 * 1024 * 1024:
        raise ConfigError('probe evidence exceeds 8 MiB')
    try:
        report = decode_json(raw, max_bytes=8 * 1024 * 1024)
        if type(report) is not dict or type(report.get('schema_version')) is not int or report.get('schema_version') != 1:
            raise ValueError('probe schema')
        if report.get('evidence_kind') != 'local_observation':
            raise ValueError('real probe evidence required')
        for key in ('video_devices', 'serial_devices'):
            entries = report.get(key)
            if not isinstance(entries, list) or len(entries) > 128:
                raise ValueError('device list')
            for entry in entries:
                if not isinstance(entry, dict) or not isinstance(entry.get('node'), str):
                    raise ValueError('device entry')
                if not isinstance(entry.get('properties'), dict) or not all(isinstance(v, str) for v in entry['properties'].values()):
                    raise ValueError('device properties')
                for links in ('by_id', 'by_path'):
                    if not isinstance(entry.get(links), list) or not all(isinstance(v, str) for v in entry[links]):
                        raise ValueError('device links')
                if key == 'video_devices':
                    formats = entry.get('formats')
                    if not isinstance(formats, dict) or not isinstance(formats.get('output'), str):
                        raise ValueError('device formats')
        return report
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ConfigError('invalid probe evidence') from exc


def main():
    parser = argparse.ArgumentParser(description='SS-07 foundation tools; no motion control')
    parser.add_argument('--version', action='version', version=__version__)
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('validate', 'serve', 'check-cameras', 'media-config', 'media-worker', 'camera-worker',
                 'recording-worker', 'recover-recordings', 'lock-recording', 'timecode-worker', 'sonar-worker',
                 'recover-sonar-archives', 'lock-sonar-archive',
                 'maintenance-worker', 'close-webrtc', 'gateway-config', 'gateway-worker'):
        cmd = commands.add_parser(name)
        cmd.add_argument('--config', default=str(ROOT / 'config/system.example.json'))
        if name in ('serve', 'media-worker', 'camera-worker', 'recording-worker', 'recover-recordings', 'timecode-worker', 'sonar-worker', 'recover-sonar-archives', 'lock-sonar-archive', 'maintenance-worker', 'gateway-worker'):
            cmd.add_argument('--state-dir', help='trusted local worker snapshot directory')
        if name in ('serve', 'timecode-worker'):
            cmd.add_argument('--timecode-journal', help='bounded timecode mapping audit SQLite path')
        if name in ('serve', 'sonar-worker'):
            cmd.add_argument('--sonar-journal', help='bounded diagnostic sonar history; not a data-disk archive')
        if name in ('serve','sonar-worker','recover-sonar-archives','lock-sonar-archive'):
            cmd.add_argument('--sonar-catalog',help='trusted local index of full data-volume sonar archives')
        if name=='lock-sonar-archive':
            cmd.add_argument('--archive-id',required=True)
            cmd.add_argument('--locked',choices=('true','false'),required=True)
            cmd.add_argument('--expected-revision',required=True)
        if name in ('media-worker', 'camera-worker', 'recording-worker', 'gateway-worker'):
            cmd.add_argument('--binary', required=True, help='native executable path')
        if name in ('serve', 'media-worker', 'media-config', 'gateway-worker', 'gateway-config'):
            cmd.add_argument('--gateway-config', required=name.startswith('gateway-'), help='root-managed HTTPS/ICE configuration')
        if name == 'media-worker':
            cmd.add_argument('--rtsp-config', help='optional root-managed RTSPS profile; absent means disabled')
            cmd.add_argument('--accounts', help='shared root-managed accounts; required for external RTSPS')
            cmd.add_argument('--rtsp-audit', help='native RTSP access audit SQLite')
            cmd.add_argument('--api-clients', help='optional root-managed machine API client keys (e.g. mobocon)')
            cmd.add_argument('--lan-monitor', help='optional key-less LAN monitor allow-list (lan-monitor.json)')
            cmd.add_argument('--sdi-encoder', help='optional root-managed SDI encoder status credentials (sdi-encoder.json)')
        if name == 'camera-worker':
            cmd.add_argument('--channel', required=True, choices=('front', 'back', 'left', 'right', 'down'))
        if name == 'check-cameras':
            cmd.add_argument('--probe', required=True, help='saved real probe JSON')
        if name in ('serve', 'recording-worker', 'recover-recordings', 'lock-recording', 'maintenance-worker'):
            cmd.add_argument('--catalog', help='trusted local recording catalog path')
        if name == 'serve':
            cmd.add_argument('--api-clients', help='optional root-managed machine API client keys; enables Bearer API access')
            cmd.add_argument('--lan-monitor', help='optional key-less LAN monitor allow-list (lan-monitor.json)')
            cmd.add_argument('--direct-access', action='store_true', help='open the full web workspace without login; retain browser sessions and CSRF')
            cmd.add_argument('--desired-config', help='separate persisted candidate configuration; --config is the active snapshot')
            cmd.add_argument('--accounts', help='root-managed accounts JSON; no default credentials')
            cmd.add_argument('--public-origin', help='exact external HTTPS origin')
            cmd.add_argument('--access-audit', help='access audit SQLite path')
            cmd.add_argument('--maintenance-audit', help='read-only maintenance audit SQLite path')
            cmd.add_argument('--application-audit', help='read-only configuration application audit SQLite path')
            cmd.add_argument('--rtsp-audit', help='read-only native RTSP access audit SQLite path')
            cmd.add_argument('--maintenance-user', help='expected Unix maintenance service user')
            cmd.add_argument('--media-cleanup-state', help='private runtime file for the stop hook to use the loaded media port')
        if name == 'close-webrtc':
            cmd.add_argument('--cleanup-state', help='use the API runtime port even when desired configuration changed')
        if name == 'maintenance-worker':
            cmd.add_argument('--active-config', help='runtime storage configuration; never use unapplied desired values for locking')
            cmd.add_argument('--application-socket-dir', help='root coordinator Unix socket directory')
            cmd.add_argument('--application-user', help='expected coordinator UID')
            cmd.add_argument('--audit', required=True)
            cmd.add_argument('--api-user', required=True, help='only this Unix UID may submit operations')
        if name == 'lock-recording':
            cmd.add_argument('--recording-id', required=True)
            cmd.add_argument('--locked', choices=('true', 'false'), required=True)
            cmd.add_argument('--expected-revision', required=True)
    cmd = commands.add_parser('api-client', help='manage machine API clients such as mobocon; prints a new key once')
    cmd.add_argument('action', choices=('add', 'rotate', 'enable', 'disable', 'remove', 'list'))
    cmd.add_argument('--api-clients', required=True)
    cmd.add_argument('--name', help='client name, e.g. mobocon')
    cmd.add_argument('--role', choices=('monitor', 'reader', 'maintainer'), default='maintainer')
    cmd.add_argument('--group', help='file group for a new key file, e.g. ss07-web')
    cmd = commands.add_parser('node-info', help='print the Mobocom node UID, firmware and control interface')
    cmd.add_argument('--json', action='store_true')
    cmd = commands.add_parser('node-service', help='install/remove the independent ss07-mobocom-node unit (root)')
    cmd.add_argument('action', choices=('install', 'uninstall', 'status'))
    for name in ('init-accounts', 'list-accounts', 'set-account'):
        cmd = commands.add_parser(name)
        cmd.add_argument('--accounts', required=True)
        if name != 'list-accounts':
            cmd.add_argument('--username', required=True)
            cmd.add_argument('--password-stdin', action='store_true', help='read one password line; never pass secrets as arguments')
        if name == 'set-account':
            cmd.add_argument('--role', required=True, choices=('monitor', 'reader', 'maintainer'))
            cmd.add_argument('--enabled', required=True, choices=('true', 'false'))
            cmd.add_argument('--expected-revision', required=True)
            cmd.add_argument('--new-password', action='store_true', help='prompt for a replacement password')
    cmd = commands.add_parser('init-catalog')
    cmd.add_argument('--catalog', required=True)
    cmd = commands.add_parser('migrate-catalog')
    cmd.add_argument('--catalog', required=True)
    cmd.add_argument('--backup', required=True, help='new schema-1 backup path; never overwritten')
    cmd.add_argument('--state-dir', required=True, help='exclude the active recording worker')
    commands.add_parser('probe')
    cmd = commands.add_parser('inventory')
    cmd.add_argument('--probe', required=True)
    cmd = commands.add_parser('init-config')
    cmd.add_argument('--output', required=True, help='new configuration file in an existing directory')
    cmd = commands.add_parser('update-config')
    cmd.add_argument('--config', required=True)
    cmd.add_argument('--candidate', required=True)
    cmd.add_argument('--expected-revision', required=True)
    cmd = commands.add_parser('init-active-config')
    cmd.add_argument('--config', required=True)
    cmd.add_argument('--output', required=True)
    for name in ('application-worker', 'recover-active-config'):
        cmd = commands.add_parser(name)
        cmd.add_argument('--active-config', required=True)
        cmd.add_argument('--journal-dir', required=True)
        cmd.add_argument('--state-dir', required=True)
        if name == 'application-worker':
            cmd.add_argument('--desired-config', required=True)
            cmd.add_argument('--socket-dir', required=True)
            cmd.add_argument('--gateway-config', required=True)
            cmd.add_argument('--rtsp-config', help='optional native RTSP profile for port validation')
            cmd.add_argument('--maintenance-user', required=True)
    args = parser.parse_args()
    try:
        if args.command == 'init-active-config':
            from pathlib import Path
            value = ConfigStore(args.output).initialize(load(args.config))
            Path(args.output).chmod(0o644)
            result = {'active_revision': value, 'services_started': False}
        elif args.command == 'recover-active-config':
            import fcntl
            import os
            import stat
            from contextlib import ExitStack
            from pathlib import Path
            from .config_runtime import ORDER
            from .application import recover_before_start
            # Boot ordering and these locks exclude running consumers. Never
            # restore a runtime file underneath live workers via this command.
            with ExitStack() as stack:
                for name in ORDER + ('configuration',):
                    path = Path(args.state_dir) / (name + '.lock')
                    try: fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                    except FileNotFoundError: continue
                    stack.callback(os.close, fd)
                    if not stat.S_ISREG(os.fstat(fd).st_mode): raise ConfigError('invalid consumer lock')
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                result = recover_before_start(args.active_config, args.journal_dir)
        elif args.command == 'application-worker':
            from .application_worker import run
            from .gateway import load as load_gateway
            from .rtsp_access import load as load_rtsp
            from .worker import stop_event
            run(args.desired_config, args.active_config, args.journal_dir, args.state_dir, args.socket_dir,
                load_gateway(args.gateway_config), pwd.getpwnam(args.maintenance_user).pw_uid, stop_event(),
                rtsp_profile=load_rtsp(args.rtsp_config))
            return 0
        elif args.command == 'api-client':
            from .api_clients import ApiClients
            clients = ApiClients(args.api_clients)
            if args.action != 'list' and not args.name: raise ConfigError('--name is required')
            key = None
            if args.action == 'add':
                group = None
                if args.group:
                    import grp
                    group = grp.getgrnam(args.group).gr_gid
                key = clients.add(args.name, args.role, group)
            elif args.action == 'rotate': key = clients.rotate(args.name)
            elif args.action in ('enable', 'disable'): clients.set(args.name, enabled=args.action == 'enable')
            elif args.action == 'remove': clients.remove(args.name)
            result = clients.public()
            if key: result = dict(result, name=args.name, key=key, note='shown once; store it in the client configuration')
        elif args.command == 'node-info':
            from .node_service import info
            return info(args.json)
        elif args.command == 'node-service':
            from . import node_service
            result = getattr(node_service, args.action)()
        elif args.command in ('init-accounts', 'list-accounts', 'set-account'):
            from .accounts import Accounts, password_bytes
            accounts = Accounts(args.accounts)
            if args.command != 'list-accounts':
                password = None
                if args.command == 'init-accounts' or args.password_stdin or args.new_password:
                    if args.password_stdin:
                        line = sys.stdin.readline(130)
                        password = line[:-1] if line.endswith('\n') else line
                    else:
                        from getpass import getpass
                        if not sys.stdin.isatty(): raise ConfigError('use --password-stdin for non-interactive provisioning')
                        password = getpass('Password: ')
                        if password != getpass('Repeat password: '): raise ConfigError('passwords do not match')
                    password_bytes(password)
                if args.command == 'init-accounts': accounts.initialize(args.username, password)
                else: accounts.update(args.username, args.role, args.enabled == 'true', args.expected_revision, password)
            result = accounts.public()
        elif args.command == 'probe':
            from .probe import collect
            result = collect()
        elif args.command == 'inventory':
            from .devices import inventory
            result = inventory(evidence(args.probe))
        elif args.command == 'init-config':
            value = ConfigStore(args.output).initialize(load(ROOT / 'config/system.example.json'))
            result = {'revision': value, 'hardware_applied': False}
        elif args.command == 'update-config':
            value = ConfigStore(args.config).update(load(args.candidate), args.expected_revision)
            result = {'revision': value, 'hardware_applied': False, 'restart_required': True}
        elif args.command == 'init-catalog':
            from .catalog import Catalog, DATABASE_SCHEMA
            Catalog(args.catalog).initialize()
            result = {'catalog_schema': DATABASE_SCHEMA, 'recording_enabled': False}
        elif args.command == 'migrate-catalog':
            from .catalog import Catalog
            from .worker import Worker
            with Worker(args.state_dir, 'storage'):
                result = Catalog(args.catalog).migrate_v1(args.backup)
        elif args.command == 'close-webrtc':
            from .media_sessions import close_all, load_cleanup_config
            result = close_all(load_cleanup_config(args.cleanup_state) if args.cleanup_state else load(args.config))
        else:
            config = load(args.config)
            gateway = None
            if getattr(args, 'gateway_config', None):
                from .gateway import load as load_gateway
                gateway = load_gateway(args.gateway_config)
            if args.command == 'validate':
                result = {'valid': True, 'revision': revision(config), 'hardware_verified': False}
            elif args.command == 'check-cameras':
                from .devices import check_cameras
                result = check_cameras(config, evidence(args.probe))
            elif args.command == 'media-config':
                from .media import render
                result = render(config, gateway)
            elif args.command == 'gateway-config':
                from .gateway import render
                print(render(config, gateway), end='')
                return 0
            elif args.command == 'gateway-worker':
                from .gateway import run
                from .worker import stop_event
                from pathlib import Path
                run(config, gateway, Path(args.binary).resolve(strict=True), stop_event(), args.state_dir)
                return 0
            elif args.command == 'maintenance-worker':
                if not args.state_dir: raise ConfigError('--state-dir is required')
                from .maintenance import run
                from .maintenance import Client
                from .worker import stop_event
                application = None
                if args.application_socket_dir or args.application_user:
                    if not all((args.application_socket_dir, args.application_user, args.active_config)):
                        raise ConfigError('application coordinator requires socket, peer user and active config')
                    application = Client(args.application_socket_dir, pwd.getpwnam(args.application_user).pw_uid, 'application.sock')
                run(args.config, args.state_dir, args.catalog, args.audit, pwd.getpwnam(args.api_user).pw_uid,
                    stop_event(), args.active_config, application)
                return 0
            elif args.command in ('recover-recordings', 'lock-recording'):
                from .catalog import Catalog, RecordStore
                from .volume import MountLease
                if not args.catalog:
                    raise ConfigError('--catalog is required')
                catalog = Catalog(args.catalog)
                catalog.initialize()
                with MountLease.acquire(config['storage'], writable=True) as volume:
                    store = RecordStore(catalog, volume)
                    if args.command == 'recover-recordings':
                        if not args.state_dir:
                            raise ConfigError('--state-dir is required to exclude an active recorder')
                        from .worker import Worker
                        with Worker(args.state_dir, 'storage'):
                            result = store.recover()
                    else:
                        result = store.set_lock(args.recording_id, args.locked == 'true', args.expected_revision)
            elif args.command in ('recover-sonar-archives','lock-sonar-archive'):
                if not args.state_dir or not args.sonar_catalog:raise ConfigError('--state-dir and --sonar-catalog are required')
                from .sonar_archive import Catalog as SonarCatalog,Store as SonarStore
                from .volume import MountLease
                from .worker import Worker
                with Worker(args.state_dir,'sonar'):
                    catalog=SonarCatalog(args.sonar_catalog)
                    with catalog.exclusive():
                        catalog.initialize()
                        with MountLease.acquire(config['storage'],writable=True) as volume:
                            store=SonarStore(catalog,volume)
                            result=store.recover() if args.command=='recover-sonar-archives' else store.set_lock(
                                args.archive_id,args.locked=='true',args.expected_revision)
            elif args.command == 'sonar-worker':
                if not args.state_dir or not args.sonar_journal:
                    raise ConfigError('--state-dir and --sonar-journal are required')
                from .sonar_worker import run
                from .worker import stop_event
                if config['storage'].get('sonar_recording_enabled',False) and not args.sonar_catalog:
                    raise ConfigError('--sonar-catalog is required when sonar recording is enabled')
                run(config, args.state_dir, args.sonar_journal, stop_event(),args.sonar_catalog)
                return 0
            elif args.command == 'timecode-worker':
                if not args.state_dir or not args.timecode_journal:
                    raise ConfigError('--state-dir and --timecode-journal are required')
                from .timecode_worker import run
                from .worker import stop_event
                run(config, args.state_dir, args.timecode_journal, stop_event())
                return 0
            elif args.command in ('media-worker', 'camera-worker', 'recording-worker'):
                if not args.state_dir:
                    raise ConfigError('--state-dir is required for workers')
                from .worker import stop_event
                from pathlib import Path
                binary = Path(args.binary).resolve(strict=True)
                if args.command == 'media-worker':
                    from .media import run
                    from .rtsp_access import load as load_rtsp
                    run(config, args.state_dir, binary, stop_event(), gateway, load_rtsp(args.rtsp_config),
                        args.accounts, args.rtsp_audit, args.api_clients, args.lan_monitor, args.sdi_encoder)
                elif args.command == 'camera-worker':
                    from .camera import run
                    run(config, args.channel, args.state_dir, binary, stop_event())
                else:
                    if not args.catalog:
                        raise ConfigError('--catalog is required for recording')
                    from .recorder import run
                    run(config, args.state_dir, args.catalog, binary, stop_event())
                return 0
            else:
                from .api import Server
                from .state import State
                state = State(config, args.state_dir, args.desired_config or args.config, args.catalog, args.timecode_journal,
                              active_path=args.config if args.desired_config else None, sonar_journal_path=args.sonar_journal,
                              sonar_catalog_path=args.sonar_catalog)
                auth = maintenance = maintenance_audit = application_audit = rtsp_audit = None
                if gateway:
                    if args.public_origin and args.public_origin != gateway['public_origin']:
                        raise ConfigError('conflicting public origins')
                    args.public_origin = gateway['public_origin']
                if any((args.direct_access, args.accounts, args.public_origin, args.access_audit, args.maintenance_audit, args.application_audit, args.rtsp_audit, args.maintenance_user, args.media_cleanup_state)):
                    if not all((args.accounts or args.direct_access, args.public_origin, args.access_audit)):
                        raise ConfigError('web access requires --public-origin, --access-audit, and either --accounts or --direct-access')
                    from .audit import Audit
                    from .auth import Auth
                    audit = Audit(args.access_audit); audit.initialize()
                    auth = Auth(args.accounts, audit, args.public_origin, direct_access=args.direct_access)
                    if args.maintenance_user:
                        if not args.state_dir or not args.maintenance_audit:
                            raise ConfigError('maintenance requires --state-dir and --maintenance-audit')
                        from .maintenance import Client
                        maintenance = Client(args.state_dir, pwd.getpwnam(args.maintenance_user).pw_uid)
                    maintenance_audit = Audit(args.maintenance_audit) if args.maintenance_audit else None
                    application_audit = Audit(args.application_audit) if args.application_audit else None
                    rtsp_audit = Audit(args.rtsp_audit) if args.rtsp_audit else None
                api_clients = None
                if args.api_clients:
                    if not auth: raise ConfigError('--api-clients requires HTTPS web access settings')
                    from .api_clients import ApiClients
                    api_clients = ApiClients(args.api_clients); api_clients.read()  # Fail closed on a bad file.
                lan_monitor = None
                if args.lan_monitor:
                    if not auth: raise ConfigError('--lan-monitor requires HTTPS web access settings')
                    from .lan_monitor import LanMonitor, log as lan_log
                    lan_monitor = LanMonitor(args.lan_monitor); lan_log(lan_monitor.describe())
                with Server((config['api']['host'], config['api']['port']), state, auth, maintenance, maintenance_audit, args.media_cleanup_state, application_audit, rtsp_audit, api_clients, lan_monitor) as server:
                    def stop(signum, frame):
                        if not server.stopping.is_set():
                            server.stopping.set()
                            threading.Thread(target=server.shutdown, daemon=True).start()
                    signal.signal(signal.SIGTERM, stop)
                    print('SS-07 {} listening on http://{}:{}'.format(
                        __version__, config['api']['host'], config['api']['port']), flush=True)
                    from .worker import heartbeat
                    with heartbeat(args.state_dir, 'api', config, server.stopping, server.shutdown):
                        server.serve_forever(poll_interval=0.2)
                return 0
        print(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False))
        return 0
    except (RevisionConflict, RecordConflict) as exc:
        print('SS-07 REVISION_CONFLICT: ' + str(exc), file=sys.stderr)
        return 3
    except (ConfigError, OSError, ValueError, RuntimeError, sqlite3.Error, KeyError) as exc:
        print('SS-07: ' + str(exc), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 0


if __name__ == '__main__':
    sys.exit(main())
