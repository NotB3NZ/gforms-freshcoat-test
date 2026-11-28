"""Render settings and startup tests: synthetic rows, temporary databases, no Google/SMTP."""
import base64
import hashlib
import json
import os
import tempfile
import unittest
import uuid
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

try:
    from fastapi.testclient import TestClient
    from sqlalchemy import event, text
    from event_checkin.web.adapters import FakeSheets, LiveSheets
    from event_checkin.web.app import create_app
    from event_checkin.web.db import Database, one
    from event_checkin.web.security import hash_password
    from event_checkin.web.seed import payload
    from event_checkin.web.service import AdmissionService, Conflict
    from event_checkin.web.settings import WebSettings
    WEB_AVAILABLE = True
except (ImportError, RuntimeError):
    WEB_AVAILABLE = False

PASSWORD = 'render-fixture-password-only'
HASH = hash_password(PASSWORD) if WEB_AVAILABLE else ''


@unittest.skipUnless(WEB_AVAILABLE, 'Install requirements-web.txt')
class RenderSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = {'RENDER': 'true', 'RENDER_EXTERNAL_URL': 'https://fixture.onrender.com',
                    'DATABASE_URL': 'postgres://fixture:fixture-password@db.invalid/checkin?sslmode=require',
                    'WEB_EVENT_ID': 'fixture-production-event', 'WEB_PASSWORD_HASH': HASH}

    def load(self, extra=None, remove=()):
        values = dict(self.env, **(extra or {}))
        for key in remove: values.pop(key, None)
        with patch.dict(os.environ, values, clear=True), patch('event_checkin.web.settings.ROOT', self.root):
            return WebSettings.load()

    def test_render_defaults_and_url_normalization(self):
        # A default local development file must not turn Render into a demo.
        (self.root / '.env.web').write_text('WEB_SYNTHETIC=true\nWEB_EVENT_ID=wrong-demo\n')
        s = self.load()
        self.assertEqual(s.mode, 'live')
        self.assertTrue(s.live_export)
        self.assertTrue(s.auto_open_admissions)
        self.assertTrue(s.secure_cookie)
        self.assertEqual(s.origin, self.env['RENDER_EXTERNAL_URL'])
        self.assertEqual(s.live_config, str(self.root))
        self.assertEqual(s.database_url, 'postgresql+psycopg://fixture:fixture-password@db.invalid/checkin?sslmode=require')
        for prefix in ['postgres://', 'postgresql://', 'postgresql+psycopg://']:
            self.assertTrue(self.load({'WEB_DATABASE_URL': prefix + 'user:password@db.invalid/other'}).database_url.startswith('postgresql+psycopg://'))

    def test_explicit_environment_and_selected_file_win(self):
        selected = self.root / 'web.env'
        selected.write_text('WEB_LIVE_EXPORT=false\nWEB_AUTO_OPEN_ADMISSIONS=false\nWEB_ORIGIN=https://file.example.invalid\n')
        s = self.load({'WEB_ENV_FILE': str(selected), 'WEB_ORIGIN': 'https://custom.example.invalid'})
        self.assertEqual(s.origin, 'https://custom.example.invalid')
        self.assertFalse(s.live_export)
        self.assertFalse(s.auto_open_admissions)
        self.assertEqual(s.mode, 'live')

    def test_local_demo_and_explicit_render_demo_remain_synthetic(self):
        for extra in [{'RENDER': 'false'}, {'WEB_SYNTHETIC': 'true'}]:
            with self.subTest(extra=extra):
                s = self.load(extra)
                self.assertEqual(s.mode, 'synthetic')
                self.assertTrue(s.database_url.startswith('sqlite:///'))
                self.assertEqual(s.origin, 'http://localhost:8080')
                self.assertFalse(s.live_export)
                self.assertFalse(s.auto_open_admissions)

    def test_preview_does_not_inherit_live_defaults_on_render(self):
        s = self.load({'WEB_READ_ONLY_PREVIEW': 'true', 'WEB_LIVE_CONFIG': str(self.root)})
        self.assertEqual(s.mode, 'preview')
        self.assertFalse(s.synthetic)
        self.assertFalse(s.live_export)
        self.assertFalse(s.auto_open_admissions)
        self.assertTrue(s.database_url.startswith('sqlite:///'))
        self.assertEqual(s.origin, 'http://localhost:8080')

    def test_missing_and_invalid_production_settings_fail_without_values(self):
        for field in ['DATABASE_URL', 'WEB_EVENT_ID', 'WEB_PASSWORD_HASH', 'RENDER_EXTERNAL_URL']:
            with self.subTest(field=field), self.assertRaises(ValueError): self.load(remove=[field])
        bad = [{'WEB_DATABASE_URL': 'sqlite:///demo.db'}, {'WEB_DATABASE_URL': 'postgresql:///missing-host'},
               {'WEB_DATABASE_URL': 'postgresql://user:secret@db.invalid:bad/database'},
               {'WEB_EVENT_ID': 'freshcoat-synthetic'}, {'WEB_EVENT_ID': 'REPLACE_WITH_EVENT'},
               {'WEB_PASSWORD_HASH': 'pbkdf2_sha256$invalid-secret'}, {'WEB_LIVE_CONFIG': ''},
               {'WEB_ORIGIN': 'http://public.example.invalid'}, {'WEB_ORIGIN': 'https://user:secret@example.invalid'},
               {'WEB_AUTO_OPEN_ADMISSIONS': 'yes'}, {'WEB_LIVE_EXPORT': 'TRUE'}]
        for extra in bad:
            with self.subTest(fields=list(extra)), self.assertRaises(ValueError) as error: self.load(extra)
            self.assertNotIn('invalid-secret', str(error.exception))
            self.assertNotIn('user:secret', str(error.exception))


