"""Normal-send exclusions use synthetic durable receipts, without network/rendering."""
import json
import hashlib
import sqlite3
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock, patch

from event_checkin import pilot, pilot_resend
from event_checkin.config import Settings
from event_checkin.delivery_evidence import DeliveryEvidence
from event_checkin.production import BatchJournal, BatchProcessor, digest, issued_identity, preview, private_json
from event_checkin.sheets import SheetIntegrityError
from tests.test_production import Sheet, record
from tests.fakes import FakeMailer


class DurableDeliveryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.s = Settings(root=root, profile='production', event_id='synthetic-event',
                          runtime=root/'runtime-production', generated=root/'generated-production')
        first = record(name='Synthetic Row Two', email='row2@example.invalid',
            tier='Regular (₱246)', _price_category='15_percent_discount',
            event_id=self.s.event_id, record_id='R-synthetic-two', participant_id='P-synthetic-two',
            ticket_token='synthetic-ticket-token', normalized_email='row2@example.invalid',
            ticket_name='Synthetic Row Two', ticket_tier='Regular', price_category='15_percent_discount',
            source_id='discount_15', state='READY', email_status='DISABLED')
        others = [record(email='row%d@example.invalid' % n, tier='Regular (₱246)',
                         _price_category='15_percent_discount',
                         **({'state': 'SENT', 'email_status': 'SENT'} if n in (3, 4) else {}))
                  for n in range(3, 11)]
        self.sheet = Sheet(discount_15=[first, *others])
        _, self.stale = preview(self.s, self.sheet, all_sources=True, limit=5)
        self.assertEqual([e['snapshot_row'] for e in self.stale['entries']], [2, 5, 6, 7, 8])
        self.parent = preview(self.s, self.sheet, ids=['discount_15:row:2'], limit=1)[1]
        # The original pilot selected a physical locator before assigning IDs.
        self.parent['entries'][0]['target'] = pilot_resend.TARGET
        self.parent['tracking_scope']['targets'] = [pilot_resend.TARGET]
        self.entry = self.parent['entries'][0]
        private_json(root/pilot.PLAN_RELATIVE, self.parent)
        self.journal = pilot_resend.ResendJournal(self.s)
        self.journal.bind(self.parent)
        self.action = self.journal.action(self.parent, self.entry)
        row = self.row()
        self.journal.claim(self.s, row)
        pilot.PilotJournal.record(self.journal, self.s, self.parent, self.entry, row, 'INTENT')
        self.parent_receipt = pilot.existing(self.s, self.parent, self.entry)[1]
        self.policy = dict(version=1, enabled=False, event_id=self.s.event_id,
            target=pilot_resend.TARGET, resend_id=pilot_resend.operation_id(self.parent),
            plan_sha256=hashlib.sha256((root/pilot.PLAN_RELATIVE).read_bytes()).hexdigest(),
            entry_sha256=digest(self.entry),
            identity_sha256=issued_identity(row), badge_sha256='synthetic-badge-hash',
            parent_action_sha256=digest(self.action), parent_receipt_sha256=digest(self.parent_receipt))
        private_json(pilot_resend.policy_path(self.s), self.policy)

    def row(self):
        return self.sheet.rows()[0]

    def linked_acceptance(self):
        self.journal.reserve(self.s, self.parent, self.policy, self.row(), self.action, self.parent_receipt)
        self.journal.record(self.s, self.parent, 'INTENT')
        self.journal.record(self.s, self.parent, 'ACCEPTED')

    def sql(self, statement, args=()):
        with sqlite3.connect(self.journal.path) as db:
            db.execute(statement, args)

    def snapshot(self):
        return self.journal.path.read_bytes(), deepcopy(self.sheet.data), deepcopy(self.stale)

    def test_row_two_linked_acceptance_excluded_and_counted_without_rewriting_history(self):
        self.linked_acceptance()
        before = self.snapshot()
        for version in (1, 2):
            report, plan, summary = preview(self.s, self.sheet, all_sources=True, limit=5,
                                            selection_version=version, include_summary=True)
            self.assertEqual([r['row'] for r in report if r['in_batch']], [5, 6, 7, 8, 9])
            self.assertTrue(report[0]['sent'])
            self.assertEqual(report[0]['blocking_reasons'], ['already sent; explicit resend required'])
            self.assertEqual(summary['discount_15']['already_sent_registrations'], 3)
            self.assertEqual(summary['discount_15']['blocked_registrations'], 0)
            self.assertEqual(summary['discount_15']['selected_eligible_registrations'], 5)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(pilot.existing(self.s, self.parent, self.entry)[1], self.parent_receipt)
        self.assertEqual(self.parent_receipt[0], 'INTENT')
        self.assertFalse(pilot.existing(self.s, self.parent, self.entry)[0][2])
        self.assertEqual(pilot_resend.read_attempt(self.s, self.parent)['status'], 'ACCEPTED')

    def test_stale_plan_rejected_before_journal_mutation_or_processing(self):
        self.linked_acceptance()
        audit = self.s.runtime/'incorrect-saved-plan.json'
        private_json(audit, self.stale)
        encoded = audit.read_bytes()
        before = self.snapshot()
        renderer, mailer = Mock(), Mock()
        service = BatchProcessor(self.s, self.sheet, renderer, mailer)
        with patch.object(service, 'journal_class', side_effect=AssertionError('No journal writes')), \
             self.assertRaisesRegex(SheetIntegrityError, 'Already sent.*stale'):
            service.execute(self.stale)
        renderer.render.assert_not_called()
        mailer.send.assert_not_called()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(audit.read_bytes(), encoded)
        self.assertFalse(self.s.email_enabled)
        self.assertFalse(json.loads(pilot_resend.policy_path(self.s).read_text())['enabled'])

    def test_receipt_rechecked_at_execution_gate_even_after_validated_selection(self):
        service = BatchProcessor(self.s, self.sheet, Mock(), Mock())
        service.plan_id = self.stale['plan_id']
        # A linked acceptance appears after the earlier plan/selection check.
        self.linked_acceptance()
        before = self.snapshot()
        service.journal = Mock()
        with self.assertRaisesRegex(SheetIntegrityError, 'Already sent'):
            service.checked(self.stale['entries'][0], self.row().get('record_id'), 'send')
        service.journal.claim.assert_not_called()
        self.assertEqual(self.snapshot(), before)

    def test_uncertain_parent_is_blocked_until_linked_acceptance_is_verified(self):
        report, plan, summary = preview(self.s, self.sheet, source='discount_15', include_summary=True)
        self.assertFalse(report[0]['sent'])
        self.assertIn('uncertain delivery requires staff review', report[0]['blocking_reasons'])
        self.assertNotIn(2, [e['snapshot_row'] for e in plan['entries']])
        self.assertEqual(summary['discount_15']['already_sent_registrations'], 2)

    def test_mismatched_or_ambiguous_receipts_fail_closed_in_preview_and_execution(self):
        self.linked_acceptance()
        original = self.journal.path.read_bytes()
        cases = [
            ("UPDATE claims SET event_id='another-event'", ()),
            ("UPDATE claims SET token='another-ticket'", ()),
            ("UPDATE pilot_resends SET parent_record_id='another-registration'", ()),
            ("UPDATE pilot_resends SET identity_hash='another-identity'", ()),
            ("UPDATE pilot_resends SET parent_message_id='another-message'", ()),
            ("UPDATE pilot_resends SET parent_receipt='[]'", ()),
            ("UPDATE pilot_resends SET binding_hash='unbound'", ()),
            ("UPDATE pilot_resends SET accepted_at=''", ()),
            ("UPDATE pilot_resends SET message_id=parent_message_id", ()),
            ("UPDATE pilot_resends SET status='UNKNOWN'", ()),
            ("UPDATE pilot_bindings SET digest='changed-plan'", ()),
            ("DELETE FROM pilot_resend_events WHERE status='ACCEPTED'", ()),
            ("INSERT INTO pilot_resends SELECT 'extra-receipt','unrelated-plan',target,parent_record_id,"
             "parent_message_id,parent_receipt,identity_hash,badge_hash,original_tracking_hash,binding_hash,"
             "'another-message',status,attempts,accepted_at FROM pilot_resends", ()),
        ]
        for statement, args in cases:
            with self.subTest(statement=statement):
                self.journal.path.write_bytes(original)  # Isolated synthetic fixture only.
                self.sql(statement, args)
                before = self.snapshot()
                report, plan = preview(self.s, self.sheet, source='discount_15')
                self.assertFalse(report[0]['sent'])
                self.assertTrue(any('binding mismatch or ambiguity' in b for b in report[0]['blocking_reasons']))
                self.assertNotIn(2, [e['snapshot_row'] for e in plan['entries']])
                with self.assertRaisesRegex(SheetIntegrityError, 'binding mismatch or ambiguity'):
                    BatchProcessor(self.s, self.sheet, Mock(), Mock()).execute(self.stale)
                self.assertEqual(self.snapshot(), before)

    def test_direct_pilot_acceptance_excludes_even_before_sent_tracking_is_saved(self):
        pilot.PilotJournal.record(self.journal, self.s, self.parent, self.entry, self.row(), 'ACCEPTED')
        report, _, summary = preview(self.s, self.sheet, source='discount_15', include_summary=True)
        self.assertTrue(report[0]['sent'])
        self.assertEqual(summary['discount_15']['already_sent_registrations'], 3)

    def test_orphaned_acceptance_event_cannot_make_a_registration_eligible(self):
        pilot.PilotJournal.record(self.journal, self.s, self.parent, self.entry, self.row(), 'ACCEPTED')
        self.sql('DELETE FROM pilot_delivery')
        report, plan = preview(self.s, self.sheet, source='discount_15')
        self.assertFalse(report[0]['sent'])
        self.assertIn('binding mismatch or ambiguity', report[0]['blocking_reasons'][0])
        self.assertNotIn(2, [e['snapshot_row'] for e in plan['entries']])

    def test_verified_acceptance_overrides_original_uncertainty_only_for_eligibility(self):
        self.linked_acceptance()
        self.sheet.data['sources']['discount_15'][0].update(state='EMAIL_REVIEW', email_status='AMBIGUOUS')
        before = self.snapshot()
        report, _ = preview(self.s, self.sheet, source='discount_15')
        self.assertEqual(report[0]['blocking_reasons'], ['already sent; explicit resend required'])
        self.assertEqual(self.snapshot(), before)

    def test_acceptance_does_not_remove_approval_or_frozen_identity_safeguards(self):
        self.linked_acceptance()
        self.sheet.data['sources']['discount_15'][0]['confirmation'] = False
        report, _ = preview(self.s, self.sheet, source='discount_15')
        self.assertTrue(report[0]['sent'])
        self.assertTrue(any('waiting for approval' in b for b in report[0]['blocking_reasons']))
        self.sheet.data['sources']['discount_15'][0]['ticket_tier'] = 'Pro'
        report, _ = preview(self.s, self.sheet, source='discount_15')
        self.assertFalse(report[0]['sent'])
        self.assertTrue(any('binding mismatch' in b for b in report[0]['blocking_reasons']))

    def test_receipts_do_not_grant_resend_authorization(self):
        self.linked_acceptance()
        before = self.snapshot()
        report, plan = preview(self.s, self.sheet, ids=['discount_15:row:2'], action='resend')
        self.assertTrue(report[0]['sent'])
        self.assertEqual(plan['action'], 'resend')
        self.assertFalse(plan['entries'])
        self.assertIn('resend requires a sent registration', report[0]['blocking_reasons'])
        self.assertFalse(self.s.email_enabled)
        self.assertFalse(json.loads(pilot_resend.policy_path(self.s).read_text())['enabled'])
        self.assertEqual(self.snapshot(), before)

    def test_explicit_resend_of_sent_tracking_retains_its_separate_behavior(self):
        pilot.PilotJournal.record(self.journal, self.s, self.parent, self.entry, self.row(), 'ACCEPTED')
        badge = self.s.generated/'P-synthetic-two/card.png'
        badge.parent.mkdir(parents=True)
        badge.write_bytes(b'synthetic-fixture-badge')
        self.sheet.data['sources']['discount_15'][0].update(
            state='SENT', email_status='SENT', email_sent_at=self.s.now(),
            render_status='RENDERED', card_path=str(badge), message_id=self.action[1])
        _, plan = preview(self.s, self.sheet, ids=['discount_15:row:2'], action='resend')
        self.assertEqual(len(plan['entries']), 1)
        renderer, mailer = Mock(), FakeMailer()
        self.s.email_enabled = True  # Synthetic settings and fake transport only.
        with patch.object(self.s, 'require_email'):
            outcomes = BatchProcessor(self.s, self.sheet, renderer, mailer).execute(plan)
        self.assertEqual(outcomes, ['sent'])
        self.assertEqual(len(mailer.calls), 1)
        renderer.render.assert_not_called()

    def test_missing_journal_preview_remains_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            self.s.runtime = Path(directory)/'absent-runtime'
            preview(self.s, self.sheet, all_sources=True)
            self.assertFalse(self.s.runtime.exists())


class SingleEntryEvidenceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.s = Settings(root=root, profile='production', event_id='synthetic-event',
                          runtime=root/'runtime-production', generated=root/'generated-production')
        self.sheet = Sheet(regular=[record(event_id=self.s.event_id, record_id='R-single',
            participant_id='P-single', ticket_token='single-synthetic-token',
            normalized_email='synthetic@example.test', ticket_name='Synthetic Participant',
            ticket_tier='Regular', price_category='regular', source_id='regular',
            state='PROCESSING', email_status='DISABLED')])
        self.plan = preview(self.s, self.sheet, source='regular')[1]
        self.journal = BatchJournal(self.s)
        self.journal.bind(self.plan)
        self.journal.action(self.plan, self.plan['entries'][0])
        self.journal.claim(self.s, self.sheet.rows()[0])
        selection = {'event_id': self.s.event_id, 'plan': self.plan}
        self.selection = self.s.runtime/'single-selection.json'
        private_json(self.selection, selection)
        self.evidence = self.s.runtime/'single-selection.smtp-evidence-001.json'
        private_json(self.evidence, {'accepted': True,
            'selection_sha256': hashlib.sha256(self.selection.read_bytes()).hexdigest()})

    def test_bound_single_entry_checkpoint_is_counted_and_excluded(self):
        before = self.journal.path.read_bytes(), deepcopy(self.sheet.data)
        report, plan, summary = preview(self.s, self.sheet, source='regular', include_summary=True)
        self.assertTrue(report[0]['sent'])
        self.assertEqual(plan['entries'], [])
        self.assertEqual(summary['regular']['already_sent_registrations'], 1)
        with self.assertRaisesRegex(SheetIntegrityError, 'Already sent.*stale'):
            BatchProcessor(self.s, self.sheet, Mock(), Mock()).execute(self.plan)
        self.assertEqual((self.journal.path.read_bytes(), self.sheet.data), before)

    def test_unbound_checkpoint_or_ambiguous_retry_is_blocked(self):
        payload = self.evidence.read_bytes()
        for kind in ('selection-hash', 'multiple-actions'):
            with self.subTest(kind=kind):
                if kind == 'selection-hash':
                    private_json(self.evidence, {'accepted': True, 'selection_sha256': 'unbound'})
                else:
                    self.evidence.write_bytes(payload)
                    retry = deepcopy(self.plan)
                    retry['plan_id'] = 'separate-synthetic-retry'
                    self.journal.bind(retry)
                    self.journal.action(retry, retry['entries'][0])
                report, plan = preview(self.s, self.sheet, source='regular')
                self.assertFalse(report[0]['sent'])
                self.assertFalse(plan['entries'])
                self.assertIn('binding mismatch or ambiguity', report[0]['blocking_reasons'][0])


if __name__ == '__main__':
    unittest.main()
