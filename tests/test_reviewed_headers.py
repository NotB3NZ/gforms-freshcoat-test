"""Synthetic schema tests for the explicitly reviewed duplicate email headers."""
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from event_checkin.config import ROOT, Settings
from event_checkin.header_init import APPROVED_RANGES, EVENT_ID, classify, load_plan
from event_checkin.production_sheets import (APPROVAL_COLUMNS, APPROVAL_HEADERS, PRODUCTION_METADATA,
                                            REVIEWED_DUPLICATE_HEADERS, ProductionTable, column_number, source_mapping)
from event_checkin.sheets import Row, SheetIntegrityError
from event_checkin.production import valid_details
from event_checkin.tiers import resolve_tier
from tests.test_production import Worksheet

NAME_HEADER = "Full Name (e.g Juan S. Dela Cruz)"
TIER_HEADER = "10% OFF Tickets (Pro Tickets are not available)"


def reviewed_source(source_id, tracking=True):
    sources = json.loads((ROOT / 'config/sources.production.example.json').read_text())['sources']
    source = next(s for s in sources if s['id'] == source_id)
    source['spreadsheet_id'] = 'synthetic-' + source_id
    source['fields']['email'] = {'column': 'C', 'header': 'Email Address'}
    source['fields']['name'] = {'column': 'D', 'header': NAME_HEADER}
    if source_id == 'discount_10':
        source['fields']['tier'] = {'column': 'M', 'header': TIER_HEADER}
    source['approved_duplicate_headers'] = deepcopy(REVIEWED_DUPLICATE_HEADERS[source_id])
    count, _ = APPROVED_RANGES[source_id]
    header = ['Synthetic question %d' % i for i in range(count)]
    for label, col in source_mapping(source).values():
        header[column_number(col)-1] = label
    for col in source['approved_duplicate_headers']['Email Address']:
        header[column_number(col)-1] = 'Email Address'
    if tracking:
        header += list(PRODUCTION_METADATA)
    return source, header


def table_for(source_id, recipient='recipient@example.invalid'):
    source, header = reviewed_source(source_id)
    row = [''] * len(header)
    row[2], row[3] = recipient, 'Synthetic Issued Person'
    secondary = source['approved_duplicate_headers']['Email Address'][1]
    row[column_number(secondary)-1] = 'different-address@example.invalid'
    row[column_number(source['fields']['tier']['column'])-1] = 'Pro'
    for col in APPROVAL_COLUMNS[source_id]:
        row[column_number(col)-1] = True
    return ProductionTable(Settings(), source, Worksheet(header, [row]))


