"""Offline policy/preview checks only: no identity reservations, rendering or SMTP."""
import itertools
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from event_checkin.config import ConfigurationError, Settings
from event_checkin.inspection import aggregate
from event_checkin.production import BatchProcessor, details, fingerprint, preview, reasons, valid_details
from event_checkin.production_sheets import APPROVAL_HEADERS, FixtureSheets
from event_checkin.sheets import Row, SheetIntegrityError
from event_checkin.tiers import APPROVED_TIER_ALIASES, TIERS, resolve_tier

APPROVED = (
    ('regular', 'Lite (₱199)', 'Lite'),
    ('discount_15', 'Lite (₱170)', 'Lite'),
    ('discount_15', 'Pro (₱297)', 'Pro'),
)
CATEGORIES = {'regular': 'regular', 'discount_15': '15_percent_discount', 'discount_10': '10_percent_discount'}


def registration(source, label, **changes):
    data = {'name': 'Synthetic Example', 'email': 'synthetic@example.test', 'tier': label,
            '_price_category': CATEGORIES[source], 'event_id': 'synthetic-alias-event'}
    data.update({key: True for key in APPROVAL_HEADERS})
    data.update(changes)
    return Row(2, data, source)


def sheet_for(*rows):
    data = {source: [] for source in CATEGORIES}
    for row in rows:
        data[row.source_id].append(deepcopy(row.data))
    return FixtureSheets({'synthetic': True, 'fixture_id': 'alias-tests', 'sources': data})


class TierAliasTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.s = Settings(profile='production', event_id='synthetic-alias-event',
                          runtime=root / 'runtime', generated=root / 'generated', email_enabled=False)

    def tearDown(self):
        self.assertFalse(self.s.runtime.exists(), 'Policy tests must not reserve identities or create journals.')
        self.assertFalse(self.s.generated.exists(), 'Policy tests must not render cards.')
        self.temp.cleanup()

    def test_exactly_three_approved_source_specific_aliases(self):
        actual = {(source, label, tier) for source, aliases in APPROVED_TIER_ALIASES.items() for label, tier in aliases.items()}
        self.assertEqual(actual, set(APPROVED))
        for source, label, tier in APPROVED:
            with self.subTest(source=source, label=label):
                row = registration(source, label)
                before = deepcopy(row.data)
                sheet = sheet_for(row)
                report, _ = preview(self.s, sheet, all_sources=True)
                self.assertEqual(resolve_tier(source, label), tier)
                self.assertEqual(details(row), ('Synthetic Example', tier, CATEGORIES[source]))
                self.assertTrue(valid_details(row))
                self.assertEqual(report[0]['tier'], label)
                self.assertEqual(report[0]['mapped_tier'], tier)
                self.assertEqual(report[0]['price_category'], CATEGORIES[source])
                self.assertTrue(report[0]['in_batch'])
                self.assertEqual(row.data, before)
                self.assertEqual(sheet.data['sources'][source][0], before)
                self.assertFalse(row.get('participant_id'))
                self.assertFalse(row.get('ticket_token'))

    def test_existing_canonical_tiers_remain_exact_and_supported(self):
        for source, tier in itertools.product(CATEGORIES, TIERS):
            self.assertEqual(resolve_tier(source, tier), tier)
        for source in CATEGORIES:
            for raw in ('lite', 'regular', 'PRO', ' Regular ', 'Regular ticket'):
                self.assertIsNone(resolve_tier(source, raw))

    def test_unknown_labels_prices_types_and_sources_fail_closed(self):
        rejected = (
            ('regular', 'Lite (₱170)'), ('regular', 'Pro (₱297)'),
            ('discount_15', 'Lite (₱199)'), ('discount_10', 'Lite (₱199)'),
            ('discount_10', 'Lite (₱170)'), ('discount_10', 'Pro (₱297)'),
            ('regular', 'Lite (₱200)'), ('regular', 'Lite (PHP 199)'),
            ('regular', 'Lite (₱199.00)'), ('regular', 'Lite (₱199) '),
            ('regular', ' Lite (₱199)'), ('regular', 'lite (₱199)'),
            ('regular', 'Lite Ticket (₱199)'), ('discount_15', 'Pro(₱297)'),
            ('regular', 'Regular (₱199)'), ('regular', 'Pro (₱1000)'),
            ('regular', ''), ('regular', None), ('regular', True), ('regular', ['Lite']),
        )
        for source, label in rejected:
            with self.subTest(source=source, label=label):
                self.assertIsNone(resolve_tier(source, label))
                row = registration(source, label)
                self.assertFalse(valid_details(row))
                report, _ = preview(self.s, sheet_for(row), all_sources=True)
                self.assertFalse(report[0]['in_batch'])
                self.assertIn('unknown raw tier', str(report[0]['blocking_reasons']))
        self.assertIsNone(resolve_tier('unknown-source', 'Lite'))
        self.assertIsNone(resolve_tier('unknown-source', 'Lite (₱199)'))
        for source in (None, [], {}):
            self.assertIsNone(resolve_tier(source, 'Lite (₱199)'))

    def test_all_16_approval_combinations_for_each_alias_only_all_true_qualifies(self):
        for source, label, tier in APPROVED:
            for combo in itertools.product((False, True), repeat=4):
                with self.subTest(source=source, label=label, approvals=combo):
                    row = registration(source, label, **dict(zip(APPROVAL_HEADERS, combo)))
                    before = deepcopy(row.data)
                    report, _ = preview(self.s, sheet_for(row), all_sources=True)
                    self.assertEqual(report[0]['mapped_tier'], tier)
                    self.assertEqual(report[0]['in_batch'], all(combo))
                    self.assertEqual(row.data, before)
                    if not all(combo):
                        self.assertIn('waiting for approval', str(report[0]['blocking_reasons']))

    def test_blank_false_malformed_or_missing_checks_block_every_alias(self):
        for source, label, _ in APPROVED:
            for key in APPROVAL_HEADERS:
                for value in (None, '', 'FALSE', False, 'false', 'true', 1, 'TRUE ', [], {}):
                    row = registration(source, label, **{key: value})
                    report, _ = preview(self.s, sheet_for(row), all_sources=True)
                    self.assertFalse(report[0]['in_batch'])
                row.data.pop(key)
                report, _ = preview(self.s, sheet_for(row), all_sources=True)
                self.assertFalse(report[0]['in_batch'])
            row = registration(source, label, **dict.fromkeys(APPROVAL_HEADERS, 'TRUE'))
            self.assertTrue(preview(self.s, sheet_for(row), all_sources=True)[0][0]['in_batch'])

    def test_aliases_cannot_bypass_retry_or_resend_approvals(self):
        for source, label, tier in APPROVED:
            for action in ('retry', 'resend'):
                row = registration(source, label, record_id='R-existing-synthetic', participant_id='P-existing-synthetic',
                                   ticket_token='x' * 43, ticket_tier=tier, normalized_email='synthetic@example.test',
                                   ticket_name='Synthetic Example', source_id=source, price_category=CATEGORIES[source],
                                   ticket_status='ACTIVE', state='EMAIL_ERROR' if action == 'retry' else 'SENT',
                                   email_status='FAILED' if action == 'retry' else 'SENT')
                before = deepcopy(row.data)
                self.assertEqual(reasons(row, [row], self.s.event_id, action), [])
                for key in APPROVAL_HEADERS:
                    row.data[key] = 'FALSE'
                    self.assertIn('waiting for approval', str(reasons(row, [row], self.s.event_id, action)))
                    row.data[key] = True
                self.assertEqual(row.data, before)

    def test_conflicting_approved_alias_tiers_still_require_duplicate_review(self):
        sheet = sheet_for(registration('discount_15', 'Lite (₱170)'), registration('discount_15', 'Pro (₱297)'))
        before = deepcopy(sheet.data)
        report, _ = preview(self.s, sheet, all_sources=True)
        self.assertTrue(all(not r['in_batch'] and 'conflicting approved' in str(r['blocking_reasons']) for r in report))
        self.assertEqual(sheet.data, before)

    def test_same_mapped_tier_with_different_price_categories_still_conflicts(self):
        sheet = sheet_for(registration('regular', 'Lite (₱199)'), registration('discount_15', 'Lite (₱170)'))
        report, _ = preview(self.s, sheet, all_sources=True)
        self.assertEqual([r['mapped_tier'] for r in report], ['Lite', 'Lite'])
        self.assertTrue(all(not r['in_batch'] and 'conflicting approved' in str(r['blocking_reasons']) for r in report))

    def test_equivalent_duplicates_still_select_only_existing_canonical_order(self):
        sheet = sheet_for(registration('discount_15', 'Lite (₱170)'), registration('discount_15', 'Lite'))
        report, _ = preview(self.s, sheet, all_sources=True)
        self.assertEqual([r['in_batch'] for r in report], [True, False])
        self.assertIn('duplicate of approved canonical', str(report[1]['blocking_reasons']))
        self.assertTrue(all(not r.get('participant_id') and not r.get('ticket_token') for r in sheet.rows()))

    def test_raw_value_changes_invalidate_plan_even_when_mapped_tier_is_same(self):
        row = registration('regular', 'Lite (₱199)')
        sheet = sheet_for(row)
        _, plan = preview(self.s, sheet, all_sources=True)
        sheet.data['sources']['regular'][0]['tier'] = 'Lite'
        changed = sheet.rows()[0]
        self.assertEqual(details(row), details(changed))
        self.assertNotEqual(fingerprint(row), fingerprint(changed))
        processor = BatchProcessor(self.s, sheet, None, None)
        with self.assertRaises(SheetIntegrityError):
            processor.locate(sheet.rows(), plan['entries'][0], 'R-not-assigned')

    def test_inspection_shares_exact_mapping_without_guessing_other_prices(self):
        data = {'regular': [registration('regular', 'Lite (₱199)', confirmation=False).data],
                'discount_15': [registration('discount_15', 'Pro (₱297)', confirmation=False).data],
                'discount_10': [registration('discount_10', 'Pro (₱297)').data]}
        before = deepcopy(data)
        report = aggregate(data)['sources']
        self.assertTrue(report['regular']['tier_labels'][0]['recognized_by_current_processor'])
        self.assertEqual(report['regular']['controlled_test_candidates_with_approved_tier'], 0)
        self.assertTrue(report['discount_15']['tier_labels'][0]['recognized_by_current_processor'])
        self.assertFalse(report['discount_10']['tier_labels'][0]['recognized_by_current_processor'])
        self.assertEqual(data, before)

    def test_production_processing_and_email_are_still_locked(self):
        for method in (self.s.require_operational, self.s.require_sheet, self.s.require_email):
            with self.assertRaises(ConfigurationError):
                method()
        self.assertFalse(self.s.email_enabled)


if __name__ == '__main__':
    unittest.main()
