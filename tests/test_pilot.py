"""Pinned-pilot checks using fictional registrations and isolated temporary state."""
import io
import json
import smtplib
import tempfile
import unittest
from contextlib import redirect_stdout
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image

from event_checkin import pilot
from event_checkin.config import ROOT, ConfigurationError, Settings
from event_checkin.header_init import EVENT_ID
from event_checkin.mailer import SMTPMailer, EmailNotSent
from event_checkin.production import digest, preview, private_json
from event_checkin.production_sheets import APPROVAL_HEADERS, PRODUCTION_METADATA
from tests.test_production import Sheet, record


class Reader:
    live = True
    source_order = ['regular', 'discount_15', 'discount_10']

    def __init__(self):
        entries = [record(name='Synthetic Pilot ' + str(i), email='pilot' + str(i) + '@example.test',
                          tier='Regular (₱246)', _price_category='15_percent_discount', event_id=EVENT_ID)
                   for i in range(4)]
        self.sheet = Sheet(regular=[record(email='unselected@example.test', confirmation=False)], discount_15=entries)
        self.dataset = self.sheet.dataset
        self.reads = 0
        self.fail_reads = False

    def rows(self):
        self.reads += 1
        if self.fail_reads:
            raise RuntimeError('Synthetic read outage')
        return self.sheet.rows()

    def capture(self, source, number):
        row = next(r for r in self.rows() if r.source_id == source and r.number == number)
        return {'source': source, 'row': number, 'cells': {k: 22+i for i,k in enumerate(PRODUCTION_METADATA)},
                'values': deepcopy(row.data)}

    def write(self, snapshot, changes):
        if snapshot != self.capture(snapshot['source'], snapshot['row']):
            raise AssertionError('Concurrent synthetic edit')
        row = next(r for r in self.rows() if r.source_id == snapshot['source'] and r.number == snapshot['row'])
        self.sheet.update(row, changes)


class Renderer:
    def __init__(self, settings):
        self.settings, self.calls = settings, []

    def render(self, pid, name, token, ticket_tier=None):
        self.calls.append((pid, name, token, ticket_tier))
        path = self.settings.generated / pid / 'card.png'
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new('RGB', (16, 16), 'white').save(path)  # Synthetic MIME bytes only.
        return path


class FakeSMTP:
    def __init__(self):
        self.connections, self.messages, self.results = 0, [], []
        self.on_login = self.on_send = None

    def connect(self, *args, **kwargs):
        self.connections += 1
        return self

    def ehlo(self): pass
    def starttls(self, **kwargs): pass
    def login(self, user, password):
        if self.on_login: self.on_login()
    def close(self): pass

    def send_message(self, message):
        self.messages.append(message)
        if self.on_send: self.on_send()
        if self.results:
            outcome = self.results.pop(0)
            if outcome: raise outcome


class PilotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        (root / 'config').mkdir()
        self.s = Settings(root=root, profile='production', event_id=EVENT_ID,
                          runtime=root/'runtime-production', generated=root/'generated-production',
                          sources_file=root/'config/sources.json', template=ROOT/'templates/event-badge-final.coat',
                          qr_x=378, qr_y=228, qr_size=444, email_enabled=False,
                          smtp_from=pilot.SENDER, smtp_user=pilot.SENDER,
                          smtp_password='fictional-test-password', smtp_host='smtp.example.invalid')
        self.s.sources_file.write_text('{}')
        (root/'config/production-header-init.approved.json').write_text('{}')
        self.reader = Reader()
        self.plan = preview(self.s, self.reader, all_sources=True, limit=3)[1]
        self.reader.reads = 0
        self.path = root / pilot.PLAN_RELATIVE
        private_json(self.path, self.plan)
        self.original_plan = self.path.read_bytes()
        self.policy = {'version': 1, 'enabled': False, 'event_id': EVENT_ID, 'sender': pilot.SENDER,
                       'plan_sha256': pilot.checksum(self.path), 'entry_sha256': [digest(e) for e in self.plan['entries']],
                       'template_sha256': pilot.TEMPLATE_SHA256, 'manifest_sha256': pilot.checksum(self.s.sources_file),
                       'headers_sha256': pilot.checksum(root/'config/production-header-init.approved.json')}
        private_json(pilot.policy_path(self.s), self.policy)
        for name, value in [('PLAN_SHA256', self.policy['plan_sha256']), ('ENTRY_DIGESTS', tuple(self.policy['entry_sha256']))]:
            p = patch.object(pilot, name, value); p.start(); self.addCleanup(p.stop)
        self.renderer = Renderer(self.s)
        self.smtp = FakeSMTP()
        p = patch('event_checkin.mailer.smtplib.SMTP', side_effect=self.smtp.connect)
        p.start(); self.addCleanup(p.stop)
        p = patch('event_checkin.pilot.verify_render', return_value={'qr_decoded': True, 'name_pixels_verified': True, 'tier_pixels_verified': True})
        self.verify = p.start(); self.addCleanup(p.stop)
        self.initial = deepcopy(self.reader.sheet.data)

    def tearDown(self):
        # Tests never overwrite the selected plan or staff/form fields.
        self.assertEqual(self.path.read_bytes(), self.original_plan)
        for source, original in self.initial['sources'].items():
            for old, new in zip(original, self.reader.sheet.data['sources'][source]):
                for key in ('name', 'email', 'tier', '_price_category', *APPROVAL_HEADERS):
                    if key not in getattr(self, 'edited_fields', set()):
                        self.assertEqual(old.get(key), new.get(key))
        self.assertEqual(self.reader.sheet.data['sources']['regular'], self.initial['sources']['regular'])
        self.assertEqual(self.reader.sheet.data['sources']['discount_15'][3], self.initial['sources']['discount_15'][3])

    def enable(self):
        self.policy['enabled'] = True
        private_json(pilot.policy_path(self.s), self.policy)
        self.s.email_enabled = True  # Synthetic settings only, never production .env.

    def run_pilot(self, **kwargs):
        return pilot.execute(self.s, self.path, reader=self.reader, renderer=self.renderer,
                             write_transport=self.reader.write, reload_settings=lambda: self.s, **kwargs)

    def resolve(self, target, outcome):
        return pilot.resolve(self.s, self.path, target, outcome, 'synthetic provider-log review',
                             reader=self.reader, write_transport=self.reader.write, reload_settings=lambda: self.s)

    def target(self, index=0): return self.plan['entries'][index]['target']
    def row(self, index=0): return self.reader.sheet.data['sources']['discount_15'][index]
    def receipt(self, index=0): return pilot.existing(self.s, self.plan, self.plan['entries'][index])[1]

    def reserve_actions(self):
        journal = pilot.PilotJournal(self.s)
        journal.bind(self.plan)
        return journal, [journal.action(self.plan, entry) for entry in self.plan['entries']]

    def test_reserved_actions_are_unstarted_and_diagnosis_is_read_only(self):
        _, actions = self.reserve_actions()
        journal_path = self.s.runtime/'production-batches.sqlite3'
        before = journal_path.read_bytes(), deepcopy(self.reader.sheet.data)
        report = pilot.inspect_pilot(self.s, self.path, reader=self.reader)
        self.assertEqual([e['registration_phase'] for e in report['entries']], ['RESERVED']*3)
        self.assertTrue(all(e['eligibility_action'] == 'send' and not e['blockers'] and not e['retry_required']
                            for e in report['entries']))
        self.assertEqual((journal_path.read_bytes(), self.reader.sheet.data), before)
        self.assertFalse(self.renderer.calls)
        self.assertFalse(self.smtp.connections)
        self.enable(); self.run_pilot()
        self.assertEqual([str(m['Message-ID']) for m in self.smtp.messages], [a[1] for a in actions])
        self.assertEqual([self.row(i)['record_id'] for i in range(3)], [a[0] for a in actions])
        self.run_pilot()
        self.assertEqual(len(self.smtp.messages), 3)

    def test_interrupted_all_source_read_before_any_entry_is_processed(self):
        self.enable()
        original_rows = self.reader.rows
        def interrupt_after_reservation():
            if (self.s.runtime/'production-batches.sqlite3').exists():
                raise RuntimeError('Synthetic all-source read interruption')
            return original_rows()
        with patch.object(self.reader, 'rows', side_effect=interrupt_after_reservation):
            with self.assertRaises(RuntimeError): self.run_pilot()
        actions = [pilot.existing(self.s, self.plan, e)[0] for e in self.plan['entries']]
        self.assertTrue(all(actions))
        self.assertFalse(self.reader.sheet.attempts)
        report = pilot.inspect_pilot(self.s, self.path, reader=self.reader)
        self.assertEqual([e['registration_phase'] for e in report['entries']], ['RESERVED']*3)
        self.assertTrue(all(not e['blockers'] for e in report['entries']))
        self.run_pilot(); self.run_pilot()
        self.assertEqual([str(m['Message-ID']) for m in self.smtp.messages], [a[1] for a in actions])
        self.assertEqual(len(self.smtp.messages), 3)

    def test_interrupted_render_read_classifies_issued_and_later_reserved_entries(self):
        self.enable()
        original_render = self.renderer.render
        def interrupt_read(*args, **kwargs):
            path = original_render(*args, **kwargs)
            self.reader.fail_reads = True
            return path
        with patch.object(self.renderer, 'render', side_effect=interrupt_read):
            with self.assertRaises(RuntimeError): self.run_pilot()
        before = deepcopy(self.reader.sheet.data)
        actions = [pilot.existing(self.s, self.plan, e)[0] for e in self.plan['entries']]
        claim = pilot.existing(self.s, self.plan, self.plan['entries'][0])[2]
        self.assertEqual(self.row()['render_status'], 'RENDERING')
        self.assertFalse(self.smtp.messages)
        self.reader.fail_reads = False
        report = pilot.inspect_pilot(self.s, self.path, reader=self.reader)
        self.assertEqual([e['registration_phase'] for e in report['entries']], ['ISSUED', 'RESERVED', 'RESERVED'])
        self.assertTrue(all(not e['blockers'] and e['recovery'] == 'RESUME' for e in report['entries']))
        self.assertEqual(self.reader.sheet.data, before)
        self.run_pilot(); self.run_pilot()
        self.assertEqual((self.row()['participant_id'], self.row()['ticket_token']), claim[3:5])
        self.assertEqual([str(m['Message-ID']) for m in self.smtp.messages], [a[1] for a in actions])
        self.assertEqual(len(self.smtp.messages), 3)

    def test_partial_initialization_and_unpersisted_identity_claim_resume_without_retry(self):
        journal, actions = self.reserve_actions()
        for i in (0, 1):
            self.row(i).update(record_id=actions[i][0], source_id='discount_15', event_id=EVENT_ID,
                               price_category='15_percent_discount')
        row = next(r for r in self.reader.rows() if r.source_id == 'discount_15' and r.number == 3)
        identity = journal.claim(self.s, row)
        report = pilot.inspect_pilot(self.s, self.path, reader=self.reader)
        self.assertEqual([e['registration_phase'] for e in report['entries']], ['INITIALIZED', 'IDENTITY_RESERVED', 'RESERVED'])
        self.assertTrue(all(not e['blockers'] and e['eligibility_action'] == 'send' for e in report['entries']))
        self.enable(); self.run_pilot(); self.run_pilot()
        self.assertEqual((self.row(1)['participant_id'], self.row(1)['ticket_token']), identity)
        self.assertEqual([str(m['Message-ID']) for m in self.smtp.messages], [a[1] for a in actions])
        self.assertEqual(len(self.smtp.messages), 3)

    def test_reserved_entries_still_require_approvals_and_duplicate_review(self):
        self.reserve_actions()
        for key in APPROVAL_HEADERS:
            old = self.row()[key]; self.row()[key] = False
            report = pilot.inspect_pilot(self.s, self.path, reader=self.reader)
            self.assertIn('waiting for approval', str(report['entries'][0]['blockers']))
            self.row()[key] = old
        self.reader.sheet.data['sources']['discount_10'] = [record(
            email=self.row()['email'], name='Conflicting Synthetic Duplicate', event_id=EVENT_ID)]
        report = pilot.inspect_pilot(self.s, self.path, reader=self.reader)
        self.assertIn('conflicting approved', str(report['entries'][0]['blockers']))
        self.assertFalse(self.reader.sheet.attempts)

    def test_unverified_partial_tracking_and_foreign_reservations_cannot_resume(self):
        journal, _ = self.reserve_actions()
        self.row()['ticket_name'] = 'Unverified Synthetic Name'
        report = pilot.inspect_pilot(self.s, self.path, reader=self.reader)
        self.assertTrue(report['entries'][0]['blockers'])
        self.row().pop('ticket_name')
        row = next(r for r in self.reader.rows() if r.source_id == 'discount_15')
        row.data['record_id'] = 'R-other-synthetic-workflow'
        journal.claim(self.s, row)
        with self.assertRaises(ConfigurationError): pilot.inspect_pilot(self.s, self.path, reader=self.reader)
        self.assertFalse(self.reader.sheet.attempts)

    def test_explicit_retry_cannot_turn_reserved_entries_into_failed_jobs(self):
        self.reserve_actions(); self.enable()
        with self.assertRaises(ConfigurationError): self.run_pilot(retry_target=self.target())
        self.assertFalse(self.reader.sheet.attempts)
        self.assertFalse(self.renderer.calls)
        self.assertFalse(self.smtp.messages)

    def test_unpersisted_claim_must_match_bound_details_before_resume(self):
        journal, actions = self.reserve_actions()
        self.row().update(record_id=actions[0][0], source_id='discount_15', event_id=EVENT_ID,
                          price_category='15_percent_discount')
        row = next(r for r in self.reader.rows() if r.source_id == 'discount_15')
        journal.claim(self.s, row)
        with journal.db() as db:
            db.execute('UPDATE claims SET tier=? WHERE record_id=?', ('Pro', actions[0][0]))
        with self.assertRaises(ConfigurationError): pilot.inspect_pilot(self.s, self.path, reader=self.reader)
        self.enable()
        with self.assertRaises(ConfigurationError): self.run_pilot()
        self.assertFalse(self.reader.sheet.attempts)
        self.assertFalse(self.smtp.messages)

    def test_render_failure_requires_explicit_retry_in_diagnosis_and_execution(self):
        self.enable(); original = self.renderer.render
        calls = 0
        def fail_first(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1: raise RuntimeError('Synthetic rendering failure')
            return original(*args, **kwargs)
        with patch.object(self.renderer, 'render', side_effect=fail_first):
            self.assertEqual(self.run_pilot(), ['render error', 'sent', 'sent'])
        before = deepcopy(self.row())
        report = pilot.inspect_pilot(self.s, self.path, reader=self.reader)
        entry = report['entries'][0]
        self.assertTrue(entry['retry_required'])
        self.assertEqual(entry['eligibility_action'], 'retry')
        self.assertEqual(entry['recovery'], 'EXPLICIT_RETRY')
        self.assertIn('failed job requires explicit retry', entry['blockers'])
        self.assertEqual(self.run_pilot()[0], 'explicit retry required')
        self.assertEqual(len(self.smtp.messages), 2)
        self.run_pilot(retry_target=self.target()); self.run_pilot()
        self.assertEqual(len(self.smtp.messages), 3)
        for key in ('record_id', 'participant_id', 'ticket_token', 'ticket_name', 'ticket_tier'):
            self.assertEqual(self.row()[key], before[key])

    def test_silent_registration_write_failure_cannot_reserve_an_identity(self):
        self.enable(); original = self.reader.write
        def drop_registration(snapshot, changes):
            if 'record_id' not in changes: original(snapshot, changes)
        with patch.object(self.reader, 'write', side_effect=drop_registration):
            with self.assertRaises(ConfigurationError): self.run_pilot()
        self.assertTrue(all(pilot.existing(self.s, self.plan, e)[2] is None for e in self.plan['entries']))
        report = pilot.inspect_pilot(self.s, self.path, reader=self.reader)
        self.assertTrue(all(e['registration_phase'] == 'RESERVED' and not e['blockers'] for e in report['entries']))
        self.assertFalse(self.smtp.messages)

    def test_read_only_preflight_does_not_create_journals_or_issue(self):
        before = deepcopy(self.reader.sheet.data)
        report = pilot.inspect_pilot(self.s, self.path, reader=self.reader)
        self.assertEqual(report['selected_count'], 3)
        self.assertTrue(all(not e['blockers'] for e in report['entries']))
        self.assertFalse(report['authorization_enabled'])
        self.assertFalse(self.reader.sheet.attempts)
        self.assertEqual(before, self.reader.sheet.data)
        self.assertFalse((self.s.runtime/'production-batches.sqlite3').exists())
        self.assertFalse(self.renderer.calls)
        self.assertFalse(self.smtp.connections)

    def test_disabled_email_or_scope_blocks_before_reads_journals_render_and_smtp(self):
        for email, enabled in ((False, False), (False, True), (True, False)):
            self.s.email_enabled = email
            self.policy['enabled'] = enabled
            private_json(pilot.policy_path(self.s), self.policy)
            with self.assertRaises(ConfigurationError): self.run_pilot()
        self.assertFalse(self.reader.reads)
        self.assertFalse((self.s.runtime/'production-batches.sqlite3').exists())
        self.assertFalse(self.renderer.calls)
        self.assertFalse(self.smtp.connections)

    def test_plan_path_bytes_and_individual_entry_binding_are_exact(self):
        other = self.path.with_name('another.json'); other.write_bytes(self.original_plan)
        with self.assertRaises(ConfigurationError): pilot.load_bound(self.s, other)
        with patch.object(pilot, 'ENTRY_DIGESTS', ('wrong',)*3):
            with self.assertRaises(ConfigurationError): pilot.load_bound(self.s, self.path)
        with patch.object(pilot, 'PLAN_SHA256', '0'*64):
            with self.assertRaises(ConfigurationError): pilot.load_bound(self.s, self.path)

    def test_sender_mismatch_blocks_before_google_or_journal_creation(self):
        self.enable(); self.s.smtp_user = 'wrong@example.invalid'
        with self.assertRaises(ConfigurationError): self.run_pilot()
        self.assertFalse(self.reader.reads)
        self.assertFalse(self.smtp.connections)

    def test_each_approval_and_cancellation_change_blocks_before_issuance(self):
        self.enable()
        for key in APPROVAL_HEADERS:
            for bad in (False, '', 'FALSE', 1, 'true'):
                with self.subTest(key=key, value=bad):
                    self.row()[key] = bad
                    with self.assertRaises(Exception): self.run_pilot()
                    self.row()[key] = self.initial['sources']['discount_15'][0][key]
        self.row()['ticket_status'] = 'CANCELLED'
        with self.assertRaises(Exception): self.run_pilot()
        self.row().pop('ticket_status')
        self.assertFalse(self.reader.sheet.attempts)
        self.assertFalse(self.renderer.calls)
        self.assertFalse(self.smtp.connections)

    def test_changed_bound_registration_and_duplicate_block_without_substitution(self):
        self.enable(); self.edited_fields = {'name'}
        self.row()['name'] = 'Changed Synthetic'
        with self.assertRaises(ConfigurationError): self.run_pilot()
        self.row()['name'] = self.initial['sources']['discount_15'][0]['name']
        self.reader.sheet.data['sources']['discount_10'] = [record(email=self.row()['email'], name='Conflicting Synthetic', event_id=EVENT_ID)]
        with self.assertRaises(Exception): self.run_pilot()
        self.assertFalse(self.reader.sheet.attempts)
        self.assertFalse(self.smtp.messages)

    def test_three_frozen_messages_acceptance_receipts_and_idempotent_resume(self):
        self.enable()
        self.assertEqual(self.run_pilot(), ['sent']*3)
        self.assertEqual(self.smtp.connections, 3)
        self.assertEqual(self.verify.call_count, 3)
        for index, message in enumerate(self.smtp.messages):
            row = self.row(index)
            receipt = self.receipt(index)
            self.assertEqual(receipt[0], 'ACCEPTED')
            self.assertEqual(row['email_sent_at'], receipt[2])
            self.assertIn('inbox delivery not confirmed', row['last_error'])
            self.assertEqual(message['From'], pilot.SENDER)
            self.assertEqual(message['Message-ID'], row['message_id'])
            self.assertIn(row['ticket_name'], message.get_body(preferencelist=('plain',)).get_content())
            self.assertIn(row['ticket_tier'], message.get_body(preferencelist=('html',)).get_content())
            images = [p for p in message.walk() if p.get_content_type() == 'image/png']
            self.assertEqual(len(images), 2)
            self.assertEqual(images[0].get_payload(decode=True), images[1].get_payload(decode=True))
        before = deepcopy(self.reader.sheet.data)
        self.assertEqual(self.run_pilot(), ['completed previously']*3)
        self.assertEqual(self.reader.sheet.data, before)
        self.assertEqual(len(self.smtp.messages), 3)

    def test_approval_revoked_after_authentication_prevents_submission(self):
        self.enable(); self.edited_fields = {'payment_received'}
        self.smtp.on_login = lambda: self.row().__setitem__('payment_received', False)
        with self.assertRaises(ConfigurationError): self.run_pilot()
        self.assertEqual(self.receipt()[0], 'UNSENT')
        self.assertFalse(any(str(m['To']) == self.row()['email'] for m in self.smtp.messages))

    def test_email_disabled_during_authentication_prevents_data(self):
        self.enable()
        self.smtp.on_login = lambda: setattr(self.s, 'email_enabled', False)
        with self.assertRaises(ConfigurationError): self.run_pilot()
        self.assertFalse(self.smtp.messages)
        self.assertEqual(self.receipt()[0], 'UNSENT')

    def test_badge_verification_failure_prevents_submission(self):
        self.enable(); self.verify.side_effect = RuntimeError('Synthetic wrong QR')
        self.run_pilot()
        self.assertFalse(self.smtp.messages)
        self.assertTrue(all(self.receipt(i)[0] == 'UNSENT' for i in range(3)))

    def test_mime_tampering_is_rejected_against_the_frozen_message(self):
        class TamperMailer(SMTPMailer):
            calls = 0
            def build_message(self, *args, **kwargs):
                message = super().build_message(*args, **kwargs)
                self.calls += 1
                if self.calls % 2:
                    message.get_body(preferencelist=('plain',)).set_content('Wrong synthetic ticket')
                return message
        self.enable()
        self.run_pilot(mailer_factory=TamperMailer)
        self.assertFalse(self.smtp.messages)

    def test_new_duplicate_during_badge_verification_blocks_data(self):
        self.enable()
        def duplicate(*args):
            self.reader.sheet.data['sources']['discount_10'] = [record(
                email=self.row()['email'], name='New Synthetic Duplicate', event_id=EVENT_ID)]
        self.verify.side_effect = duplicate
        self.run_pilot()
        self.assertFalse(any(str(m['To']) == self.row()['email'] for m in self.smtp.messages))
        self.assertEqual(self.receipt()[0], 'UNSENT')

    def test_uncertain_delivery_requires_review_then_explicit_targeted_retry(self):
        self.enable(); self.smtp.results = [TimeoutError('Synthetic DATA disconnect'), None, None]
        outcomes = self.run_pilot()
        self.assertEqual(outcomes, ['review', 'sent', 'sent'])
        before = deepcopy(self.row())
        self.assertEqual(self.receipt()[0], 'UNCERTAIN')
        report = pilot.inspect_pilot(self.s, self.path, reader=self.reader)
        self.assertEqual(report['entries'][0]['recovery'], 'PROVIDER_REVIEW')
        self.assertIn('uncertain delivery requires staff review', report['entries'][0]['blockers'])
        with self.assertRaises(ConfigurationError): self.run_pilot(retry_target=self.target())
        self.assertIn('review', self.run_pilot()[0])
        self.assertEqual(len(self.smtp.messages), 3)
        self.s.email_enabled = False
        self.resolve(self.target(), 'not-sent')
        self.assertEqual(self.receipt()[0], 'RETRY_AUTHORIZED')
        self.s.email_enabled = True
        self.assertEqual(self.run_pilot(retry_target=self.target())[0], 'sent')
        self.assertEqual(len(self.smtp.messages), 4)
        for key in ('record_id', 'participant_id', 'ticket_token', 'ticket_name', 'ticket_tier', 'price_category', 'message_id'):
            self.assertEqual(self.row()[key], before[key])
        self.assertEqual(self.smtp.messages[0]['Message-ID'], self.smtp.messages[-1]['Message-ID'])

    def test_acceptance_is_durable_before_final_sheet_write_and_resume_never_resends(self):
        self.enable()
        self.reader.sheet.fail_before = lambda changes: changes.get('email_status') == 'SENT'
        with self.assertRaises(ConnectionError): self.run_pilot()
        self.assertEqual(self.receipt()[0], 'ACCEPTED')
        self.assertEqual(self.row()['email_status'], 'SENDING')
        self.assertEqual(len(self.smtp.messages), 1)
        self.assertEqual(self.run_pilot(), ['sent']*3)
        self.assertEqual(len(self.smtp.messages), 3)
        self.assertEqual(len(self.renderer.calls), 3)

    def test_lost_final_write_response_and_partial_completion_resume(self):
        self.enable()
        self.reader.sheet.fail_after = lambda changes: changes.get('email_status') == 'SENT'
        with self.assertRaises(ConnectionError): self.run_pilot()
        self.assertEqual(self.row()['email_status'], 'SENT')
        self.assertEqual(self.receipt()[0], 'ACCEPTED')
        self.assertEqual(self.run_pilot(), ['sent']*3)
        self.assertEqual(len(self.smtp.messages), 3)

    def test_uncertain_intent_write_never_automatically_submits(self):
        self.enable()
        self.reader.sheet.fail_before = lambda c: c.get('email_status') == 'SENDING'
        with self.assertRaises(ConnectionError): self.run_pilot()
        self.assertEqual(self.receipt()[0], 'INTENT')
        self.assertFalse(self.smtp.messages)
        old = deepcopy(self.row())
        self.run_pilot()  # The other two entries may complete; this one stays uncertain.
        self.assertEqual(len(self.smtp.messages), 2)
        self.s.email_enabled = False
        self.resolve(self.target(), 'not-sent')
        self.s.email_enabled = True
        self.run_pilot(retry_target=self.target())
        self.assertEqual(len(self.smtp.messages), 3)
        self.assertEqual(old['ticket_token'], self.row()['ticket_token'])
        self.assertEqual(old['participant_id'], self.row()['participant_id'])

    def test_reserved_identity_survives_failed_identity_sheet_persistence(self):
        self.enable()
        self.reader.sheet.fail_before = lambda c: 'participant_id' in c
        with self.assertRaises(ConnectionError): self.run_pilot()
        claim = pilot.existing(self.s, self.plan, self.plan['entries'][0])[2]
        self.assertTrue(claim)
        self.assertFalse(self.row().get('ticket_token'))
        self.run_pilot()
        self.assertEqual(claim[3:5], (self.row()['participant_id'], self.row()['ticket_token']))
        self.assertEqual(len(self.smtp.messages), 3)

    def test_failed_acceptance_receipt_is_uncertain_and_never_automatically_retried(self):
        self.enable()
        original = pilot.PilotJournal.record
        def fail_accept(journal, settings, plan, entry, row, status, **kwargs):
            # Even a wrongly typed callback error cannot turn an accepted SMTP
            # transaction into a known-unsent result that permits automatic retry.
            if status == 'ACCEPTED': raise EmailNotSent('Synthetic receipt failure')
            return original(journal, settings, plan, entry, row, status, **kwargs)
        with patch.object(pilot.PilotJournal, 'record', fail_accept):
            self.assertEqual(self.run_pilot(), ['review']*3)
        self.assertEqual(len(self.smtp.messages), 3)
        self.assertTrue(all(self.receipt(i)[0] == 'UNCERTAIN' for i in range(3)))
        self.run_pilot()
        self.assertEqual(len(self.smtp.messages), 3)

    def test_scope_disabled_during_authentication_blocks_submission(self):
        self.enable()
        def disable():
            self.policy['enabled'] = False
            private_json(pilot.policy_path(self.s), self.policy)
        self.smtp.on_login = disable
        with self.assertRaises(ConfigurationError): self.run_pilot()
        self.assertFalse(self.smtp.messages)
        self.assertEqual(self.receipt()[0], 'UNSENT')

    def test_uncertain_review_can_confirm_acceptance_without_smtp(self):
        self.enable(); self.smtp.results = [TimeoutError('Synthetic uncertain DATA'), None, None]
        self.run_pilot()
        self.s.email_enabled = False
        self.resolve(self.target(), 'accepted')
        self.assertEqual(self.receipt()[0], 'ACCEPTED')
        self.assertEqual(self.row()['email_status'], 'SENT')
        self.s.email_enabled = True
        self.run_pilot()
        self.assertEqual(len(self.smtp.messages), 3)

    def test_no_network_read_between_smtp_acceptance_and_durable_receipt(self):
        self.enable(); self.smtp.on_send = lambda: setattr(self.reader, 'fail_reads', True)
        with self.assertRaises(RuntimeError): self.run_pilot()
        self.assertEqual(self.receipt()[0], 'ACCEPTED')
        self.reader.fail_reads = False; self.smtp.on_send = None
        report = pilot.inspect_pilot(self.s, self.path, reader=self.reader)
        self.assertEqual([e['recovery'] for e in report['entries']], ['RECONCILE_ACCEPTANCE', 'RESUME', 'RESUME'])
        self.assertEqual([e['registration_phase'] for e in report['entries']], ['ISSUED', 'RESERVED', 'RESERVED'])
        actions = [pilot.existing(self.s, self.plan, e)[0] for e in self.plan['entries']]
        self.run_pilot()
        self.assertEqual([str(m['Message-ID']) for m in self.smtp.messages], [a[1] for a in actions])
        self.assertEqual(len(self.smtp.messages), 3)

    def test_silent_final_write_failure_cannot_mark_action_complete(self):
        self.enable()
        original = self.reader.write
        def drop_completion(snapshot, changes):
            if changes.get('email_status') != 'SENT':
                original(snapshot, changes)
        self.reader.write = drop_completion
        with self.assertRaises(ConfigurationError): self.run_pilot()
        self.assertEqual(self.receipt()[0], 'ACCEPTED')
        self.assertEqual(pilot.existing(self.s, self.plan, self.plan['entries'][0])[0][2], 0)
        self.reader.write = original
        self.run_pilot()
        self.assertEqual(len(self.smtp.messages), 3)

    def test_known_rejection_needs_explicit_retry_preserving_identity_and_mid(self):
        self.enable(); self.smtp.results = [smtplib.SMTPDataError(550, b'Synthetic refusal'), None, None]
        self.assertEqual(self.run_pilot(), ['email error', 'sent', 'sent'])
        before = deepcopy(self.row())
        report = pilot.inspect_pilot(self.s, self.path, reader=self.reader)
        self.assertTrue(report['entries'][0]['retry_required'])
        self.assertEqual(report['entries'][0]['recovery'], 'EXPLICIT_RETRY')
        self.assertIn('failed job requires explicit retry', report['entries'][0]['blockers'])
        self.assertEqual(self.run_pilot()[0], 'explicit retry required')
        self.run_pilot(retry_target=self.target())
        self.assertEqual(len(self.smtp.messages), 4)
        for key in ('participant_id', 'ticket_token', 'ticket_name', 'ticket_tier', 'message_id'):
            self.assertEqual(before[key], self.row()[key])
        self.assertEqual(len(self.renderer.calls), 3)

    def test_acceptance_can_be_reconciled_while_disabled_and_never_cleared(self):
        self.enable(); self.reader.sheet.fail_before = lambda c: c.get('email_status') == 'SENT'
        with self.assertRaises(ConnectionError): self.run_pilot()
        self.s.email_enabled = False; self.policy['enabled'] = False
        private_json(pilot.policy_path(self.s), self.policy)
        self.row()['ticket_status'] = 'CANCELLED'
        self.resolve(self.target(), 'accepted')
        self.assertEqual(self.row()['state'], 'CANCELLED')
        self.assertEqual(self.row()['ticket_status'], 'CANCELLED')
        with self.assertRaises(ConfigurationError): self.resolve(self.target(), 'not-sent')
        self.assertEqual(len(self.smtp.messages), 1)
        self.assertEqual(self.receipt()[0], 'ACCEPTED')

    def test_cli_requires_explicit_authorization_and_does_not_touch_adapters(self):
        args = SimpleNamespace(plan=self.path, outcome=None, evidence=None, resolve=None, retry=None,
                               preflight=False, diagnose=False, deactivate=False, activate=False, authorize_pilot=False)
        with patch.object(pilot, 'PilotReader') as reader, redirect_stdout(io.StringIO()):
            self.assertEqual(pilot.run_command(self.s, args), 1)
            reader.assert_not_called()
        with self.assertRaises(ConfigurationError): self.s.require_operational()

    def test_activation_is_exact_local_only_and_deactivation_survives_schema_drift(self):
        args = SimpleNamespace(plan=self.path, outcome=None, evidence=None, resolve=None, retry=None,
                               preflight=False, diagnose=False, deactivate=False, activate=True, authorize_pilot=True)
        with patch.object(pilot, 'PilotReader') as reader, redirect_stdout(io.StringIO()):
            self.assertEqual(pilot.run_command(self.s, args), 0)
            self.assertTrue(json.loads(pilot.policy_path(self.s).read_text())['enabled'])
            self.assertFalse(self.s.email_enabled)
            self.s.sources_file.write_text('{"synthetic_drift":true}')
            args.activate = False; args.deactivate = True
            self.assertEqual(pilot.run_command(self.s, args), 0)
            reader.assert_not_called()
        self.assertFalse(json.loads(pilot.policy_path(self.s).read_text())['enabled'])
        self.assertFalse(self.smtp.connections)

    def test_live_write_payload_is_selected_tracking_values_only(self):
        self.enable()
        scoped = pilot.PilotSettings(self.s, self.path, self.policy, reload_settings=lambda: self.s)
        self.reader.plan = {'sources': [{'source': {'id': 'discount_15', 'tab_id': 123, 'spreadsheet_id': 'synthetic-sheet'}}]}
        sheet = pilot.PilotSheet(scoped, self.plan, self.reader)
        sheet.http = Mock()
        rows = sheet.rows()
        selected = next(r for r in rows if (r.source_id, r.number) == ('discount_15', 2))
        sheet.update(selected, {'state': 'PENDING'})
        requests = sheet.http.batch_update.call_args[0][1]['requests']
        self.assertEqual(len(requests), 1)
        update = requests[0]['updateCells']
        self.assertEqual(update['fields'], 'userEnteredValue')
        self.assertEqual((update['range']['sheetId'], update['range']['startRowIndex'], update['range']['endRowIndex']), (123, 1, 2))
        self.assertEqual(update['rows'], [{'values': [{'userEnteredValue': {'stringValue': 'PENDING'}}]}])
        for changes in ({'confirmation': True}, {'name': 'Changed'}, {'checked_in_at': 'now'}):
            with self.assertRaises(ConfigurationError): sheet.update(selected, changes)
        unselected = next(r for r in rows if (r.source_id, r.number) == ('discount_15', 5))
        with self.assertRaises(ConfigurationError): sheet.update(unselected, {'state': 'PENDING'})
        self.assertEqual(sheet.http.batch_update.call_count, 1)


class PilotSchemaTests(unittest.TestCase):
    def test_all_three_snapshots_gate_reads_and_are_rechecked_afterwards(self):
        from event_checkin.reviewed_sheets import ReviewedSheets
        from event_checkin.sheets import SheetIntegrityError
        plan = {'sources': [{'source': {'id': s}} for s in ('regular', 'discount_15', 'discount_10')]}
        headers = SimpleNamespace(read_scheduler=SimpleNamespace(generation=0), read_reviewed=Mock())
        calls = []
        def snapshot(entry, data=True):
            calls.append((entry['source']['id'], data))
            return SimpleNamespace(header={'source': entry['source']['id']}, rows=[])
        headers.read_reviewed.side_effect = snapshot
        reader = ReviewedSheets(Settings(), plan, headers)
        self.assertEqual(reader.rows(), [])
        self.assertEqual(calls, [(s, data) for data in (False, True) for s in ('regular','discount_15','discount_10')])
        def drift(entry, data=True):
            result = snapshot(entry, data)
            if entry['source']['id'] == 'discount_10':
                if not data: raise SheetIntegrityError('Synthetic schema failure')
            return result
        calls.clear(); headers.read_reviewed.side_effect = drift
        with self.assertRaises(SheetIntegrityError): reader.rows()
        self.assertTrue(all(not data for _,data in calls))
        def moved(entry, data=True):
            result = snapshot(entry, data)
            if data: result.header = {'changed': True}
            return result
        headers.read_reviewed.side_effect = moved
        with self.assertRaises(SheetIntegrityError): reader.rows()


if __name__ == '__main__':
    unittest.main()
