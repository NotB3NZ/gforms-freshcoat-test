import io
import itertools
import json
import unittest
from copy import deepcopy
from types import SimpleNamespace
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import Mock, patch

from event_checkin.inspection import InspectionReader, aggregate, public_tier, valid_email
from event_checkin.production_sheets import APPROVAL_HEADERS, PRODUCTION_METADATA
from event_checkin.sheets import SheetIntegrityError
from tests import test_header_init


def registration(**changes):
    row = dict(name='Private Synthetic Person', email='private@example.test', tier='Regular',
               state='', email_status='', render_status='', ticket_status='')
    row.update({key: True for key in APPROVAL_HEADERS})
    row.update(changes)
    return row


def sources(*rows):
    return {'regular': list(rows), 'discount_15': [], 'discount_10': []}


class AggregateInspectionTests(unittest.TestCase):
    def test_all_16_combinations_and_malformed_values_are_aggregate_only(self):
        rows = [registration(**dict(zip(APPROVAL_HEADERS, combo))) for combo in itertools.product((True, False), repeat=4)]
        result = aggregate(sources(*rows))['sources']['regular']
        self.assertEqual(result['registrations'], 16)
        self.assertEqual(result['all_four_approvals_true'], 1)
        self.assertEqual(result['awaiting_approval'], 15)
        for value in ('FALSE', False, '', None, 'yes', 'true', 1, 'TRUE '):
            result = aggregate(sources(registration(confirmation=value)))['sources']['regular']
            self.assertEqual(result['all_four_approvals_true'], 0)
        self.assertEqual(aggregate(sources(registration(confirmation='TRUE')))['sources']['regular']['all_four_approvals_true'], 1)

    def test_counts_tiers_blanks_unrecognized_missing_contacts_and_statuses(self):
        data = sources(registration(), registration(tier='Lite Ticket (₱500)'), registration(tier='VIP', name='', email='bad'),
                       registration(tier='', name=' ', email=''), registration(tier='Regular / Pro'))
        result = aggregate(data)['sources']['regular']
        self.assertEqual(result['registrations'], 5)
        self.assertEqual(result['blank_tiers'], 1)
        self.assertEqual(result['unrecognized_by_current_processor'], 3)
        self.assertEqual(result['missing_names'], 2)
        self.assertEqual(result['invalid_emails_including_missing'], 2)
        self.assertEqual(result['missing_emails'], 1)
        self.assertEqual(result['processing_status']['state'], {'(blank)': 5})
        labels = {x['label']: x for x in result['tier_labels']}
        self.assertIsNone(labels['Lite Ticket (₱500)']['mapped_tier'])
        self.assertIsNone(labels['Regular / Pro']['mapped_tier'])

    def test_prefilled_checkbox_rows_do_not_become_registrations(self):
        data = sources(registration(), {}, dict.fromkeys(APPROVAL_HEADERS, False), dict.fromkeys(APPROVAL_HEADERS, True))
        result = aggregate(data)['sources']['regular']
        self.assertEqual(result['registrations'], 1)
        self.assertEqual(result['checkbox_only_rows_excluded'], 2)
        # A malformed/blank contact row with an actual form field still counts.
        result = aggregate(sources({'name': ' '}))['sources']['regular']
        self.assertEqual(result['registrations'], 1)
        self.assertEqual(result['missing_names'], 1)

    def test_cross_source_duplicates_conflicting_tiers_names_and_approved_groups(self):
        data = {'regular': [registration(email='  PERSON@EXAMPLE.TEST ', tier='Lite')],
                'discount_15': [registration(email='person@example.test', tier='Pro', name='Other Private Person')],
                'discount_10': [registration(email='person+tag@example.test')]}
        report = aggregate(data)
        duplicates = report['duplicates']
        self.assertEqual(duplicates['cross_source_email_groups'], 1)
        self.assertEqual(duplicates['registrations_in_cross_source_groups'], 2)
        self.assertEqual(duplicates['cross_source_groups_with_conflicting_mapped_tiers'], 1)
        self.assertEqual(duplicates['cross_source_groups_with_conflicting_all_approved_tiers'], 1)
        self.assertEqual(duplicates['cross_source_groups_with_different_names'], 1)
        self.assertEqual(report['sources']['discount_10']['controlled_test_candidates_with_approved_tier'], 1)
        output = json.dumps(report)
        self.assertNotIn('PERSON@', output)
        self.assertNotIn('person@example.test', output)
        self.assertNotIn('Private Person', output)

    def test_price_label_variants_do_not_imply_a_conflicting_canonical_tier(self):
        data = {'regular': [registration(tier='Lite (₱199)')],
                'discount_15': [registration(tier='Lite (₱170)')], 'discount_10': []}
        report = aggregate(data)['duplicates']
        self.assertEqual(report['cross_source_groups_with_different_raw_tiers'], 1)
        self.assertEqual(report['cross_source_groups_with_conflicting_mapped_tiers'], 0)
        self.assertEqual(report['cross_source_groups_with_ambiguous_or_blank_tiers'], 0)

    def test_within_source_duplicate_sizes_and_conflicting_tiers_remain_aggregate(self):
        data = sources(registration(tier='Lite'), registration(tier='Pro'), registration(tier='Pro'),
                       registration(email='other@example.test'), registration(email='other@example.test'))
        report = aggregate(data)['duplicates']
        self.assertEqual(report['duplicate_email_groups_event_wide'], 2)
        self.assertEqual(report['registrations_in_event_wide_duplicate_groups'], 5)
        self.assertEqual(report['event_wide_extra_registrations'], 3)
        self.assertEqual(report['event_wide_groups_with_conflicting_mapped_tiers'], 1)
        self.assertEqual(report['within_source_duplicates']['regular']['email_groups'], 2)
        self.assertEqual(report['within_source_duplicates']['regular']['groups_with_conflicting_mapped_tiers'], 1)
        self.assertEqual(report['cross_source_email_groups'], 0)
        self.assertNotIn('other@example.test', json.dumps(report))

    def test_gmail_dots_plus_tags_and_invalid_email_are_not_merged(self):
        data = sources(*(registration(email=x) for x in ('a.b@gmail.com', 'ab@gmail.com', 'ab+tag@gmail.com', 'bad', 'bad')))
        self.assertEqual(aggregate(data)['duplicates']['duplicate_email_groups_event_wide'], 0)
        self.assertFalse(valid_email('bad\x00@example.test'))

    def test_labels_and_unexpected_status_cannot_leak_names_emails_or_tokens(self):
        name, email, token = 'Hidden Private Name', 'hidden@example.test', 'VERY_PRIVATE_TOKEN_' + 'x' * 32
        data = sources(registration(name=name, email=email, tier=email, state=token),
                       registration(tier=name), registration(tier=token), registration(tier=12345678901))
        output = json.dumps(aggregate(data))
        for value in (name, email, token, '12345678901'):
            self.assertNotIn(value, output)
        self.assertIn('withheld', output)
        self.assertIn('(unrecognized)', output)

    def test_report_uses_only_exact_approved_aliases_and_preserves_input(self):
        data = {'regular': [registration(tier='Lite (₱199)')],
                'discount_15': [registration(tier='Pro (₱297)')],
                'discount_10': [registration(tier='Pro (₱297)')]}
        original = deepcopy(data)
        report = aggregate(data)['sources']
        self.assertEqual(report['regular']['tier_labels'][0]['mapped_tier'], 'Lite')
        self.assertEqual(report['discount_15']['tier_labels'][0]['mapped_tier'], 'Pro')
        self.assertTrue(report['regular']['tier_labels'][0]['recognized_by_current_processor'])
        self.assertIsNone(report['discount_10']['tier_labels'][0]['mapped_tier'])
        self.assertTrue(report['discount_10']['tier_labels'][0]['review_required'])
        self.assertEqual(data, original)

    def test_unapproved_price_variants_are_not_inferred_even_in_duplicate_reports(self):
        data = {'regular': [registration(tier='Regular (PHP 500)')],
                'discount_15': [registration(tier='Regular (PHP 425)')], 'discount_10': []}
        report = aggregate(data)
        for source in ('regular', 'discount_15'):
            self.assertIsNone(report['sources'][source]['tier_labels'][0]['mapped_tier'])
        self.assertEqual(report['duplicates']['cross_source_groups_with_ambiguous_or_blank_tiers'], 1)


class InspectionReaderTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_header_init.HeaderInitTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.plan = self.fixture.plan
        self.header_transport = Mock()
        self.header_transport.read.side_effect = lambda entry: {
            'title': entry['source']['tab_name'], 'tab_id': entry['source']['tab_id'],
            'column_count': len(entry['original_headers']) + 24,
            'headers': entry['original_headers'] + list(PRODUCTION_METADATA)}
        self.requested_ranges = []
        def batch_get(spreadsheet_id, ranges, params):
            self.requested_ranges.append((spreadsheet_id, ranges, params))
            values = []
            for span in ranges:
                values.append({'range': span + '1000', 'majorDimension': 'COLUMNS', 'values': []})
            return {'spreadsheetId': spreadsheet_id, 'valueRanges': values}
        self.header_transport.http.values_batch_get.side_effect = batch_get

    def reader(self):
        with patch('event_checkin.inspection.HeaderTransport', return_value=self.header_transport) as constructor:
            reader = InspectionReader(self.fixture.s, self.plan)
            constructor.assert_called_once_with(self.fixture.s, self.plan, apply=False)
        return reader

    def test_reads_only_11_approved_columns_never_tokens_or_uploads(self):
        result = self.reader().read_all()
        self.assertEqual(result, {'regular': [], 'discount_15': [], 'discount_10': []})
        for index, (spreadsheet, ranges, params) in enumerate(self.requested_ranges):
            self.assertEqual(len(ranges), 11)
            columns = [span.split('!')[1].split('2:')[0] for span in ranges]
            if index == 0:
                self.assertEqual(set(columns), {'C', 'D', 'H', 'A', 'U', 'V', 'W', 'AC', 'AD', 'AE', 'AP'})
            else:
                self.assertEqual(set(columns), {'C', 'D', 'M', 'A', 'S', 'T', 'U', 'AA', 'AB', 'AC', 'AN'})
            self.assertEqual(params, {'majorDimension': 'COLUMNS', 'valueRenderOption': 'UNFORMATTED_VALUE'})
        self.header_transport.write.assert_not_called()
        self.header_transport.http.batch_update.assert_not_called()
        self.header_transport.http.values_get.assert_not_called()

    def test_missing_source_headers_stops_before_participant_reads(self):
        original = self.header_transport.read.side_effect
        def read(entry):
            if entry['source']['id'] == 'discount_10':
                raise SheetIntegrityError('Missing source')
            return original(entry)
        self.header_transport.read.side_effect = read
        with self.assertRaises(SheetIntegrityError):
            self.reader().read_all()
        self.assertFalse(self.requested_ranges)

    def test_unexpected_response_range_shape_or_source_stops_report(self):
        original = self.header_transport.http.values_batch_get.side_effect
        for kind in ('range', 'shape', 'source'):
            def read(*args, **kwargs):
                result = original(*args, **kwargs)
                if kind == 'range':
                    result['valueRanges'][0]['range'] = "'Participants'!K2:K1000"
                elif kind == 'shape':
                    result['valueRanges'][0]['majorDimension'] = 'ROWS'
                else:
                    result['spreadsheetId'] = 'wrong'
                return result
            self.header_transport.http.values_batch_get.side_effect = read
            with self.subTest(kind=kind), self.assertRaises(SheetIntegrityError):
                self.reader().read_all()

    def test_cli_prints_only_aggregates_and_sanitizes_provider_failure(self):
        from event_checkin.__main__ import main
        with patch('sys.argv', ['event_checkin', '--profile', 'production', 'production-inspect', '--plan', str(self.fixture.path)]), \
                patch('event_checkin.__main__.load_settings', return_value=self.fixture.s), \
                patch('event_checkin.inspection.InspectionReader') as reader, \
                patch('event_checkin.production_cli.CardRenderer') as renderer, \
                patch('event_checkin.production_cli.SMTPMailer') as mailer:
            reader.return_value.read_all.return_value = sources(registration())
            with redirect_stdout(io.StringIO()) as output:
                self.assertIsNone(main())
            self.assertNotIn('private@example.test', output.getvalue())
            self.assertNotIn('Private Synthetic Person', output.getvalue())
            self.assertIn('LOCKED', output.getvalue())
            reader.return_value.read_all.side_effect = ConnectionError('private@example.test Private Synthetic Person')
            with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as error:
                self.assertEqual(main(), 1)
            self.assertNotIn('private@example.test', error.getvalue())
            self.assertNotIn('Private Synthetic Person', error.getvalue())
            self.assertEqual(output.getvalue(), '')
            renderer.assert_not_called()
            mailer.assert_not_called()
