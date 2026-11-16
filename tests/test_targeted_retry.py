"""Offline regression for Regular 99 and individual retries of 109–111.

All registrations, policies, Sheets transport, SMTP and journal mutations here
are synthetic and confined to the fixture's temporary directory.
"""
import json
import smtplib
import sqlite3
import unittest
from contextlib import closing
from copy import deepcopy
from unittest.mock import patch

from event_checkin import authorized_batch as batch, pilot
from event_checkin.config import ConfigurationError
from event_checkin.google_reads import ReadScheduler
from event_checkin.production import preview, private_json
from event_checkin.production_sheets import APPROVAL_HEADERS
from tests import test_pilot
from tests.sheet_transport import FakeClock, SheetsAPI
from tests.test_production import record


class TargetedRetryTests(unittest.TestCase):
    def setUp(self):
        self.f = test_pilot.PilotTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.s = self.f.s
        self.clock = FakeClock()
        self.api = SheetsAPI(self.f, self.clock)
        self.f.reader.dataset = self.f.plan['dataset']
        self.f.reader.sheet.data['sources']['regular'] = [
            record(name='Synthetic Regular '+str(n), email='regular%d@example.invalid' % n,
                   confirmation=n in (99, 109, 110, 111)) for n in range(2, 112)]
        self.plan = preview(self.s, self.f.reader, source='regular', start_row=99, end_row=111, limit='all')[1]
        self.path = self.s.runtime/'batch-reviews/batch-20261107-153238.json'
        private_json(self.path, self.plan)
        self.policy_path = batch.prepare(self.s, self.path)
        self.scope = batch.PlanAuthorization()
        self.writes = []
        self.enable(True)
        self.f.smtp.results = [None] + [smtplib.SMTPDataError(550, b'Synthetic refusal') for _ in range(3)]
        self.assertEqual(self.execute(), ['sent', 'email error', 'email error', 'email error'])
        self.row(99)['name'] = 'Changed Synthetic Regular 99'
        self.f.smtp.messages.clear()
        self.f.renderer.calls.clear()
        self.writes.clear()
        self.f.reader.sheet.attempts.clear()
        self.encoded = self.path.read_bytes()

    def enable(self, enabled):
        policy = json.loads(self.policy_path.read_text())
        policy['enabled'] = enabled
        private_json(self.policy_path, policy)
        self.s.email_enabled = enabled  # Temporary fictional settings only.

    def row(self, n):
        return self.f.reader.sheet.data['sources']['regular'][n-2]

    def write(self, snapshot, changes):
        self.writes.append((snapshot['source'], snapshot['row'], deepcopy(changes)))
        self.f.reader.write(snapshot, changes)

    def execute(self, n=None, **kwargs):
        return pilot.execute(self.s, self.path, retry_target='regular:row:'+str(n) if n else None,
            authorization=self.scope, reader=kwargs.pop('reader', self.f.reader), renderer=self.f.renderer,
            write_transport=self.write, reload_settings=lambda: self.s, **kwargs)

    def inspect(self, n=None):
        return pilot.inspect_pilot(self.s, self.path, reader=self.f.reader, authorization=self.scope,
                                   retry_target='regular:row:'+str(n) if n else None)

    def journal_state(self):
        with closing(sqlite3.connect(self.s.runtime/'production-batches.sqlite3')) as db:
            names = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            return {name: db.execute('SELECT * FROM '+name).fetchall() for name in names}

    def sql(self, statement):
        with sqlite3.connect(self.s.runtime/'production-batches.sqlite3') as db:
            db.execute(statement)

    def test_individual_retries_preserve_completed_peer_and_other_targets(self):
        for n in (109, 110, 111):
            with self.subTest(row=n):
                before = deepcopy(self.f.reader.sheet.data)
                journal = self.journal_state()
                policy = self.policy_path.read_bytes()
                identity = {k: self.row(n)[k] for k in
                            ('record_id', 'participant_id', 'ticket_token', 'message_id')}
                card = self.s.generated/self.row(n)['participant_id']/'card.png'
                card_bytes = card.read_bytes()
                self.writes.clear()
                messages = len(self.f.smtp.messages)
                outcome = self.execute(n)
                self.assertEqual(outcome[0], 'completed previously')
                self.assertEqual(outcome[(109, 110, 111).index(n)+1], 'sent')
                self.assertEqual(len(self.f.smtp.messages), messages+1)
                self.assertEqual(str(self.f.smtp.messages[-1]['To']), self.row(n)['email'])
                self.assertEqual(str(self.f.smtp.messages[-1]['Message-ID']), identity['message_id'])
                self.assertTrue(self.writes)
                self.assertTrue(all((source, number) == ('regular', n) for source, number, _ in self.writes))
                for source, rows in before['sources'].items():
                    for number, old in enumerate(rows, 2):
                        if (source, number) != ('regular', n):
                            self.assertEqual(self.f.reader.sheet.data['sources'][source][number-2], old)
                self.assertEqual({k: self.row(n)[k] for k in identity}, identity)
                self.assertEqual(card.read_bytes(), card_bytes)
                self.assertFalse(self.f.renderer.calls)
                self.assertEqual(self.path.read_bytes(), self.encoded)
                self.assertEqual(self.policy_path.read_bytes(), policy)
                after = self.journal_state()
                for table in ('plans', 'pilot_bindings', 'authorized_plan_bindings', 'claims'):
                    self.assertEqual(after[table], journal[table])
                for table, locator_index in (('actions', 1), ('pilot_delivery', 1), ('pilot_delivery_events', 2)):
                    peer = lambda rows: [r for r in rows if r[locator_index] == 'regular:row:99']
                    self.assertEqual(peer(after[table]), peer(journal[table]))

    def test_targeted_inspection_is_read_only_and_ordinary_inspection_stays_strict(self):
        self.enable(False)
        before = self.journal_state(), deepcopy(self.f.reader.sheet.data), self.policy_path.read_bytes()
        for n in (109, 110, 111):
            report = self.inspect(n)
            self.assertEqual(report['entries'][0]['recovery'], 'COMPLETED')
            self.assertEqual(next(e for e in report['entries'] if e['target'] == 'regular:row:'+str(n))['recovery'],
                             'EXPLICIT_RETRY')
            with self.assertRaises(ConfigurationError):
                self.execute(n)
        with self.assertRaisesRegex(ConfigurationError, 'changed or moved'):
            self.inspect()
        self.assertEqual((self.journal_state(), self.f.reader.sheet.data, self.policy_path.read_bytes()), before)
        self.assertFalse(self.writes)
        self.assertFalse(self.f.smtp.messages)

    def test_only_completed_non_targets_can_skip_stale_details(self):
        with self.assertRaises(ConfigurationError):
            self.execute(99)  # A completed entry never gets the exception as a target.
        self.row(110)['name'] = 'Changed Synthetic Unfinished Peer'
        with self.assertRaises(ConfigurationError):
            self.execute(109)
        self.assertFalse(self.writes)
        self.assertFalse(self.f.smtp.messages)

    def test_unverified_completed_peers_cannot_bypass_stale_details(self):
        mutations = [
            "UPDATE actions SET done=0 WHERE target='regular:row:99'",
            "UPDATE actions SET message_id='other' WHERE target='regular:row:99'",
            "UPDATE pilot_delivery SET status='UNSENT',accepted_at='' WHERE target='regular:row:99'",
            "DELETE FROM pilot_delivery WHERE target='regular:row:99'",
            "UPDATE pilot_delivery SET identity_hash='other' WHERE target='regular:row:99'",
            "UPDATE pilot_delivery SET attempts=0 WHERE target='regular:row:99'",
            "DELETE FROM pilot_delivery_events WHERE target='regular:row:99' AND status='ACCEPTED'",
            "UPDATE pilot_delivery SET accepted_at='invalid' WHERE target='regular:row:99'",
            "UPDATE claims SET name='other' WHERE record_id=(SELECT record_id FROM actions WHERE target='regular:row:99')",
            "UPDATE plans SET digest='other'",
            "UPDATE pilot_bindings SET digest='other'",
            "UPDATE authorized_plan_bindings SET authorization_digest='other'",
        ]
        path = self.s.runtime/'production-batches.sqlite3'
        original = path.read_bytes()
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                path.write_bytes(original)
                self.sql(mutation)
                journal = self.journal_state()
                with self.assertRaises(ConfigurationError):
                    self.execute(109)
                self.assertEqual(self.journal_state(), journal)
                self.assertFalse(self.writes)
                self.assertFalse(self.f.smtp.messages)
        path.write_bytes(original)

    def test_retry_targets_still_require_approvals_identity_and_known_unsent_state(self):
        for n in (109, 110, 111):
            for field, value in [(key, False) for key in APPROVAL_HEADERS] + [
                    ('name', 'Changed Synthetic Target'), ('participant_id', 'P-other'),
                    ('ticket_token', 'other'), ('ticket_name', 'other'), ('message_id', 'other'),
                    ('email_status', 'AMBIGUOUS'), ('ticket_status', 'CANCELLED')]:
                with self.subTest(row=n, field=field):
                    old = deepcopy(self.row(n))
                    self.row(n)[field] = value
                    journal = self.journal_state()
                    with self.assertRaises(ConfigurationError):
                        self.execute(n)
                    self.assertEqual(self.journal_state(), journal)
                    self.assertFalse(self.writes)
                    self.assertFalse(self.f.smtp.messages)
                    self.row(n).clear()
                    self.row(n).update(old)
        path = self.s.runtime/'production-batches.sqlite3'
        original = path.read_bytes()
        for status in ('INTENT', 'UNCERTAIN', 'ACCEPTED'):
            with self.subTest(receipt=status):
                self.sql("UPDATE pilot_delivery SET status='%s' WHERE target='regular:row:109'" % status)
                with self.assertRaises(ConfigurationError):
                    self.execute(109)
                self.assertFalse(self.writes)
                self.assertFalse(self.f.smtp.messages)
                path.write_bytes(original)

    def test_all_sources_still_supply_duplicate_and_schema_checks(self):
        self.f.reader.sheet.data['sources']['discount_10'].append(record(
            name='Conflicting Synthetic Attendee', email=self.row(109)['email'], _price_category='10_percent_discount'))
        with self.assertRaises(ConfigurationError):
            self.execute(109)
        self.assertFalse(self.writes)
        self.assertFalse(self.f.smtp.messages)
        self.f.reader.sheet.data['sources']['discount_10'].clear()
        # A changed completed peer remains in duplicate checks using its actual
        # current form fields, rather than a substituted historical snapshot.
        self.row(99)['email'] = self.row(109)['email']
        with self.assertRaises(ConfigurationError):
            self.execute(109)
        self.assertFalse(self.writes)
        self.assertFalse(self.f.smtp.messages)
        self.row(99)['email'] = 'regular99@example.invalid'
        self.api.entries[-1]['original_headers'][2] = 'Changed synthetic header'
        scheduler = ReadScheduler(clock=self.clock.monotonic, sleep=self.clock.sleep, progress=lambda _: None)
        with patch('gspread.service_account', side_effect=self.api.client):
            reader = pilot.PilotReader(self.s, read_scheduler=scheduler)
            with self.assertRaises(Exception):
                self.execute(109, reader=reader)
        self.assertEqual({e['source']['id'] for e in self.api.entries}, {'regular', 'discount_15', 'discount_10'})
        self.assertTrue(all(c['method'] == 'get' for c in self.api.calls))
        self.assertFalse(self.writes)
        self.assertFalse(self.f.smtp.messages)

    def test_typed_update_path_uses_the_same_retry_scope(self):
        def read_for_update(source, number):
            return self.f.reader.rows(), self.f.reader.capture(source, number)
        with patch.object(self.f.reader, 'read_for_update', side_effect=read_for_update, create=True):
            self.assertEqual(self.execute(109), ['completed previously', 'sent', 'outside explicit retry', 'outside explicit retry'])
        self.assertTrue(self.writes)
        self.assertTrue(all((source, number) == ('regular', 109) for source, number, _ in self.writes))

    def test_final_submission_rechecks_target_approval_with_stale_completed_peer(self):
        peer = deepcopy(self.row(99))
        receipt = [r for r in self.journal_state()['pilot_delivery'] if r[1] == 'regular:row:99']
        self.f.smtp.on_login = lambda: self.row(109).__setitem__('payment_received', False)
        with self.assertRaises(ConfigurationError):
            self.execute(109)
        self.assertFalse(self.f.smtp.messages)
        self.assertTrue(all((source, number) == ('regular', 109) for source, number, _ in self.writes))
        self.assertEqual(self.row(99), peer)
        self.assertEqual([r for r in self.journal_state()['pilot_delivery'] if r[1] == 'regular:row:99'], receipt)


if __name__ == '__main__':
    unittest.main()