class ReviewedHeaderTests(unittest.TestCase):
    def test_exact_reviewed_duplicate_positions_and_labels_are_accepted(self):
        for source_id in APPROVED_RANGES:
            table = table_for(source_id)
            self.assertEqual(table.validate_header(table.ws.values[0], require_metadata=True), table.ws.values[0])

    def test_recipient_always_comes_from_C_never_secondary_email(self):
        for source_id in APPROVED_RANGES:
            for recipient in ('recipient@example.invalid', ''):
                with self.subTest(source=source_id, empty=not recipient):
                    table = table_for(source_id, recipient)
                    original = deepcopy(table.ws.values)
                    row = table.rows(require_metadata=True)[0]
                    self.assertEqual(row.get('email'), recipient)
                    self.assertEqual(row.get('name'), 'Synthetic Issued Person')
                    self.assertEqual(row.get('tier'), 'Pro')
                    self.assertEqual(table.ws.values, original)
                    ranges = next(c[1] for c in table.ws.calls if c[0] == 'batch_get')
                    self.assertIn('C2:C10', ranges)
                    secondary = table.source['approved_duplicate_headers']['Email Address'][1]
                    self.assertFalse(any(span.startswith(secondary+'2:') for span in ranges))
                    self.assertFalse(any(c[0] in ('batch_update','update','add_cols') for c in table.ws.calls))

    def test_exception_is_explicit_exact_and_cannot_change_recipient_source(self):
        for source_id in APPROVED_RANGES:
            source, header = reviewed_source(source_id)
            del source['approved_duplicate_headers']
            with self.assertRaisesRegex(SheetIntegrityError, 'Duplicate'):
                ProductionTable(Settings(), source, Worksheet(header)).validate_header(header)
            for bad in ({'Email Address':['C','G']}, {'Email Address':['C','E','F']},
                        {'Confirmation':['A','B']}, {}, {'Email':['C','E']}):
                source, header = reviewed_source(source_id)
                source['approved_duplicate_headers'] = bad
                with self.subTest(source=source_id, policy=bad), self.assertRaises(SheetIntegrityError):
                    ProductionTable(Settings(), source, Worksheet(header))
            source, header = reviewed_source(source_id)
            source['fields']['email']['column'] = source['approved_duplicate_headers']['Email Address'][1]
            with self.assertRaises(SheetIntegrityError):
                ProductionTable(Settings(), source, Worksheet(header))

    def test_missing_moved_renamed_or_extra_email_duplicate_stops_before_rows(self):
        for source_id in APPROVED_RANGES:
            for change in ('missing', 'moved', 'renamed', 'extra'):
                table = table_for(source_id)
                header = table.ws.values[0]
                secondary = column_number(table.source['approved_duplicate_headers']['Email Address'][1])-1
                if change == 'missing': header[secondary] = ''
                elif change == 'renamed': header[secondary] = 'Unreviewed email label'
                elif change == 'moved': header[secondary], header[6] = header[6], header[secondary]
                else: header.append('Email Address')
                with self.subTest(source=source_id, change=change), self.assertRaises(SheetIntegrityError):
                    table.rows(require_metadata=True)
                self.assertFalse(any(c[0] == 'batch_get' for c in table.ws.calls))

    def test_unexpected_duplicate_headers_are_still_rejected(self):
        for source_id in APPROVED_RANGES:
            for label in ('Confirmation', 'Synthetic question 1', 'event_id', 'ticket_tier'):
                table = table_for(source_id)
                table.ws.values[0].append(label)
                with self.subTest(source=source_id, label=label), self.assertRaises(SheetIntegrityError):
                    table.rows(require_metadata=True)
                self.assertFalse(any(c[0] == 'batch_get' for c in table.ws.calls))

    def test_all_mapped_fields_and_four_approvals_keep_exact_columns_and_labels(self):
        for source_id in APPROVED_RANGES:
            for key in ('name','email','tier',*APPROVAL_HEADERS):
                for change in ('missing','moved','renamed'):
                    table = table_for(source_id)
                    index = column_number(table.mapping[key][1])-1
                    header = table.ws.values[0]
                    if change == 'missing': header[index] = ''
                    elif change == 'renamed': header[index] = 'Unexpected field'
                    else: header[index], header[1] = header[1], header[index]
                    with self.subTest(source=source_id, field=key, change=change), self.assertRaises(SheetIntegrityError):
                        table.rows(require_metadata=True)
                    self.assertFalse(any(c[0] == 'batch_get' for c in table.ws.calls))

    def test_all_24_tracking_headers_are_unique_and_in_approved_order(self):
        for source_id in APPROVED_RANGES:
            start = APPROVED_RANGES[source_id][0]
            for offset, label in enumerate(PRODUCTION_METADATA):
                for change in ('missing','moved','duplicated'):
                    table = table_for(source_id)
                    header = table.ws.values[0]
                    if change == 'missing': header[start+offset] = ''
                    elif change == 'duplicated': header.append(label)
                    else:
                        other = start + (offset+1) % 24
                        header[start+offset], header[other] = header[other], header[start+offset]
                    with self.subTest(source=source_id, field=label, change=change), self.assertRaises(SheetIntegrityError):
                        table.rows(require_metadata=True)
                    self.assertFalse(any(c[0] == 'batch_get' for c in table.ws.calls))

    def test_approved_snapshot_load_and_exact_unmapped_text_validation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            entries, sources = [], []
            for source_id in APPROVED_RANGES:
                source, original = reviewed_source(source_id, tracking=False)
                sources.append(source)
                entries.append({'source':source, 'original_headers':original, 'range':APPROVED_RANGES[source_id][1]})
            plan = {'version':1, 'event_id':EVENT_ID, 'headers':list(PRODUCTION_METADATA), 'sources':entries}
            manifest, approved = root/'sources.json', root/'approved.json'
            manifest.write_text(json.dumps({'version':1,'sources':sources}))
            approved.write_text(json.dumps(plan))
            credential = root/'fake-credential.json'; credential.write_text('{}')
            settings = Settings(root=root, profile='production', event_id=EVENT_ID, sources_file=manifest,
                                service_account=credential, email_enabled=False)
            self.assertEqual(load_plan(settings, approved), plan)
            for entry in plan['sources']:
                snapshot = {'title':entry['source']['tab_name'], 'tab_id':entry['source']['tab_id'],
                            'headers':entry['original_headers']+list(PRODUCTION_METADATA), 'column_count':60}
                self.assertEqual(classify(entry,snapshot),'initialized')
                snapshot['headers'][17] = 'Unexpected change to an unmapped question'
                with self.assertRaises(SheetIntegrityError): classify(entry,snapshot)
            changed = deepcopy(plan)
            changed['sources'][0]['source']['approved_duplicate_headers'] = {'Email Address':['C','F']}
            approved.write_text(json.dumps(changed))
            with self.assertRaises(ValueError): load_plan(settings, approved)

    def test_tier_wording_does_not_change_aliases_prices_or_eligibility(self):
        source, _ = reviewed_source('discount_10')
        self.assertEqual(source['fields']['tier']['header'], TIER_HEADER)
        self.assertEqual(source['price_category'], '10_percent_discount')
        self.assertEqual(resolve_tier('discount_10','Pro'),'Pro')
        self.assertIsNone(resolve_tier('discount_10', TIER_HEADER))
        self.assertTrue(valid_details(Row(2,{'name':'Synthetic Name','email':'synthetic@example.invalid',
                                            'tier':'Pro','_price_category':source['price_category']},'discount_10')))
