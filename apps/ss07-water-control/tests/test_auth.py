"""Credential, role, request-boundary and independent maintenance regression tests."""
import copy
import http.client
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from ss07.accounts import Accounts, password_record, validate_accounts, verify_password
from ss07.api import Server
from ss07.audit import Audit, AuditUnavailable
from ss07.auth import Auth, AuthError, COOKIE, public_origin
from ss07.config import ConfigError, ROOT, load, revision
from ss07.gateway import render as gateway_render, validate as gateway_validate
from ss07.maintenance import Client, Operations, run as maintenance_run
from ss07.media import render as media_render
from ss07.media_sessions import Grants, save_cleanup_config, load_cleanup_config
from ss07.persistence import RevisionConflict, atomic_json
from ss07.state import State
from ss07.volume import DirectoryVolume, open_directory
from test_recordings import RecordingFixture, CONTAINER, box

PASSWORD = 'Test-only SS07 password'
HASH = None


def account_fixture(path):
    global HASH
    if HASH is None: HASH = password_record(PASSWORD)
    data = dict(schema_version=1, users={name: dict(role=role, enabled=True, password=copy.deepcopy(HASH))
                for name, role in [('admin', 'maintainer'), ('viewer', 'monitor'), ('reader', 'reader')]})
    atomic_json(path, data, mode=0o640)
    return data


def eventually(predicate, timeout=4):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value: return value
        time.sleep(.02)
    raise AssertionError('condition timed out')


