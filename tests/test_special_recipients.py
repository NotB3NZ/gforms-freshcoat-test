import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from event_checkin.production import fingerprint, private_json, reasons, recipient
from event_checkin.sheets import Row
from event_checkin.special_recipients import POLICY, TAB_ID, apply_recipient_overrides, binding
from event_checkin.special_source import special_source
from event_checkin.production_sheets import APPROVAL_HEADERS


class SpecialRecipientsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.s = SimpleNamespace(runtime=Path(tmp.name), event_id='synthetic')
        self.source = special_source('synthetic', 'Full Name')
        self.policy = self.s.runtime / POLICY
        self.f = 'attendee@example.invalid'
        self.anchor = self.row(15, 'Booking Contact')
        self.overridden = self.row(17, 'Named Attendee')
        self.calls = []

    def row(self, number, name, *, source='special', email='booker@example.invalid', tier='Pass'):
        return Row(number, {'submission_timestamp': 46001.75, 'email': email,
                            'name': name, 'tier': tier, '_price_category': source,
                            **dict.fromkeys(APPROVAL_HEADERS, True)}, source)

    def values_batch_get(self, spreadsheet, ranges, params):
        self.calls.append(ranges)
        return {'valueRanges': [{'range': name, 'values': [[self.f]]} for name in ranges]}

    def approve(self):
        private_json(self.policy, {'version': 1, 'event_id': self.s.event_id, 'tab_id': TAB_ID,
                                   'rows': {'17': binding(self.s, self.overridden, self.f)}})

    def annotate(self, *additional):
        return apply_recipient_overrides(self.s, [self.anchor, self.overridden, *additional], self.source, self)

    def test_exact_F_override_keeps_form_C_and_preexisting_fingerprints(self):
        original = fingerprint(self.overridden)
        self.annotate()
        self.assertEqual(self.calls, [])
        self.approve()
        rows = self.annotate()
        self.assertEqual(self.calls, [["'SPECIAL | REGIS'!F17"]])
        self.assertEqual(recipient(self.overridden), self.f)
        self.assertEqual(self.overridden.get('email'), 'booker@example.invalid')
        self.assertEqual(recipient(self.anchor), 'booker@example.invalid')
        self.assertEqual(reasons(self.overridden, rows, self.s.event_id), [])
        self.assertNotEqual(fingerprint(self.overridden), original)
        self.policy.unlink()
        self.annotate()
        self.assertEqual(fingerprint(self.overridden), original)

    def test_changed_F_or_registration_blocks_instead_of_falling_back_to_send(self):
        self.approve()
        self.annotate()
        old = fingerprint(self.overridden)
        self.f = 'new@example.invalid'
        rows = self.annotate()
        self.assertNotEqual(fingerprint(self.overridden), old)
        self.assertIn('Special recipient approval changed; staff review required',
                      reasons(self.overridden, rows, self.s.event_id))
        self.f = 'attendee@example.invalid'
        self.overridden.data['submission_timestamp'] += 1
        self.assertIn('Special recipient approval changed; staff review required',
                      reasons(self.overridden, self.annotate(), self.s.event_id))

    def test_payment_and_cross_source_duplicate_checks_still_apply(self):
        self.approve()
        other = self.row(4, 'Someone Across Tabs', source='regular', email=self.f, tier='Lite')
        rows = self.annotate(other)
        self.assertIn('conflicting approved registrations; staff review required',
                      reasons(self.overridden, rows, self.s.event_id))
        self.overridden.data['confirmation'] = False
        self.assertIn('waiting for approval: confirmation',
                      reasons(self.overridden, self.annotate(), self.s.event_id))

    def test_same_attendee_using_original_C_still_blocked(self):
        self.approve()
        repeat = self.row(34, ' named  attendee ')
        rows = self.annotate(repeat)
        for row in (self.overridden, repeat):
            self.assertTrue(any('possible duplicate attendee' in b for b in reasons(row, rows, self.s.event_id)))
