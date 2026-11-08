"""Reusable plan scopes: synthetic Sheets/SMTP, temporary state and fake clocks."""
import argparse
import io
import json
import sqlite3
import unittest
import uuid
from contextlib import closing, redirect_stdout
from copy import deepcopy
from unittest.mock import patch

from event_checkin import authorized_batch as batch, pilot, pilot_resend
from event_checkin.config import ConfigurationError
from event_checkin.google_reads import ReadScheduler
from event_checkin.production import preview, private_json, reasons
from event_checkin.shared_email import approvals_path, review_members, save_approval
from event_checkin.production_cli import register, run as run_cli
from tests import test_pilot
from tests.sheet_transport import FakeClock, SheetsAPI, quota_error
from tests.test_production import record


class AuthorizedBatchTests(unittest.TestCase):
    def setUp(self):
        self.f = test_pilot.PilotTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.clock = FakeClock()
        self.api = SheetsAPI(self.f, self.clock)
        self.f.reader.dataset = self.f.plan['dataset']
        self.f.reader.sheet.data['sources']['discount_15'].extend(
            record(name='Synthetic Selected '+str(i), email='selected%d@example.invalid' % i,
                   tier='Regular (₱246)', _price_category='15_percent_discount') for i in range(6, 12))
        self.s = self.f.s
        p = patch.object(pilot, 'PLAN_SHA256', self.f.policy['plan_sha256'])
        p.start()
        self.addCleanup(p.stop)
        self.plan = preview(self.s, self.f.reader, source='discount_15', start_row=5, end_row=9, limit=5)[1]
        self.path = self.s.runtime/'batch-reviews/five-selected.json'
        private_json(self.path, self.plan)
        self.encoded = self.path.read_bytes()
        self.policy_path = batch.prepare(self.s, self.path)
        self.scope = batch.PlanAuthorization()
        self.f.reader.reads = 0
        self.original = deepcopy(self.f.reader.sheet.data)

    def policy(self):
        return json.loads(self.policy_path.read_text())

    def enable(self):
        policy = self.policy()
        policy['enabled'] = True
        private_json(self.policy_path, policy)
        self.s.email_enabled = True  # Isolated synthetic settings only.

    def disable(self):
        policy = self.policy()
        policy['enabled'] = False
        private_json(self.policy_path, policy)
        self.s.email_enabled = False

    def execute(self, **kwargs):
        return pilot.execute(self.s, self.path, authorization=self.scope, reader=self.f.reader,
            renderer=self.f.renderer, write_transport=self.f.reader.write, reload_settings=lambda: self.s, **kwargs)

    def inspect(self):
        return pilot.inspect_pilot(self.s, self.path, reader=self.f.reader, authorization=self.scope)

    def row(self, index=0):
        return self.f.reader.sheet.data['sources']['discount_15'][3+index]

    def receipt(self, index=0):
        return pilot.existing(self.s, self.plan, self.plan['entries'][index])[1]

    def resolve(self, outcome):
        return pilot.resolve(self.s, self.path, self.plan['entries'][0]['target'], outcome,
            'Synthetic provider evidence', authorization=self.scope, reader=self.f.reader,
            write_transport=self.f.reader.write, reload_settings=lambda: self.s)

    def args(self, *flags):
        parser = argparse.ArgumentParser()
        commands = parser.add_subparsers(dest='command')
        register(commands)
        return parser.parse_args(['batch-run', '--authorized-plan', '--plan', str(self.path), *flags])

    def test_prepare_and_preflight_are_disabled_and_read_only(self):
        self.assertFalse(self.policy()['enabled'])
        self.assertEqual(self.policy_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(len(self.policy()['entry_sha256']), 5)
        before = deepcopy(self.f.reader.sheet.data), self.policy_path.read_bytes(), self.path.read_bytes()
        with patch('gspread.service_account', side_effect=AssertionError('Injected reader only')):
            report = self.inspect()
            self.assertEqual(batch.prepare(self.s, self.path), self.policy_path)
        self.assertEqual(report['selected_count'], 5)
        self.assertTrue(all(e['recovery'] == 'RESUME' for e in report['entries']))
        self.assertEqual((self.f.reader.sheet.data, self.policy_path.read_bytes(), self.path.read_bytes()), before)
        self.assertFalse((self.s.runtime/'production-batches.sqlite3').exists())
        self.assertFalse(self.f.smtp.connections)
        self.assertFalse(self.f.renderer.calls)

    def test_authorized_batch_uses_verified_group_for_preflight_send_and_resume(self):
        # Start from raw fixture rows, save an explicit private approval, then
        # exercise the authorized path (which receives unannotated source reads).
        source_rows = self.f.reader.sheet.data['sources']['discount_15']
        source_rows[3]['email'] = 'authorized-shared@example.invalid'
        source_rows[4]['email'] = 'authorized-shared@example.invalid'
        third = record(name='Synthetic unapproved attendee', email='authorized-shared@example.invalid',
                       tier='Regular (₱246)', _price_category='15_percent_discount',
                       confirmation=False)
        source_rows.append(third)
        self.edited_fields = {'email'}
        review, review_hash, _ = review_members(
            self.s, self.f.reader.rows(), 'discount_15', [5, 6])
        save_approval(self.s, review, review_hash,
                      confirm_distinct_attendees=True, confirm_payment_coverage=True)
        _, plan = preview(self.s, self.f.reader,
                          ids=['discount_15:row:5', 'discount_15:row:6'], limit='all',
                          selection_version=2)
        self.path = self.s.runtime/'batch-reviews/shared-email-authorized.json'
        private_json(self.path, plan)
        self.plan = plan
        self.encoded = self.path.read_bytes()
        self.original_plan = self.encoded
        self.policy_path = batch.prepare(self.s, self.path)
        self.scope = batch.PlanAuthorization()
        self.assertEqual([entry['snapshot_row'] for entry in self.plan['entries']], [5, 6])

        # Inspection deliberately reads the underlying reader's raw rows.
        before = self.inspect()
        self.assertTrue(all(not entry['blockers'] for entry in before['entries']))
        preview_rows = pilot.verified_rows(self.s, self.f.reader.rows())
        third_row = next(row for row in preview_rows if row.number == 12)
        self.assertTrue(reasons(third_row, preview_rows, self.s.event_id))

        self.enable()
        self.f.smtp.results = [None]
        first = self.execute(first_sends_only=True)
        self.assertEqual(first, ['outside remaining first sends; original attempt retained', 'sent'])
        self.assertEqual(self.row(1)['email_status'], 'SENT')

        # The remaining attendee can send after the first accepted ticket. Lose
        # the final Sheet completion write to exercise receipt-based recovery.
        original_write = self.f.reader.write
        def interrupt_completion(snapshot, changes):
            if changes.get('email_status') == 'SENT':
                raise ConnectionError('Synthetic interruption after SMTP acceptance')
            return original_write(snapshot, changes)
        self.f.reader.write = interrupt_completion
        self.f.smtp.results = [None]
        try:
            with self.assertRaises(ConnectionError):
                self.execute()
        finally:
            self.f.reader.write = original_write
        self.assertEqual(len(self.f.smtp.messages), 2)
        resumed = self.execute()
        self.assertEqual(resumed, ['sent', 'completed previously'])
        rows = [self.row(0), self.row(1)]
        self.assertEqual([row['email_status'] for row in rows], ['SENT', 'SENT'])
        self.assertEqual(len({row['participant_id'] for row in rows}), 2)
        self.assertEqual(len({row['ticket_token'] for row in rows}), 2)
        self.assertEqual(len({row['message_id'] for row in rows}), 2)
        self.assertEqual(len({message['Message-ID'] for message in self.f.smtp.messages}), 2)
        self.assertEqual(len({message['To'] for message in self.f.smtp.messages}), 1)
        self.assertTrue(all(not any(key.startswith('_shared_email_') for key in change)
                            for change in self.f.reader.sheet.attempts))
        after = self.inspect()
        self.assertTrue(all(not [blocker for blocker in entry['blockers']
                                 if blocker != 'already sent; explicit resend required']
                            for entry in after['entries']), after['entries'])

        # Revoking approval blocks the same-email issued group. A changed detail
        # also invalidates the saved plan and cannot inherit the prior binding.
        private_json(approvals_path(self.s), {'version': 1, 'event_id': self.s.event_id, 'groups': []})
        revoked = self.inspect()
        self.assertTrue(all(entry['blockers'] for entry in revoked['entries']))
        source_rows[3]['name'] = 'Changed synthetic attendee'
        self.edited_fields.add('name')
        with self.assertRaisesRegex(Exception, 'changed or moved'):
            self.inspect()

    def test_execution_requires_both_gates_before_any_io(self):
        for email, authorized in ((False, False), (True, False), (False, True)):
            with self.subTest(email=email, authorized=authorized):
                p = self.policy()
                p['enabled'] = authorized
                private_json(self.policy_path, p)
                self.s.email_enabled = email
                with self.assertRaises(ConfigurationError): self.execute()
        self.assertFalse(self.f.reader.reads)
        self.assertFalse(self.f.smtp.connections)
        self.assertFalse(self.f.renderer.calls)
        self.assertFalse((self.s.runtime/'production-batches.sqlite3').exists())

    def test_cli_explicit_authority_and_scope_switches_are_required(self):
        self.enable()
        with redirect_stdout(io.StringIO()), patch.object(pilot, 'PilotReader', side_effect=AssertionError('No reads')):
            self.assertEqual(run_cli(self.s, self.args()), 1)
        for flags in (['--pilot'], ['--fixture', 'fictional.json'], ['--authorize-pilot'], ['--remaining-first-sends']):
            with self.subTest(flags=flags):
                with redirect_stdout(io.StringIO()):
                    if flags[0] in ('--pilot', '--fixture'):
                        with self.assertRaises(ConfigurationError): run_cli(self.s, self.args(*flags))
                    else:
                        self.assertEqual(run_cli(self.s, self.args(*flags)), 1)
        with self.assertRaises(ConfigurationError): self.s.require_operational()
        self.assertFalse(self.f.smtp.connections)

    def test_cli_preflight_activation_and_deactivation_do_not_send(self):
        output = io.StringIO()
        with redirect_stdout(output), patch.object(pilot, 'PilotReader', return_value=self.f.reader):
            self.assertEqual(run_cli(self.s, self.args('--preflight')), 0)
        self.assertEqual(json.loads(output.getvalue())['selected_count'], 5)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(run_cli(self.s, self.args('--activate')), 1)
            self.assertEqual(run_cli(self.s, self.args('--activate', '--authorize-plan')), 0)
        self.assertTrue(self.policy()['enabled'])
        self.assertFalse(self.s.email_enabled)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(run_cli(self.s, self.args('--deactivate')), 0)
        self.assertFalse(self.policy()['enabled'])
        self.assertFalse(self.f.smtp.connections)
        self.assertFalse((self.s.runtime/'production-batches.sqlite3').exists())

    def test_plan_bytes_path_and_selections_cannot_be_changed_or_rebound(self):
        for change in ('whitespace', 'fingerprint', 'extra-entry', 'event', 'action'):
            with self.subTest(change=change):
                plan = deepcopy(self.plan)
                if change == 'fingerprint': plan['entries'][0]['fingerprint'] = '0'*64
                elif change == 'extra-entry': plan['entries'].append(deepcopy(plan['entries'][0]))
                elif change == 'event': plan['event_id'] = 'other-event'
                elif change == 'action': plan['action'] = 'resend'
                if change == 'whitespace': self.path.write_bytes(self.encoded+b'\n')
                else: private_json(self.path, plan)
                with self.assertRaises((ConfigurationError, ValueError)):
                    self.scope.load_bound(self.s, self.path)
                self.path.write_bytes(self.encoded)
        copy = self.path.with_name('copied-plan.json')
        copy.write_bytes(self.encoded)
        with self.assertRaises(ConfigurationError): self.scope.load_bound(self.s, copy)
        with self.assertRaises(ConfigurationError): batch.prepare(self.s, copy)

    def test_configuration_sender_assets_and_other_plans_are_not_authorized(self):
        for key, value in (('smtp_user', 'other@example.invalid'), ('smtp_host', 'changed.invalid'),
                           ('qr_x', 379), ('timezone', 'UTC'), ('generated', self.s.root/'other-cards')):
            with self.subTest(key=key):
                old = getattr(self.s, key)
                setattr(self.s, key, value)
                with self.assertRaises((ConfigurationError, ValueError)): self.scope.load_bound(self.s, self.path)
                setattr(self.s, key, old)
        other = deepcopy(self.plan)
        other['plan_id'] = str(uuid.uuid4())
        path = self.path.with_name('unrelated.json')
        private_json(path, other)
        with self.assertRaises(FileNotFoundError): self.scope.load_bound(self.s, path)
        self.assertFalse(self.f.smtp.connections)

    def test_email_asset_drift_requires_review_without_touching_the_real_template(self):
        copy = self.s.root/'changed-synthetic-email.html'
        copy.write_text('<html><body>Different synthetic content</body></html>')
        with patch.object(batch, 'EMAIL_TEMPLATE', copy):
            with self.assertRaises(ConfigurationError): self.scope.load_bound(self.s, self.path)
        self.assertFalse((self.s.runtime/'production-batches.sqlite3').exists())

    def test_exact_selected_scope_completed_skip_and_normal_preview_recognizes_receipts(self):
        self.enable()
        self.assertEqual(self.execute(), ['sent']*5)
        self.assertEqual(len(self.f.smtp.messages), 5)
        for i, message in enumerate(self.f.smtp.messages):
            self.assertEqual(str(message['To']), self.row(i)['email'])
            self.assertEqual(str(message['Message-ID']), self.row(i)['message_id'])
            self.assertEqual(self.receipt(i)[0], 'ACCEPTED')
        for index, row in enumerate(self.f.reader.sheet.data['sources']['discount_15']):
            if not 3 <= index <= 7:
                self.assertEqual(row, self.original['sources']['discount_15'][index])
        self.assertEqual(self.f.reader.sheet.data['sources']['regular'], self.original['sources']['regular'])
        before = deepcopy(self.f.reader.sheet.data)
        self.assertEqual(self.execute(), ['completed previously']*5)
        self.assertEqual(self.f.reader.sheet.data, before)
        self.assertEqual(len(self.f.smtp.messages), 5)
        report, plan, summary = preview(self.s, self.f.reader, source='discount_15', include_summary=True)
        self.assertEqual(summary['discount_15']['already_sent_registrations'], 5)
        self.assertTrue(all(r['sent'] for r in report if 5 <= r['row'] <= 9))
        self.assertTrue(all(e['snapshot_row'] not in range(5, 10) for e in plan['entries']))
        self.assertEqual(self.path.read_bytes(), self.encoded)

    def test_stale_registration_and_revoked_approval_prevent_first_write(self):
        self.enable()
        self.row()['email'] = 'changed@example.invalid'
        with self.assertRaises(ConfigurationError): self.execute()
        self.row()['email'] = self.original['sources']['discount_15'][3]['email']
        self.row()['payment_received'] = False
        with self.assertRaises(ConfigurationError): self.execute()
        self.assertFalse((self.s.runtime/'production-batches.sqlite3').exists())
        self.assertFalse(self.f.renderer.calls)
        self.assertFalse(self.f.smtp.messages)

    def test_authorization_revocation_during_authentication_prevents_data(self):
        self.enable()
        self.f.smtp.on_login = self.disable
        with self.assertRaises(ConfigurationError): self.execute()
        self.assertFalse(self.f.smtp.messages)
        self.assertEqual(self.receipt()[0], 'UNSENT')

    def test_partial_completion_resumes_with_same_reserved_ids_and_no_duplicate_send(self):
        self.enable()
        self.f.reader.sheet.fail_before = lambda changes: bool(changes.get('record_id') and self.receipt() and self.receipt()[0] == 'ACCEPTED')
        with self.assertRaises(ConnectionError): self.execute()
        self.assertEqual(len(self.f.smtp.messages), 1)
        with closing(sqlite3.connect(self.s.runtime/'production-batches.sqlite3')) as db:
            actions = db.execute('SELECT target,record_id,message_id FROM actions ORDER BY target').fetchall()
        self.assertEqual(self.execute(), ['completed previously', 'sent', 'sent', 'sent', 'sent'])
        with closing(sqlite3.connect(self.s.runtime/'production-batches.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT target,record_id,message_id FROM actions ORDER BY target').fetchall(), actions)
        self.assertEqual(len(self.f.smtp.messages), 5)

    def test_accepted_receipt_reconciles_lost_tracking_without_resubmission(self):
        self.enable()
        self.f.reader.sheet.fail_before = lambda c: c.get('email_status') == 'SENT'
        with self.assertRaises(ConnectionError): self.execute()
        receipt = self.receipt()
        self.assertEqual(receipt[0], 'ACCEPTED')
        identity = {k: self.row()[k] for k in ('participant_id', 'ticket_token', 'record_id', 'message_id')}
        self.disable()
        self.resolve('accepted')
        self.assertEqual(self.receipt(), receipt)
        self.assertEqual(len(self.f.smtp.messages), 1)
        self.assertEqual({k: self.row()[k] for k in identity}, identity)
        self.enable()
        self.assertEqual(self.execute(), ['completed previously', 'sent', 'sent', 'sent', 'sent'])
        self.assertEqual(len(self.f.smtp.messages), 5)

    def test_uncertain_requires_provider_review_then_explicit_targeted_retry(self):
        self.enable()
        self.f.smtp.results = [TimeoutError('Synthetic DATA disconnect')]
        self.assertEqual(self.execute(), ['review', 'sent', 'sent', 'sent', 'sent'])
        mid = self.row()['message_id']
        self.assertEqual(self.receipt()[0], 'UNCERTAIN')
        self.assertIn('review', self.execute()[0])
        with self.assertRaises(ConfigurationError): self.execute(retry_target=self.plan['entries'][0]['target'])
        self.assertEqual(len(self.f.smtp.messages), 5)
        self.disable()
        self.resolve('not-sent')
        self.enable()
        self.assertEqual(self.execute(retry_target=self.plan['entries'][0]['target'])[0], 'sent')
        self.assertEqual(self.row()['message_id'], mid)
        self.assertEqual(len(self.f.smtp.messages), 6)
        self.assertEqual(str(self.f.smtp.messages[0]['Message-ID']), str(self.f.smtp.messages[-1]['Message-ID']))

    def test_lost_acceptance_record_stays_uncertain_and_is_not_automatically_retried(self):
        self.enable()
        original = batch.AuthorizedJournal.record
        failed = False
        def lose_receipt(journal, settings, plan, entry, row, status, **kwargs):
            nonlocal failed
            if not failed and status == 'ACCEPTED':
                failed = True
                raise OSError('Synthetic acceptance persistence failure')
            return original(journal, settings, plan, entry, row, status, **kwargs)
        with patch.object(batch.AuthorizedJournal, 'record', lose_receipt):
            self.assertEqual(self.execute()[0], 'review')
        self.assertEqual(self.receipt()[0], 'UNCERTAIN')
        self.assertEqual(len(self.f.smtp.messages), 5)
        self.assertIn('review', self.execute()[0])
        self.assertEqual(len(self.f.smtp.messages), 5)

    def test_real_reader_schema_drift_stops_without_participant_reads_or_writes(self):
        scheduler = ReadScheduler(clock=self.clock.monotonic, sleep=self.clock.sleep, progress=lambda _: None)
        self.api.entries[-1]['original_headers'][2] = 'Changed synthetic header'
        with patch('gspread.service_account', side_effect=self.api.client):
            reader = pilot.PilotReader(self.s, read_scheduler=scheduler)
            with self.assertRaises(Exception): pilot.inspect_pilot(self.s, self.path, reader=reader, authorization=self.scope)
        self.assertEqual(len(self.api.calls), 3)
        self.assertTrue(all(len(c['params']['ranges']) == 1 for c in self.api.calls))
        self.assertFalse((self.s.runtime/'production-batches.sqlite3').exists())

    def test_quota_backoff_is_bounded_and_never_retries_writes_or_smtp(self):
        scheduler = ReadScheduler(clock=self.clock.monotonic, sleep=self.clock.sleep, jitter=lambda *_: 0,
                                  progress=lambda _: None, attempts=3, budget=30)
        self.api.hook = lambda _: (_ for _ in ()).throw(quota_error())
        with patch('gspread.service_account', side_effect=self.api.client):
            reader = pilot.PilotReader(self.s, read_scheduler=scheduler)
            with self.assertRaises(Exception): pilot.inspect_pilot(self.s, self.path, reader=reader, authorization=self.scope)
        self.assertEqual(len(self.api.calls), 3)
        self.assertEqual(scheduler.retries, 2)
        self.assertTrue(all(c['method'] == 'get' for c in self.api.calls))
        self.assertFalse(self.f.smtp.connections)
        self.assertFalse(self.f.renderer.calls)

    def test_quota_wait_refreshes_approvals_before_processing(self):
        scheduler = ReadScheduler(clock=self.clock.monotonic, sleep=self.clock.sleep, jitter=lambda *_: 0,
                                  progress=lambda _: None)
        failed = False
        def quota(call):
            nonlocal failed
            if not failed and call['book'].endswith('discount_10') and len(call['params'].get('ranges', [])) > 1:
                failed = True
                self.row()['confirmation'] = False
                raise quota_error()
        self.api.hook = quota
        self.enable()
        with patch('gspread.service_account', side_effect=self.api.client):
            reader = pilot.PilotReader(self.s, read_scheduler=scheduler)
            with self.assertRaises(ConfigurationError):
                pilot.execute(self.s, self.path, reader=reader, renderer=self.f.renderer,
                              reload_settings=lambda: self.s, authorization=self.scope)
        self.assertEqual(scheduler.retries, 1)
        self.assertTrue(all(c['method'] == 'get' for c in self.api.calls))
        self.assertFalse(self.f.smtp.connections)
        times = [c['time'] for c in self.api.calls]
        self.assertTrue(all(b-a >= 1.25-1e-9 for a, b in zip(times, times[1:])))

    def test_stale_plan_cannot_adopt_row_twos_successful_linked_resend(self):
        self.f.enable()
        self.f.reader.sheet.fail_before = lambda c: c.get('email_status') == 'SENDING'
        with self.assertRaises(ConnectionError): self.f.run_pilot()
        self.s.email_enabled = False
        self.f.policy['enabled'] = False
        private_json(pilot.policy_path(self.s), self.f.policy)
        resend_policy = pilot_resend.prepare(self.s, self.f.path)
        self.f.enable()
        resend_policy['enabled'] = True
        private_json(pilot_resend.policy_path(self.s), resend_policy)
        pilot_resend.execute(self.s, self.f.path, acknowledged=True, reader=self.f.reader, reload_settings=lambda: self.s)
        self.s.email_enabled = False
        self.f.policy['enabled'] = False
        private_json(pilot.policy_path(self.s), self.f.policy)
        resend_policy['enabled'] = False
        private_json(pilot_resend.policy_path(self.s), resend_policy)
        stale = deepcopy(self.plan)
        stale.update(plan_id=str(uuid.uuid4()), entries=[deepcopy(self.f.plan['entries'][0])], limit=1, requested_limit=1)
        stale['tracking_scope']['targets'] = [stale['entries'][0]['target']]
        path = self.path.with_name('stale-row-two.json')
        private_json(path, stale)
        policy_path = batch.prepare(self.s, path)
        report = pilot.inspect_pilot(self.s, path, reader=self.f.reader, authorization=self.scope)
        self.assertEqual(report['entries'][0]['recovery'], 'ALREADY_SENT')
        policy = json.loads(policy_path.read_text())
        policy['enabled'] = True
        private_json(policy_path, policy)
        self.s.email_enabled = True
        journal = self.s.runtime/'production-batches.sqlite3'
        before = journal.read_bytes(), deepcopy(self.f.reader.sheet.data), len(self.f.smtp.messages)
        with self.assertRaisesRegex(ConfigurationError, 'already accepted'):
            pilot.execute(self.s, path, reader=self.f.reader, authorization=self.scope, reload_settings=lambda: self.s)
        self.assertEqual((journal.read_bytes(), self.f.reader.sheet.data, len(self.f.smtp.messages)), before)
        self.assertEqual(self.f.receipt()[0], 'INTENT')
        self.assertEqual(pilot_resend.read_attempt(self.s, self.f.plan)['status'], 'ACCEPTED')

    def test_local_deactivation_survives_asset_and_plan_drift(self):
        self.enable()
        self.path.write_bytes(self.encoded+b'\n')
        self.s.qr_x += 1
        with redirect_stdout(io.StringIO()): self.assertEqual(run_cli(self.s, self.args('--deactivate')), 0)
        self.assertFalse(self.policy()['enabled'])
        self.assertTrue(self.s.email_enabled)  # Deactivation never edits saved email settings.


if __name__ == '__main__':
    unittest.main()
