"""Synthetic web integration tests; never opens production config, Google or SMTP."""
import base64
import io
import multiprocessing
import os
import tempfile
import time
import unittest
import uuid
from dataclasses import replace
from pathlib import Path

WEB_AVAILABLE = True
try:
    from fastapi.testclient import TestClient
    from sqlalchemy import text, event
except (ImportError, RuntimeError):
    WEB_AVAILABLE = False

if WEB_AVAILABLE:
    from event_checkin.web.adapters import FakeSheets
    from event_checkin.web.app import COOKIE, create_app
    from event_checkin.web.db import Database, many, one
    from event_checkin.web.security import digest, hash_password
    from event_checkin.web.seed import payload, token
    from event_checkin.web.service import AdmissionService, Conflict, NotReady
    from event_checkin.web.settings import WebSettings
    from event_checkin.web.sync import Synchronizer

PASSWORD = 'synthetic-test-password-only'
PASSWORD_HASH = hash_password(PASSWORD) if WEB_AVAILABLE else ""


def race_admission(url, settings, staff, request_id, gate, queue):
    db = Database(url)
    gate.wait(15)
    try:
        queue.put(AdmissionService(db, settings).confirm('DEMO-P-lite', request_id, 'qr', staff))
    except Exception as error:
        queue.put({'error': type(error).__name__})
    finally:
        db.engine.dispose()