@unittest.skipUnless(WEB_AVAILABLE, 'Install requirements-web.txt')
class RenderStartupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.local_url = 'sqlite:///' + str(self.root / 'live-test.db')
        # Settings validate as real production. Only the DB transport is redirected
        # to temporary SQLite so tests need no external database or network.
        self.s = WebSettings(database_url='postgresql+psycopg://fixture:password@db.invalid/checkin',
                             password_hash=HASH, origin='https://fixture.onrender.com',
                             event_id='fixture-production-event', synthetic=False, live_export=True,
                             auto_open_admissions=True, live_config=str(self.root), fixture=self.root / 'sheets.json')
        self.fake = FakeSheets(self.s)
        data = payload(self.s.event_id)
        for rows in data['sources'].values():
            for row in rows:
                if not row['ticket_token'].startswith('INVALID_TEST_'):
                    row['ticket_token'] = base64.urlsafe_b64encode(hashlib.sha256(row['participant_id'].encode()).digest()).decode().rstrip('=')
        self.fake.write(data)

    def factory(self, _url): return Database(self.local_url)

    def client(self, settings=None, source=None):
        return TestClient(create_app(settings or self.s, source or self.fake, background=False), base_url=self.s.origin)

    def snapshot(self):
        db = Database(self.local_url)
        try:
            with db.transaction() as conn:
                return {table: [dict(r) for r in conn.execute(text('SELECT * FROM ' + table)).mappings()]
                        for table in ['events', 'participants', 'attendance', 'requests', 'outbox']}
        finally: db.engine.dispose()

    def login(self, client):
        login = client.post('/api/login', json={'password': PASSWORD}, headers={'origin': self.s.origin})
        self.assertEqual(login.status_code, 200)
        headers = {'origin': self.s.origin, 'x-csrf-token': login.json()['csrf']}
        self.assertEqual(client.post('/api/nickname', json={'nickname': 'Production fixture staff'}, headers=headers).status_code, 200)
        return headers

    def test_first_start_reconciles_before_opening_and_restart_preserves_attendance_requests_outbox(self):
        with patch('event_checkin.web.app.Database', side_effect=self.factory), self.client() as client:
            dashboard = client.app.state.service.dashboard()
            self.assertTrue(dashboard['admissions_open'])
            self.assertEqual(dashboard['counts'], {'eligible': 7, 'arrived': 1, 'waiting': 6})
            headers = self.login(client)
            request_id = str(uuid.uuid4())
            body = {'participant_id': 'DEMO-P-lite', 'method': 'qr', 'request_id': request_id}
            result = client.post('/api/checkins', json=body, headers=headers).json()
            self.assertEqual(result['status'], 'accepted')
        before = self.snapshot()
        self.assertEqual(before['events'][0]['admissions_initialized'], 1)
        with patch('event_checkin.web.app.Database', side_effect=self.factory), self.client() as client:
            headers = self.login(client)
            self.assertEqual(client.get('/api/requests/' + request_id).json()['result'], result)
            duplicate = client.post('/api/checkins', json=dict(body, request_id=str(uuid.uuid4())), headers=headers).json()
            self.assertEqual(duplicate['status'], 'duplicate')
            self.assertEqual(duplicate['participant']['checked_in_at'], result['participant']['checked_in_at'])
            self.assertEqual(duplicate['participant']['checked_in_by'], 'Production fixture staff')
            client.app.state.sync.run_once(force=True)
        after = self.snapshot()
        self.assertEqual(len(after['participants']), len(before['participants']))
        self.assertEqual(after['attendance'], before['attendance'])
        self.assertEqual(len(after['outbox']), 1)
        self.assertIsNotNone(after['outbox'][0]['delivered_at'])
        exported = next(r for r in self.fake.rows() if r.get('participant_id') == 'DEMO-P-lite')
        self.assertEqual(exported.get('checkin_request_id'), request_id)
        self.assertEqual(exported.get('checked_in_by'), 'Production fixture staff')
        self.assertEqual(exported.get('email_status'), 'SENT')

    def test_explicit_closed_start_and_later_closure_survive_restarts(self):
        with patch('event_checkin.web.app.Database', side_effect=self.factory), self.client(replace(self.s, auto_open_admissions=False)) as client:
            self.assertFalse(client.app.state.service.dashboard()['admissions_open'])
            client.app.state.service.open_admissions()
            with client.app.state.db.transaction() as conn:
                conn.execute(text('UPDATE events SET admissions_open=0'))
        for _ in range(2):
            with patch('event_checkin.web.app.Database', side_effect=self.factory), self.client() as client:
                self.assertFalse(client.app.state.service.dashboard()['admissions_open'])
                self.assertEqual(client.app.state.service.dashboard()['counts']['arrived'], 1)

    def test_failed_initial_import_never_opens_and_is_safe_to_retry(self):
        original = self.fake.read_payload()
        broken = deepcopy(original)
        broken['sources']['regular'][1]['ticket_token'] = broken['sources']['regular'][0]['ticket_token']
        self.fake.write(broken)
        with patch('event_checkin.web.app.Database', side_effect=self.factory), self.assertRaisesRegex(RuntimeError, 'reconciliation failed'):
            with self.client(): pass
        state = self.snapshot()
        self.assertEqual(state['events'][0]['admissions_open'], 0)
        self.assertEqual(state['events'][0]['admissions_initialized'], 0)
        self.assertFalse(state['participants'])
        self.fake.write(original)
        with patch('event_checkin.web.app.Database', side_effect=self.factory), self.client() as client:
            self.assertTrue(client.app.state.service.dashboard()['admissions_open'])

    def test_restart_reconciliation_conflict_keeps_original_attendance_and_state(self):
        with patch('event_checkin.web.app.Database', side_effect=self.factory), self.client(): pass
        before = self.snapshot()
        data = self.fake.read_payload()
        for row in data['sources']['regular']:
            if row['participant_id'] == 'DEMO-P-arrived': row['checked_in_by'] = 'Conflicting legacy writer'
        self.fake.write(data)
        with patch('event_checkin.web.app.Database', side_effect=self.factory), self.assertRaisesRegex(RuntimeError, 'reconciliation failed'):
            with self.client(): pass
        self.assertEqual(self.snapshot(), before)

    def test_no_matching_event_or_empty_source_cannot_open(self):
        for data in [dict(payload('different-event')), dict(payload(self.s.event_id), sources={key: [] for key in self.fake.read_payload()['sources']})]:
            source = Mock()
            from event_checkin.production_sheets import FixtureSheets
            source.rows.return_value = FixtureSheets(data).rows()
            with patch('event_checkin.web.app.Database', side_effect=self.factory), self.assertRaisesRegex(RuntimeError, 'reconciliation failed'):
                with self.client(source=source): pass
            self.assertEqual(self.snapshot()['events'][0]['admissions_open'], 0)

    def test_database_and_google_failures_are_redacted_and_do_not_start_worker(self):
        secret = 'private-password-or-registration-content'
        with patch('event_checkin.web.app.Database', side_effect=RuntimeError(secret)), self.assertRaisesRegex(RuntimeError, 'database initialization') as error:
            with self.client(): pass
        self.assertNotIn(secret, str(error.exception))
        source = Mock()
        source.rows.side_effect = ConnectionError(secret)
        with patch('event_checkin.web.app.Database', side_effect=self.factory), patch('event_checkin.web.app.Synchronizer.start') as worker, self.assertRaises(RuntimeError) as error:
            with self.client(source=source): pass
        worker.assert_not_called()
        self.assertNotIn(secret, str(error.exception))

    def test_import_and_open_roll_back_together(self):
        db = self.factory(None)
        db.migrate(self.s)
        def fail_open(conn, cursor, statement, params, context, many):
            if statement.startswith('UPDATE events SET admissions_open=1,admissions_initialized=1'):
                raise RuntimeError('Synthetic failure before admission-state commit')
        event.listen(db.engine, 'before_cursor_execute', fail_open)
        try:
            with self.assertRaises(RuntimeError):
                AdmissionService(db, self.s).import_rows(self.fake.rows(), initialize_admissions=True)
        finally:
            event.remove(db.engine, 'before_cursor_execute', fail_open)
            db.engine.dispose()
        state = self.snapshot()
        self.assertFalse(state['participants'])
        self.assertFalse(state['attendance'])
        self.assertEqual(state['events'][0]['admissions_open'], 0)

    def test_live_adapter_strips_sending_settings_without_editing_profile_or_gates(self):
        production = self.root / '.env.production'
        original = 'EVENT_ID=fixture-production-event\nEMAIL_ENABLED=true\nSMTP_PASSWORD=fixture-smtp-secret\n'
        production.write_text(original)
        with patch('event_checkin.web.adapters.ProductionSheets') as sheets:
            reader = LiveSheets(self.s)
        self.assertFalse(reader.legacy.email_enabled)
        self.assertEqual(reader.legacy.smtp_password, '')
        sheets.assert_called_once_with(reader.legacy, writable=False)
        self.assertEqual(production.read_text(), original)
        from event_checkin.config import ConfigurationError, load_settings
        with self.assertRaises(ConfigurationError): reader.legacy.require_email()
        with self.assertRaises(ConfigurationError): load_settings('production', self.root)
        self.assertTrue(load_settings('production', self.root, authorized_batch=True).email_enabled)

    def test_explicit_export_disable_retains_outbox_without_calling_writer(self):
        self.fake.export_batch = Mock(side_effect=AssertionError('Exports explicitly disabled'))
        with patch('event_checkin.web.app.Database', side_effect=self.factory), self.client(replace(self.s, live_export=False)) as client:
            headers = self.login(client)
            response = client.post('/api/checkins', headers=headers, json={'participant_id': 'DEMO-P-lite', 'method': 'manual', 'request_id': str(uuid.uuid4())})
            self.assertEqual(response.json()['status'], 'accepted')
            client.app.state.sync.run_once(force=True)
            self.assertEqual(client.app.state.service.dashboard()['sync']['pending_exports'], 1)
        self.fake.export_batch.assert_not_called()