class AccountTests(unittest.TestCase):
    def test_real_password_hash_permissions_and_last_maintainer(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'accounts.json'; data = account_fixture(path); accounts = Accounts(path)
            self.assertTrue(verify_password(PASSWORD, data['users']['admin']['password']))
            self.assertFalse(verify_password('Other test-only password', data['users']['admin']['password']))
            self.assertNotIn('password', json.dumps(accounts.public()))
            before = path.read_bytes(); old = revision(data)
            with self.assertRaises(ConfigError): accounts.update('admin', 'monitor', True, old)
            self.assertEqual(path.read_bytes(), before)
            group = path.stat().st_gid
            new = accounts.update('viewer', 'reader', True, old)
            self.assertNotEqual(new, old); self.assertEqual(group, path.stat().st_gid)
            with self.assertRaises(RevisionConflict): accounts.update('reader', 'monitor', True, old)
            path.chmod(0o644)
            with self.assertRaises(ConfigError): accounts.read()
            path.chmod(0o640); target=Path(tmp)/'link';target.symlink_to(path)
            with self.assertRaises(OSError): Accounts(target).read()
            data['users']['admin']['password']['n'] = 2**30
            with self.assertRaises(ConfigError): validate_accounts(data)

    def test_account_cli_uses_stdin_and_never_returns_password_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'accounts.json'
            command = [sys.executable, '-m', 'ss07', 'init-accounts', '--accounts', str(path), '--username', 'admin', '--password-stdin']
            result = subprocess.run(command, input=PASSWORD+'\n', text=True, capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn(PASSWORD, result.stdout+result.stderr)
            self.assertNotIn('digest', result.stdout)
            self.assertEqual(subprocess.run(command, input=PASSWORD+'\n', text=True, capture_output=True, timeout=5).returncode, 3)

    def test_origin_and_gateway_have_no_wildcard_or_credential_shortcut(self):
        for value in (None, {}, 'http://localhost:8443', 'https://user:secret@localhost:8443', 'https://localhost:8443/', 'https://localhost:0'):
            with self.assertRaises(ValueError): public_origin(value)
        config=load(ROOT/'config/system.example.json')
        gateway=json.loads((ROOT/'config/gateway.example.json').read_text())
        self.assertIn('admin off', gateway_render(config, gateway))
        for fields in ({'listen_ip':'0.0.0.0'}, {'ice_ip':'224.0.0.1'}, {'private_key':'/tmp/key\nrespond 200'},
                       {'public_origin':'https://127.0.0.1:443'}, {'schema_version':True}):
            with self.assertRaises((ValueError, TypeError)): gateway_validate(dict(gateway, **fields))
        with self.assertRaises(ConfigError): gateway_render(config, dict(gateway, public_origin='https://127.0.0.1:8077'))
        media=media_render(config, dict(gateway, listen_ip='192.168.3.233', ice_ip='192.168.3.233'))
        self.assertEqual(media['webrtcAdditionalHosts'], ['192.168.3.233'])
        for field in ('apiAddress','rtspAddress','webrtcAddress'):
            self.assertTrue(media[field].startswith('127.0.0.1:'))

    def test_stop_hook_uses_loaded_media_port_not_unapplied_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'loaded.json';config=load(ROOT/'config/system.example.json')
            save_cleanup_config(path,config)
            config['media']={'api_port':10001}
            self.assertEqual(load_cleanup_config(path)['media']['api_port'],9997)
            path.chmod(0o644)
            with self.assertRaises(ValueError):load_cleanup_config(path)


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.path=self.root/'accounts.json';account_fixture(self.path)
        self.audit=Audit(self.root/'audit.sqlite');self.audit.initialize()
        self.now=1.0;self.auth=Auth(self.path, self.audit, 'https://127.0.0.1:8443', clock=lambda:self.now)

    def test_direct_access_without_accounts_is_bounded_and_expires(self):
        auth=Auth(None,self.audit,self.auth.origin,clock=lambda:self.now,direct_access=True)
        tokens=[auth.open_session()[0] for _ in range(32)]
        self.assertEqual(len(set(tokens)),32)
        session=auth.session(tokens[0],'maintain')
        self.assertFalse(auth.public(session)['authentication_required'])
        with self.assertRaises(AuthError) as limit:auth.open_session()
        self.assertEqual(limit.exception.code,'SESSION_LIMIT')
        with self.assertRaises(AuthError):auth.csrf(session,'x'*43)
        with self.assertRaises(AuthError):auth.login('admin',PASSWORD)
        self.now+=auth.IDLE_SECONDS
        renewed,_=auth.open_session()
        self.assertEqual(len(auth.sessions),1)
        with self.assertRaises(AuthError):auth.session(tokens[0])
        auth.logout(renewed)
        self.assertFalse(auth.active_keys())

    def test_idle_absolute_role_and_account_revision_revocation(self):
        token,_=self.auth.login('viewer',PASSWORD)
        with self.assertRaises(AuthError) as denied:self.auth.session(token,'maintain')
        self.assertEqual(denied.exception.status,403)
        self.now+=1800
        with self.assertRaises(AuthError):self.auth.session(token)
        self.now+=61;token,_=self.auth.login('admin',PASSWORD)
        for _ in range(25):
            self.now+=1700;self.auth.session(token)
        self.now+=700
        with self.assertRaises(AuthError):self.auth.session(token)
        self.now+=61;token,_=self.auth.login('admin',PASSWORD)
        accounts=Accounts(self.path);accounts.update('reader','monitor',True,accounts.public()['revision'])
        with self.assertRaises(AuthError):self.auth.session(token)
        self.assertFalse(self.auth.sessions)

    def test_rate_limit_session_bound_and_csrf(self):
        with patch('ss07.auth.verify_password',return_value=False) as verify:
            for _ in range(5):
                with self.assertRaises(AuthError) as denied:self.auth.login('unknown',PASSWORD)
                self.assertEqual(denied.exception.code,'INVALID_CREDENTIALS')
            with self.assertRaises(AuthError) as denied:self.auth.login('different-unknown',PASSWORD)
            self.assertEqual(denied.exception.status,429);self.assertEqual(verify.call_count,5)
        self.now+=61
        tokens=[self.auth.login('admin',PASSWORD)[0] for _ in range(5)]
        self.assertEqual(len(self.auth.sessions),4)
        with self.assertRaises(AuthError):self.auth.session(tokens[0])
        session=self.auth.session(tokens[-1]);Auth.csrf(session,session['csrf'])
        with self.assertRaises(AuthError):Auth.csrf(session,'x'*43)
        self.assertNotIn(tokens[-1],self.auth.sessions)

    def test_audit_failure_does_not_grant_access_or_prevent_revocation(self):
        with patch.object(self.audit,'append',side_effect=AuditUnavailable('test full disk')):
            with self.assertRaises(OSError):self.auth.login('admin',PASSWORD)
        self.assertFalse(self.auth.sessions)
        token,_=self.auth.login('admin',PASSWORD)
        with patch.object(self.audit,'append',side_effect=AuditUnavailable('test full disk')):
            with self.assertRaises(OSError):self.auth.logout(token)
        with self.assertRaises(AuthError):self.auth.session(token)

    def test_audit_retention_schema_and_atomic_initialization(self):
        with patch('ss07.audit.os.link',side_effect=OSError('injected publish failure')):
            other=Audit(self.root/'other.sqlite')
            with self.assertRaises(OSError):other.initialize()
        self.assertFalse(other.path.exists());self.assertFalse(list(self.root.glob('.ss07-audit-*')))
        db=self.audit.connect(writable=True)
        try:
            db.execute('BEGIN');db.executemany('INSERT INTO events(data) VALUES(?)',[('{}',)]*4100);db.execute('COMMIT')
        finally:db.close()
        self.audit.append('LOGIN_OK',username='admin',role='maintainer')
        db=self.audit.connect()
        try:self.assertEqual(db.execute('SELECT count(*) FROM events').fetchone()[0],4096)
        finally:db.close()
        self.assertEqual(self.audit.history(limit=1)['items'][0]['kind'],'LOGIN_OK')
        with self.assertRaises(ValueError):self.audit.append('LOGIN_OK',password=PASSWORD)
        with self.assertRaises(ValueError):self.audit.append('LOGIN_OK',username='user\nforged log')
        db=sqlite3.connect(str(self.audit.path));db.execute('PRAGMA user_version=2');db.close()
        with self.assertRaises(AuditUnavailable):self.audit.initialize()

    def test_slow_login_audit_does_not_block_existing_monitor_sessions(self):
        token,_=self.auth.login('viewer',PASSWORD)
        entered,release=threading.Event(),threading.Event()
        def slow(*args,**kwargs):entered.set();release.wait(3)
        with patch.object(self.audit,'append',side_effect=slow),patch('ss07.auth.verify_password',return_value=True):
            thread=threading.Thread(target=lambda:self.auth.login('admin',PASSWORD));thread.start()
            try:
                self.assertTrue(entered.wait(1));start=time.monotonic()
                self.auth.session(token,'monitor');self.assertLess(time.monotonic()-start,.2)
                with self.assertRaises(AuthError) as error:self.auth.login('reader',PASSWORD)
                self.assertEqual(error.exception.code,'LOGIN_BUSY')
            finally:release.set();thread.join(3)

    def test_native_session_ids_are_distinct_and_ended_grants_do_not_exhaust_quota(self):
        config=load(ROOT/'config/system.example.json')
        with patch.object(Grants,'_run'):
            grants=Grants(config,self.auth)
        grants.ready=True
        token,session=self.auth.login('admin',PASSWORD);key=session['key']
        native_id=str(uuid.uuid4());old_path='/media/front/whep/'+str(uuid.uuid4())
        marker=grants.begin(key);grants.add(old_path,key,marker);grants.end(key)
        with patch('ss07.media_sessions.inventory',return_value=[dict(id=native_id,userAgent=marker)]):grants.prune_ended()
        self.assertTrue(grants.owns(old_path,key))
        new_path='/media/back/whep/'+str(uuid.uuid4())
        def changed(config):
            marker=grants.begin(key);grants.add(new_path,key,marker);grants.end(key)
            return []  # Inventory captured before the new concurrent POST.
        with patch('ss07.media_sessions.inventory',side_effect=changed):grants.prune_ended()
        self.assertFalse(grants.owns(old_path,key));self.assertTrue(grants.owns(new_path,key))
        with patch('ss07.media_sessions.media_call',return_value=(200,b'')):grants.close()


class APIAuthTests(RecordingFixture):
    def setUp(self):
        super().setUp()
        self.accounts_path=self.base/'accounts.json';account_fixture(self.accounts_path)
        self.config=load(ROOT/'config/system.example.json');self.config_path=self.base/'system.json'
        atomic_json(self.config_path,self.config)
        self.audit=Audit(self.base/'access.sqlite');self.audit.initialize()
        self.auth=Auth(self.accounts_path,self.audit,'https://127.0.0.1:8443')
        self.state_dir=self.base/'state';self.state_dir.mkdir()
        self.stop=threading.Event()
        self.broker=threading.Thread(target=maintenance_run,args=(self.config_path,self.state_dir,self.catalog.path,
            self.base/'maintenance.sqlite',os.geteuid(),self.stop),daemon=True)
        self.broker.start();eventually(lambda:(self.state_dir/'maintenance.sock').exists())
        self.kick=patch('ss07.media_sessions.kick_orphans',return_value=True);self.kick.start()
        self.server=Server(('127.0.0.1',0),State(self.config,self.state_dir,self.config_path,self.catalog.path),
                           self.auth,Client(self.state_dir),Audit(self.base/'maintenance.sqlite'))
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()

    def tearDown(self):
        self.server.shutdown();self.thread.join(3);self.server.server_close()
        self.kick.stop();self.stop.set();self.broker.join(4)
        self.assertFalse(self.broker.is_alive());super().tearDown()

    def request(self,path,method='GET',body=None,session=None,headers=None):
        sent={'Host':self.auth.authority}
        if body is not None:sent.update({'Content-Type':'application/json'});body=json.dumps(body).encode()
        if session:sent.update(Cookie=COOKIE+'='+session[0],Origin=self.auth.origin,**{'X-CSRF-Token':session[1]['csrf_token']})
        sent.update(headers or {})
        connection=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=4)
        try:
            connection.request(method,path,body,sent);response=connection.getresponse()
            return response.status,dict(response.getheaders()),response.read()
        finally:connection.close()

    def login(self,name):
        status,headers,raw=self.request('/api/v1/login','POST',{'username':name,'password':PASSWORD},headers={'Origin':self.auth.origin})
        self.assertEqual(status,200,raw)
        self.assertIn('Secure; HttpOnly; SameSite=Strict; Path=/',headers['Set-Cookie'])
        return headers['Set-Cookie'].split(';',1)[0].split('=',1)[1],json.loads(raw)['data']

    def test_direct_workspace_session_read_write_and_browser_isolation(self):
        self.auth.direct_access=True
        self.accounts_path.unlink()  # No account file or password needed.
        def enter():
            status,headers,raw=self.request('/api/v1/session')
            self.assertEqual(status,200,raw)
            data=json.loads(raw)['data']
            self.assertFalse(data['authentication_required'])
            self.assertEqual(data['role'],'maintainer')
            self.assertIn('Secure; HttpOnly; SameSite=Strict',headers['Set-Cookie'])
            return headers['Set-Cookie'].split(';',1)[0].split('=',1)[1],data
        first,second=enter(),enter()
        self.assertNotEqual(first[0],second[0])
        self.assertNotEqual(first[1]['csrf_token'],second[1]['csrf_token'])
        status,headers,raw=self.request('/api/v1/session',session=first)
        self.assertEqual(status,200);self.assertNotIn('Set-Cookie',headers)
        for path in ('/api/v1/status','/api/v1/streams','/api/v1/recordings','/api/v1/config'):
            self.assertEqual(self.request(path,session=first)[0],200,path)
        with patch.object(self.server.maintenance,'request',return_value={'data':{},'request_id':'test'}) as operation:
            self.assertEqual(self.request('/api/v1/config/plan','POST',{},session=first)[0],200)
            self.assertEqual(operation.call_args.args[0],{'username':'local-operator','role':'maintainer'})
            operation.reset_mock()
            self.assertEqual(self.request('/api/v1/config/plan','POST',{},session=first,
                headers={'X-CSRF-Token':'x'*43})[0],403)
            self.assertEqual(self.request('/api/v1/config/plan','POST',{},session=first,
                headers={'Origin':'https://other.invalid'})[0],403)
            operation.assert_not_called()
        self.assertEqual(self.request('/api/v1/session',headers={'Sec-Fetch-Site':'cross-site'})[0],403)
        self.assertEqual(self.request('/api/v1/logout','POST',{},session=first)[0],200)
        self.assertEqual(self.request('/api/v1/status',session=first)[0],401)
        self.assertEqual(self.request('/api/v1/status',session=second)[0],200)
        enter()

    def test_roles_cannot_read_or_mutate_outside_permission(self):
        self.assertEqual(self.request('/api/v1/status')[0],401)
        self.assertEqual(self.request('/api/v1/session')[0],200)
        viewer=self.login('viewer')
        self.assertEqual(self.request('/api/v1/status',session=viewer)[0],200)
        for path in ('/api/v1/config','/api/v1/recordings','/api/v1/timecode/history','/api/v1/sonars/history','/api/v1/sonars/archives','/api/v1/logs'):
            self.assertEqual(self.request(path,session=viewer)[0],403)
        reader=self.login('reader')
        self.assertEqual(self.request('/api/v1/recordings',session=reader)[0],200)
        self.assertEqual(self.request('/api/v1/config','PUT',{},reader)[0],403)
        self.assertEqual(self.request('/api/v1/logout','POST',{},reader)[0],200)
        self.assertEqual(self.request('/api/v1/recordings',session=reader)[0],401)

    def test_sonar_history_is_reader_only_and_keeps_synthetic_marker(self):
        from ss07.sonar_journal import Journal
        from test_sonar import measurement, tagged
        journal=Journal(self.base/'sonar.sqlite');journal.initialize()
        journal.append([tagged(measurement())]);self.server.state.sonar_journal=journal
        self.assertEqual(self.request('/api/v1/sonars/history')[0],401)
        self.assertEqual(self.request('/api/v1/sonars/history',session=self.login('viewer'))[0],403)
        for name in ('reader','admin'):
            status,headers,raw=self.request('/api/v1/sonars/history',session=self.login(name))
            self.assertEqual(status,200)
            self.assertEqual(json.loads(raw)['data']['items'][0]['evidence_kind'],'synthetic_test')

    def test_sonar_archive_metadata_and_download_require_reader_permission(self):
        from ss07.sonar_archive import Catalog as SonarCatalog,Writer
        from ss07.volume import DirectoryVolume,open_directory
        from test_sonar import measurement,tagged
        catalog=SonarCatalog(self.base/'sonar-archives.sqlite');catalog.initialize()
        with DirectoryVolume(open_directory(self.root),'test-volume','synthetic_test') as volume:
            writer=Writer(catalog,volume);writer.append([tagged(measurement())]);meta=writer.finish()
        self.server.state.sonar_catalog=catalog
        viewer=self.login('viewer');reader=self.login('reader')
        path='/api/v1/sonars/archives/'+meta['archive_id']+'/content'
        self.assertEqual(self.request(path,session=viewer)[0],403)
        def volume(*args,**kwargs):return DirectoryVolume(open_directory(self.root),'test-volume','synthetic_test')
        with patch('ss07.sonar_archive_http.MountLease.acquire',side_effect=volume):
            self.assertEqual(self.request('/api/v1/sonars/archives',session=reader)[0],200)
            status,headers,raw=self.request(path,session=reader)
            self.assertEqual(status,200);self.assertIn(b'synthetic_test',raw)

    def test_host_origin_csrf_and_sensitive_logs(self):
        self.assertEqual(self.request('/api/v1/status',headers={'Host':'attacker.invalid'})[0],400)
        self.assertEqual(self.request('/api/v1/login','POST',{'username':'admin','password':PASSWORD})[0],403)
        admin=self.login('admin')
        body={'candidate':self.config,'expected_revision':revision(self.config)}
        self.assertEqual(self.request('/api/v1/config','PUT',body,admin,{'Origin':'https://attacker.invalid'})[0],403)
        self.assertEqual(self.request('/api/v1/config','PUT',body,admin,{'X-CSRF-Token':'x'*43})[0],403)
        status,headers,raw=self.request('/api/v1/logs?source=access&download=1',session=admin)
        self.assertEqual(status,200);self.assertIn('attachment',headers['Content-Disposition'])
        self.assertNotIn(PASSWORD.encode(),raw);self.assertNotIn(admin[0].encode(),raw)
        self.assertEqual(self.request('/api/v1/logs?source=../../etc/passwd',session=admin)[0],422)
        self.assertEqual(self.request('/api/v1/logs?source=recording&cursor=2',session=admin)[0],422)

    def test_logs_default_query_and_malformed_fields(self):
        admin=self.login('admin')
        for path in ('/api/v1/logs','/api/v1/logs?'):
            status,headers,raw=self.request(path,session=admin)
            self.assertEqual(status,200,raw)
            self.assertEqual(json.loads(raw)['data']['source'],'access')
            self.assertNotIn(PASSWORD.encode(),raw)
            self.assertNotIn(admin[0].encode(),raw)
        for query in ('source','source=access&&limit=50','source=','limit=1&limit=2'):
            self.assertEqual(self.request('/api/v1/logs?'+query,session=admin)[0],422)

    def test_rtsp_audit_is_exportable_only_by_maintainer(self):
        audit=Audit(self.base/'rtsp.sqlite');audit.initialize()
        audit.append('RTSP_REVOKE',outcome='relay_closed')
        self.server.rtsp_audit=audit
        for name in ('viewer','reader'):
            self.assertEqual(self.request('/api/v1/logs?source=rtsp',session=self.login(name))[0],403)
        status,headers,raw=self.request('/api/v1/logs?source=rtsp&download=1',session=self.login('admin'))
        self.assertEqual(status,200)
        self.assertIn('ss07-rtsp-events.json',headers['Content-Disposition'])
        self.assertEqual(json.loads(raw)['data']['items'][0]['kind'],'RTSP_REVOKE')

    def test_config_cas_persisted_is_distinct_from_running(self):
        admin=self.login('admin');candidate=copy.deepcopy(self.config);candidate['device_id']='new-device'
        body={'candidate':candidate,'expected_revision':revision(self.config)}
        result=self.request('/api/v1/config','PUT',body,admin)
        self.assertEqual(result[0],200,result[2]);self.assertFalse(json.loads(result[2])['data']['hardware_applied'])
        self.assertEqual(self.request('/api/v1/config','PUT',body,admin)[0],409)
        data=json.loads(self.request('/api/v1/config',session=admin)[2])['data']
        self.assertTrue(data['pending_restart']);self.assertNotEqual(data['revision'],data['loaded_revision'])
        self.assertEqual(self.server.state.config['device_id'],self.config['device_id'])
        self.assertEqual(load(self.config_path)['device_id'],'new-device')
        body['candidate']['timecode']['read_only']=False;body['expected_revision']=data['revision']
        self.assertEqual(self.request('/api/v1/config','PUT',body,admin)[0],422)

    def test_lock_requires_permission_cas_and_real_volume_gate(self):
        item=self.segment();path='/api/v1/recordings/'+item['recording_id']+'/lock'
        admin=self.login('admin');body=dict(locked=True,expected_revision='0')
        self.assertEqual(self.request(path,'POST',body,self.login('reader'))[0],403)
        self.assertEqual(self.request(path,'POST',body,admin)[0],409)
        def volume(*args,**kwargs):return DirectoryVolume(open_directory(self.root),'test-volume','synthetic_test')
        with patch('ss07.maintenance.MountLease.acquire',side_effect=volume):
            self.assertEqual(self.request(path,'POST',body,admin)[0],200)
            self.assertEqual(self.request(path,'POST',dict(locked=False,expected_revision='0'),admin)[0],409)
        self.assertTrue(self.catalog.list()['items'][0]['locked'])

    def test_broker_checks_peer_uid_and_download_limit_preserves_status(self):
        with self.assertRaises(OSError):Client(self.state_dir,os.geteuid()+123).request({'username':'admin','role':'maintainer'},'config.update',{})
        admin=self.login('admin')
        for _ in range(4):self.server.download_slots.acquire()
        try:
            self.assertEqual(self.request('/api/v1/recordings/'+str(uuid.uuid4())+'/content',session=admin)[0],503)
            self.assertEqual(self.request('/api/v1/status',session=admin)[0],200)
        finally:
            for _ in range(4):self.server.download_slots.release()

    def test_expired_session_closes_open_event_stream(self):
        admin=self.login('admin');now=[time.monotonic()];self.auth.clock=lambda:now[0]
        self.server.event_interval=.03
        connection=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=2)
        try:
            connection.request('GET','/api/v1/events',headers={'Host':self.auth.authority,'Cookie':COOKIE+'='+admin[0]})
            response=connection.getresponse();self.assertEqual(response.status,200)
            for _ in range(5):response.readline()
            now[0]+=self.auth.ABSOLUTE_SECONDS
            response.read()  # EOF within the socket deadline, without waiting for a page reload.
            self.assertEqual(self.request('/api/v1/status',session=admin)[0],401)
        finally:connection.close()

    def test_logout_interrupts_download_between_bounded_sendfile_chunks(self):
        item=self.segment(content=CONTAINER+box(b'free',b'x'*(2*1048576)))
        admin=self.login('admin');original=socket.socket.sendfile
        def revoke_after_chunk(sock,stream,*args,**kwargs):
            sent=original(sock,stream,*args,**kwargs)
            self.auth.logout(admin[0]);return sent
        def volume(*args,**kwargs):return DirectoryVolume(open_directory(self.root),'test-volume','synthetic_test')
        connection=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=3)
        try:
            with patch('ss07.recording_http.MountLease.acquire',side_effect=volume),patch.object(socket.socket,'sendfile',revoke_after_chunk):
                connection.request('GET','/api/v1/recordings/'+item['recording_id']+'/content',
                    headers={'Host':self.auth.authority,'Cookie':COOKIE+'='+admin[0]})
                response=connection.getresponse();self.assertEqual(response.status,200)
                with self.assertRaises(http.client.IncompleteRead) as short:response.read()
                self.assertEqual(len(short.exception.partial),1048576)
        finally:connection.close()

    def test_maintenance_audit_failures_report_uncertain_without_blind_retry(self):
        operations=Operations(self.config_path,self.catalog.path,self.base/'maintenance.sqlite')
        candidate=copy.deepcopy(self.config);candidate['device_id']='changed'
        request=dict(schema_version=1,request_id=str(uuid.uuid4()),actor={'username':'admin','role':'maintainer'},
                     operation='config.update',payload={'candidate':candidate,'expected_revision':revision(self.config)})
        with patch.object(operations.audit,'append',side_effect=AuditUnavailable('before write')):
            self.assertEqual(operations.execute(request)['error']['status'],503)
        self.assertEqual(load(self.config_path),self.config)
        with patch.object(operations.audit,'append',side_effect=[None,AuditUnavailable('after write')]):
            self.assertEqual(operations.execute(request)['error']['code'],'MAINTENANCE_RESULT_UNCERTAIN')
        self.assertEqual(load(self.config_path),candidate)


if __name__ == '__main__':unittest.main()