@unittest.skipUnless(WEB_AVAILABLE, "Install requirements-web.txt in the isolated web environment")
class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.s = WebSettings(database_url=os.environ.get('WEB_TEST_DATABASE_URL', 'sqlite:///' + str(root / 'web.sqlite3')),
                             password_hash=PASSWORD_HASH, fixture=root / 'sheets.json', event_id='freshcoat-synthetic-' + uuid.uuid4().hex)
        self.fake = FakeSheets(self.s)
        self.fake.write(payload(self.s.event_id))
        self.app = create_app(self.s, self.fake, background=False)
        self.client = TestClient(self.app, base_url=self.s.origin)
        self.client.__enter__()
        self.db, self.service = self.app.state.db, self.app.state.service
        self.service.import_rows(self.fake.rows())
        self.service.open_admissions()
        with self.db.transaction() as conn:
            conn.execute(text('DELETE FROM login_attempts'))
        self.headers = self.login()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temp.cleanup()

    def login(self, nickname='Kai', client=None):
        c = client or self.client
        response = c.post('/api/login', json={'password': PASSWORD}, headers={'origin': self.s.origin})
        self.assertEqual(response.status_code, 200)
        headers = {'origin': self.s.origin, 'x-csrf-token': response.json()['csrf']}
        if nickname is not None:
            self.assertEqual(c.post('/api/nickname', json={'nickname': nickname}, headers=headers).status_code, 200)
        return headers

    def post(self, path, body, headers=None):
        return self.client.post('/api/' + path, json=body, headers=headers or self.headers)

    def confirm(self, key='lite', request_id=None, method='qr'):
        return self.post('checkins', {'participant_id': 'DEMO-P-' + key, 'request_id': request_id or str(uuid.uuid4()), 'method': method})

    def count(self, table):
        with self.db.transaction() as conn:
            return one(conn, 'SELECT COUNT(*) AS n FROM ' + table + ' WHERE event_id=:e', e=self.s.event_id)['n']

    def staff(self):
        with self.db.transaction() as conn:
            return one(conn, 'SELECT * FROM staff_sessions WHERE cookie_digest=:h', h=digest(self.client.cookies.get(COOKIE)))

    def test_separate_api_origin_login_and_request_checks(self):
        frontend = "https://staff.example.invalid"
        backend = "https://api.example.invalid"
        settings = replace(self.s, origin=frontend, api_origin=backend)
        app = create_app(settings, self.fake, background=False)
        with TestClient(app, base_url=backend) as client:
            self.assertEqual(client.get('/api/session').json(), {'authenticated': False})
            response = client.post('/api/login', json={'password': PASSWORD}, headers={'origin': frontend})
            self.assertEqual(response.status_code, 200)
            self.assertIn('Secure', response.headers['set-cookie'])
            self.assertIn('HttpOnly', response.headers['set-cookie'])
            headers = {'origin': frontend, 'x-csrf-token': response.json()['csrf']}
            self.assertEqual(client.post('/api/nickname', json={'nickname': 'Vercel staff'}, headers=headers).status_code, 200)
            self.assertEqual(client.get('/api/dashboard').status_code, 200)
            self.assertEqual(client.post('/api/sync', json={}, headers={'origin': frontend}).status_code, 403)
            self.assertEqual(client.post('/api/sync', json={}, headers={**headers, 'origin': backend}).status_code, 403)
            self.assertEqual(client.post('/api/sync', json={}, headers={**headers, 'origin': 'https://evil.invalid'}).status_code, 403)
            self.assertEqual(client.get('/api/session', headers={'host': 'evil.invalid', 'x-forwarded-host': 'api.example.invalid'}).status_code, 400)
            self.assertEqual(client.post('/api/logout', json={}, headers=headers).status_code, 200)
            self.assertEqual(client.get('/api/dashboard').status_code, 401)

    def test_separate_api_origin_validation_and_environment_loading(self):
        from unittest.mock import patch
        settings = replace(self.s, origin='https://staff.example.invalid', api_origin='https://api.example.invalid')
        self.assertIs(settings.validate(), settings)
        for origin in ('http://api.example.invalid', 'https://api.example.invalid/path',
                       'https://user:secret@api.example.invalid', 'https://api.example.invalid?x=1'):
            with self.assertRaises(ValueError):
                replace(settings, api_origin=origin).validate()
        with self.assertRaises(ValueError):
            replace(settings, origin='http://localhost:8080').validate()
        with self.assertRaises(ValueError):
            replace(settings, read_only_preview=True).validate()
        with patch.dict(os.environ, {
            'WEB_ENV_FILE': str(Path(self.temp.name) / 'absent.env'),
            'WEB_PASSWORD_HASH': PASSWORD_HASH, 'WEB_ORIGIN': settings.origin,
            'WEB_API_ORIGIN': settings.api_origin,
        }, clear=True):
            self.assertEqual(WebSettings.load().api_origin, settings.api_origin)

    def test_auth_nickname_cookie_csrf_logout_expiry(self):
        original_cookie = self.client.cookies.get(COOKIE)
        session = self.client.get('/api/session').json()
        self.assertEqual(session['nickname'], 'Kai')
        self.assertNotIn('session_id', session)
        with self.db.transaction() as conn:
            stored = one(conn, 'SELECT * FROM staff_sessions WHERE cookie_digest=:h', h=digest(original_cookie))
        self.assertNotEqual(stored['session_id'], original_cookie)
        self.assertNotIn(original_cookie, str(stored))
        self.assertEqual(self.post('sync', {}, {'origin': self.s.origin}).status_code, 403)
        self.assertEqual(self.post('sync', {}, {'origin': 'https://evil.invalid', 'x-csrf-token': self.headers['x-csrf-token']}).status_code, 403)
        self.assertEqual(self.post('nickname', {'nickname': 'Other operator'}).status_code, 409)
        self.assertEqual(self.post('logout', {}).status_code, 200)
        self.client.cookies.set(COOKIE, original_cookie)
        self.assertEqual(self.client.get('/api/dashboard').status_code, 401)
        self.client.cookies.clear()
        self.headers = self.login(None)
        self.assertEqual(self.client.get('/api/dashboard').status_code, 403)
        self.assertEqual(self.post('lookup', {'token': token('lite')}).status_code, 403)
        self.assertEqual(self.post('nickname', {'nickname': '   '}).status_code, 422)
        self.assertEqual(self.post('nickname', {'nickname': 'A\x00B'}).status_code, 422)
        self.assertEqual(self.post('nickname', {'nickname': '  <Kai>  '}).json()['nickname'], '<Kai>')
        self.assertEqual(self.client.get('/api/session').json()['nickname'], '<Kai>')
        with self.db.transaction() as conn:
            conn.execute(text('UPDATE staff_sessions SET expires_at=:t'), {'t': time.time() - 1})
        self.assertEqual(self.client.get('/api/participants').status_code, 401)

    def test_unauthorized_endpoints_and_login_origin(self):
        self.client.cookies.clear()
        for path in ['/api/dashboard', '/api/participants', '/api/participants/DEMO-P-lite', '/api/requests/' + str(uuid.uuid4())]:
            self.assertEqual(self.client.get(path).status_code, 401)
        for path, body in [('lookup', {'token': token('lite')}), ('decode', {'image': 'bad'}), ('checkins', {'participant_id':'x','request_id':str(uuid.uuid4()),'method':'qr'}), ('sync', {})]:
            self.assertEqual(self.post(path, body).status_code, 401)
        self.assertEqual(self.client.post('/api/login', json={'password': PASSWORD}).status_code, 403)
        self.assertEqual(self.post('login', {'password': 'wrong'}, {'origin':self.s.origin}).status_code, 401)
        self.assertEqual(self.client.get('/api/session').json(), {'authenticated': False})

    def test_throttle_persists_and_secure_cookie_configuration(self):
        for _ in range(14):
            response = self.post('login', {'password': 'wrong'})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.post('login', {'password': PASSWORD}).status_code, 429)
        self.assertEqual(self.client.get('/api/dashboard').status_code, 200)
        with self.db.transaction() as conn:
            self.assertEqual(one(conn, 'SELECT attempts FROM login_attempts WHERE bucket=:b', b='global')['attempts'],15)
        self.assertTrue(replace(self.s, origin='https://staff.example.invalid').secure_cookie)
        with self.assertRaises(ValueError): replace(self.s, origin='http://staff.example.invalid').validate()
        with self.assertRaises(ValueError): replace(self.s, password_hash='').validate()
        with self.assertRaises(ValueError): replace(self.s, synthetic=False).validate()

    def test_real_login_cookie_headers(self):
        result = self.post('login', {'password': PASSWORD})
        cookie = result.headers['set-cookie']
        for attribute in ['HttpOnly', 'SameSite=strict', 'Max-Age=28800']:
            self.assertIn(attribute, cookie)
        self.assertNotIn('Secure', cookie)
        self.assertIn("frame-ancestors 'none'", result.headers['content-security-policy'])
        self.assertEqual(result.headers['cache-control'], 'no-store')

    def test_lookup_search_review_are_read_only_no_tokens_exposed(self):
        before = self.count('attendance')
        result = self.post('lookup', {'token': token('lite')}).json()
        self.assertEqual(result['status'], 'found')
        self.assertEqual(result['participant']['checked_in_at'], None)
        search = self.client.get('/api/participants?q=alex.riv').json()
        self.assertEqual(search['total'], 2)
        self.assertNotIn('token', str(search))
        self.assertEqual(self.client.get('/api/participants/DEMO-P-pro').status_code, 200)
        self.assertEqual(self.count('attendance'), before)
        self.assertEqual(self.count('outbox'), 0)
        self.assertEqual(self.client.get('/api/participants?limit=101').status_code, 422)
        self.assertEqual(self.client.get('/api/participants?q=MARÍA').json()['total'], 1)
        self.assertEqual(self.client.get('/api/participants?q=%25').json()['total'], 0)
        self.assertEqual(len(self.client.get('/api/participants?limit=2&offset=2').json()['items']), 2)
        self.assertEqual(self.client.get('/api/participants?attendance=ineligible').json()['total'], 5)

    def test_commit_duplicate_attribution_and_idempotency(self):
        request_id = str(uuid.uuid4())
        first = self.confirm(request_id=request_id, method='manual').json()
        self.assertEqual(first['status'], 'accepted')
        p = first['participant']
        self.assertEqual((p['participant_id'],p['checked_in_by'],p['method']),('DEMO-P-lite','Kai','manual'))
        self.assertLess(abs(p['checked_in_at']-time.time()), 5)
        # Treat the committed response as lost. A fresh GET and replay recover it.
        self.assertEqual(self.client.get('/api/requests/'+request_id).json()['result'], first)
        self.assertEqual(self.confirm(request_id=request_id, method='manual').json(), first)
        restarted=Database(self.s.database_url)
        self.assertEqual(AdmissionService(restarted,self.s).confirm('DEMO-P-lite',request_id,'manual',self.staff()),first)
        restarted.engine.dispose()
        duplicate = self.confirm(method='qr').json()
        self.assertEqual(duplicate['status'], 'duplicate')
        self.assertEqual(duplicate['participant']['checked_in_at'], p['checked_in_at'])
        self.assertEqual(self.confirm('pro',request_id,method='manual').status_code,409)
        self.assertEqual(self.confirm(request_id=request_id,method='qr').status_code,409)
        self.assertEqual(self.count('attendance'),2)
        self.assertEqual(self.count('outbox'),1)
        self.assertEqual(self.post('checkins', {'participant_id':'DEMO-P-pro','request_id':str(uuid.uuid4()),'method':'qr','checked_in_by':'Forged'}).status_code,422)
        self._staff_id = self.staff()['session_id']
        with self.db.transaction() as conn:
            self.assertEqual(one(conn,'SELECT staff_session_id FROM attendance WHERE event_id=:e AND participant_id=:p',e=self.s.event_id,p='DEMO-P-lite')['staff_session_id'],self.staff_id_without_transaction())

    def staff_id_without_transaction(self):
        # Session identity is intentionally separate from cookie secret.
        return self._staff_id if hasattr(self,'_staff_id') else self.staff()['session_id']

    def test_invalid_cancelled_duplicate_test_unknown_tickets(self):
        for key in ['cancelled','invalid','controlled','unapproved','duplicate']:
            self.assertEqual(self.confirm(key).json()['status'],'invalid')
        self.assertEqual(self.post('lookup', {'token':'https://example.invalid/'}).json()['status'],'invalid')
        self.assertEqual(self.post('lookup', {'token':'INVALID_TEST_'+'x'*30}).json()['status'],'invalid')
        self.assertEqual(self.post('lookup', {'token':token('unknown')}).json()['status'],'unknown')
        self.assertEqual(self.confirm('arrived').json()['status'],'duplicate')
        self.assertEqual(self.count('outbox'),0)
        self.assertEqual(self.count('attendance'),1)

    def test_database_uniqueness_and_independent_process_race(self):
        staff = self.staff()
        ctx = multiprocessing.get_context('spawn')
        gate, queue = ctx.Event(), ctx.Queue()
        processes=[ctx.Process(target=race_admission,args=(self.s.database_url,self.s,staff,str(uuid.uuid4()),gate,queue)) for _ in range(3)]
        for process in processes: process.start()
        gate.set()
        results=[queue.get(timeout=30) for _ in processes]
        for process in processes:
            process.join(30)
            self.assertEqual(process.exitcode,0)
        self.assertEqual(sorted(r.get('status',r) for r in results),['accepted','duplicate','duplicate'])
        self.assertEqual(len({r['participant']['checked_in_at'] for r in results}),1)
        self.assertEqual(self.count('outbox'),1)

    def test_registration_refresh_cannot_overwrite_attendance_or_remap_identity(self):
        first=self.confirm().json()
        updated=self.fake.read_payload()
        updated['sources']['regular'][0]['ticket_status']='CANCELLED'
        self.fake.write(updated)
        self.service.import_rows(self.fake.rows())
        self.assertFalse(self.post('lookup',{'token':token('lite')}).json()['participant']['eligible'])
        self.assertEqual(self.confirm().json()['participant']['checked_in_at'],first['participant']['checked_in_at'])
        self.assertEqual(self.count('outbox'),1)
        updated['sources']['regular'][0]['ticket_token']=token('remapped')
        self.fake.write(updated)
        with self.assertRaises(Conflict): self.service.import_rows(self.fake.rows())
        self.assertEqual(self.post('lookup',{'token':token('lite')}).json()['status'],'found')

    def test_partial_reads_missing_records_and_duplicate_tokens_keep_snapshot(self):
        data=self.fake.read_payload()
        data['sources']['regular'].pop()
        self.fake.write(data)
        with self.assertRaises(Conflict): self.service.import_rows(self.fake.rows())
        self.assertEqual(self.client.get('/api/participants').json()['total'],12)
        data=payload(self.s.event_id)
        data['sources']['regular'][1]['ticket_token']=data['sources']['regular'][0]['ticket_token']
        self.fake.write(data)
        self.assertEqual(self.app.state.sync.run_once(force=True)['status'],'error')
        self.assertEqual(self.client.get('/api/participants').json()['total'],12)
        self.assertTrue(self.service.dashboard()['sync']['error'])
        data=payload(self.s.event_id);data['fail_reads']=True;self.fake.write(data)
        self.assertEqual(self.app.state.sync.run_once(force=True)['status'],'error')
        self.assertEqual(self.count('attendance'),1)

    def test_initial_attendance_special_tiers_and_frozen_aliases(self):
        data=self.service.dashboard()
        self.assertEqual(data['counts'],{'eligible':7,'arrived':1,'waiting':6})
        self.assertEqual(self.client.get('/api/participants?tier=Special%20Ticket').json()['items'][0]['source_id'],'special')
        self.assertEqual(self.client.get('/api/participants?tier=Pass').json()['items'][0]['tier'],'Pass')
        self.assertEqual(self.client.get('/api/participants?tier=Regular').json()['total'],3)
        self.assertEqual(data['activity'][0]['method'],'legacy')
        self.assertEqual(self.count('outbox'),0)
        changed=self.fake.read_payload();changed['sources']['regular'][0]['checked_in_at']='2026-11-23T01:00:00+00:00';self.fake.write(changed)
        with self.assertRaises(Conflict):self.service.import_rows(self.fake.rows())

    def test_sync_outbox_restart_batch_export_failures_and_source_mapping(self):
        for key in ['regular','special','pass']: self.assertEqual(self.confirm(key).json()['status'],'accepted')
        data=self.fake.read_payload();data['fail_exports']=True;self.fake.write(data)
        self.assertEqual(self.app.state.sync.run_once(force=True)['status'],'complete')
        self.assertEqual(self.service.dashboard()['sync']['pending_exports'],3)
        self.assertTrue(self.service.dashboard()['sync']['export_error'])
        # Recreate service/engine like a process restart; force retry due time in the test.
        second=Database(self.s.database_url)
        restart=Synchronizer(AdmissionService(second,self.s),FakeSheets(self.s))
        data['fail_exports']=False
        # Reorder rows: the correct source record must still receive its own update.
        data['sources']['special'].reverse()
        self.fake.write(data)
        with second.transaction() as conn:conn.execute(text('UPDATE outbox SET next_attempt=0 WHERE event_id=:e'),{'e':self.s.event_id})
        restart.run_once(force=True)
        self.assertEqual(self.service.dashboard()['sync']['pending_exports'],0)
        current={r.get('participant_id'):r for r in self.fake.rows()}
        for key in ['regular','special','pass']:
            r=current['DEMO-P-'+key]
            self.assertEqual(r.get('checked_in_by'),'Kai')
            self.assertEqual(r.get('email_status'),'SENT')
            self.assertTrue(r.get('checked_in_at'))
        self.assertEqual(current['DEMO-P-special'].get('ticket_tier'),'Special Ticket')
        self.assertEqual(current['DEMO-P-pass'].get('ticket_tier'),'Pass')
        self.assertEqual(current['DEMO-P-lite'].get('checked_in_at'),'')
        restart.run_once(force=True)
        self.assertFalse(self.service.dashboard()['sync']['error'])
        second.engine.dispose()

    def test_unissued_can_become_issued_and_dry_run_is_non_mutating(self):
        data=self.fake.read_payload(); row=data['sources']['regular'][0]
        for field in ['participant_id','ticket_token','ticket_name','ticket_tier','normalized_email','source_id','price_category']:row[field]=''
        # Separate fresh event/database demonstrates unissued stable records.
        other=replace(self.s,event_id='unissued-event',database_url='sqlite:///'+str(Path(self.temp.name)/'unissued.db'))
        for records in data['sources'].values():
            for r in records:r['event_id']=other.event_id
        data['event_id']=other.event_id
        sheets=FakeSheets(other);sheets.write(data)
        db=Database(other.database_url);db.migrate(other);svc=AdmissionService(db,other)
        report=svc.import_rows(sheets.rows(),dry_run=True)
        self.assertEqual(report['registrations'],12)
        self.assertEqual(svc.list_participants()['total'],0)
        svc.import_rows(sheets.rows())
        restored=payload(other.event_id);sheets.write(restored);svc.import_rows(sheets.rows())
        self.assertEqual(svc.list_participants()['total'],12)
        db.engine.dispose()

    def test_real_qr_image_decode_without_camera_never_admits(self):
        import qrcode
        image=qrcode.make(token('lite')); output=io.BytesIO();image.save(output,format='PNG')
        response=self.post('decode',{'image':'data:image/png;base64,'+base64.b64encode(output.getvalue()).decode()})
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json()['participant']['participant_id'],'DEMO-P-lite')
        self.assertEqual(self.count('attendance'),1)
        self.assertEqual(self.post('decode',{'image':'data:image/png;base64,not-image'}).status_code,422)
        self.assertEqual(self.post('decode',{'image':'data:image/png;base64,'+'a'*400000}).status_code,413)

    def test_transaction_rolls_back_attendance_when_outbox_cannot_commit(self):
        def fail_outbox(conn, cursor, statement, parameters, context, executemany):
            if statement.startswith('INSERT INTO outbox'):
                raise RuntimeError('simulated durable storage failure')
        event.listen(self.db.engine, 'before_cursor_execute', fail_outbox)
        try:
            with self.assertRaises(RuntimeError):
                self.service.confirm('DEMO-P-lite', str(uuid.uuid4()), 'manual', self.staff())
        finally:
            event.remove(self.db.engine, 'before_cursor_execute', fail_outbox)
        self.assertEqual(self.count('attendance'), 1)
        self.assertEqual(self.count('requests'), 0)
        self.assertEqual(self.count('outbox'), 0)

    def test_slow_sheet_read_does_not_hold_admission_transaction(self):
        import threading
        started, finish = threading.Event(), threading.Event()
        original = self.fake.rows
        def slow_read():
            started.set()
            finish.wait(10)
            return original()
        self.fake.rows = slow_read
        sync_thread = threading.Thread(target=lambda:self.app.state.sync.run_once(force=True))
        sync_thread.start()
        self.assertTrue(started.wait(5))
        try:
            self.assertEqual(self.confirm('pro').json()['status'], 'accepted')
            self.assertTrue(sync_thread.is_alive())
        finally:
            finish.set();sync_thread.join(15)
        self.assertFalse(sync_thread.is_alive())

    def test_reviewed_shared_email_exception_and_approval_refresh(self):
        from event_checkin.shared_email import review_members, save_approval
        from event_checkin.production_sheets import FixtureSheets
        # A separate fresh snapshot, before freezing any issued identity in this DB.
        data=payload(self.s.event_id)
        first,second=data['sources']['regular'][:2]
        first['email']=first['normalized_email']='team@example.invalid'
        second['email']=second['normalized_email']='team@example.invalid'
        self.fake.write(data)
        rows=FixtureSheets(data).rows()
        review,sha,_=review_members(self.fake.approval_settings,rows,'regular',[2,3])
        save_approval(self.fake.approval_settings,review,sha,confirm_distinct_attendees=True,confirm_payment_coverage=True)
        from event_checkin.web.service import eligibility
        annotated=self.fake.rows()
        self.assertTrue(eligibility(annotated[0],annotated,self.s.event_id,True)[0])
        self.assertTrue(eligibility(annotated[1],annotated,self.s.event_id,True)[0])
        data['sources']['regular'][1]['confirmation']=False
        self.fake.write(data)
        annotated=self.fake.rows()
        self.assertFalse(eligibility(annotated[1],annotated,self.s.event_id,True)[0])
        # A form detail edit invalidates reviewed group bindings for both attendees.
        data['sources']['regular'][1]['name']='Changed name'
        self.fake.write(data)
        annotated=self.fake.rows()
        self.assertFalse(eligibility(annotated[0],annotated,self.s.event_id,True)[0])

    def test_background_worker_import_and_queued_export(self):
        isolated=replace(self.s,database_url='sqlite:///'+str(Path(self.temp.name)/'background.db'),
                         fixture=Path(self.temp.name)/'background-sheets.json')
        fake=FakeSheets(isolated);fake.write(payload(isolated.event_id))
        with TestClient(create_app(isolated,fake,background=True),base_url=isolated.origin) as client:
            self.login('Background shift',client)
            deadline=time.time()+5
            while time.time()<deadline:
                if client.app.state.service.dashboard()['sync']['last_success']:break
                time.sleep(0.05)
            client.app.state.service.open_admissions()
            session=client.get('/api/session').json()
            headers={'origin':isolated.origin,'x-csrf-token':session['csrf']}
            result=client.post('/api/checkins',headers=headers,json={'participant_id':'DEMO-P-pro','method':'manual','request_id':str(uuid.uuid4())})
            self.assertEqual(result.json()['status'],'accepted')
            self.assertEqual(client.post('/api/sync',headers=headers,json={}).json()['status'],'queued')
            deadline=time.time()+6
            while time.time()<deadline:
                if not client.app.state.service.dashboard()['sync']['pending_exports']:break
                time.sleep(0.05)
            self.assertEqual(client.app.state.service.dashboard()['sync']['pending_exports'],0)
            row=next(r for r in fake.rows() if r.get('participant_id')=='DEMO-P-pro')
            self.assertEqual(row.get('checked_in_by'),'Background shift')

    def test_secure_cookie_on_https(self):
        secure=replace(self.s,origin='https://staff.example.invalid')
        with TestClient(create_app(secure,self.fake,background=False),base_url=secure.origin) as client:
            response=client.post('/api/login',json={'password':PASSWORD},headers={'origin':secure.origin})
            self.assertEqual(response.status_code,200)
            self.assertIn('Secure',response.headers['set-cookie'])
            self.assertIn('HttpOnly',response.headers['set-cookie'])
            self.assertIn('max-age=',response.headers['strict-transport-security'])

    def test_sheets_commit_with_lost_response_can_replay_outbox(self):
        result=self.confirm('pro').json()
        original=self.fake.export_batch
        def write_then_lose_response(items):
            original(items)
            raise ConnectionError('Synthetic response lost after Sheet write')
        self.fake.export_batch=write_then_lose_response
        self.app.state.sync.run_once(force=True)
        self.assertEqual(self.service.dashboard()['sync']['pending_exports'],1)
        self.fake.export_batch=original
        with self.db.transaction() as conn:
            conn.execute(text('UPDATE outbox SET next_attempt=0 WHERE event_id=:e'),{'e':self.s.event_id})
        self.app.state.sync.run_once(force=True)
        self.assertEqual(self.service.dashboard()['sync']['pending_exports'],0)
        row=next(r for r in self.fake.rows() if r.get('participant_id')=='DEMO-P-pro')
        self.assertEqual(row.get('checkin_request_id'),result['request_id'])
        self.assertEqual(self.count('attendance'),2)

    def test_closed_event_and_live_development_tokens_fail_closed(self):
        with self.db.transaction() as conn:conn.execute(text('UPDATE events SET admissions_open=0 WHERE event_id=:e'),{'e':self.s.event_id})
        self.assertEqual(self.confirm().status_code,409)
        live=AdmissionService(self.db,replace(self.s,synthetic=False))
        self.assertEqual(live.lookup(token('lite'))['status'],'invalid')
        from event_checkin.web.service import eligibility
        row=self.fake.rows()[0]
        self.assertFalse(eligibility(row,self.fake.rows(),self.s.event_id,False)[0])


if __name__=='__main__':unittest.main()
