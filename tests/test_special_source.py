"""Four-source offline policy, actual badge/MIME, header and exact-plan checks."""
import argparse
import itertools
import json
import tempfile
import unittest
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from event_checkin import authorized_batch as batch, pilot
from event_checkin.cards import CardRenderer
from event_checkin.config import ConfigurationError, ROOT
from event_checkin.confirmation_email import build_confirmation_message
from event_checkin.controlled_render import verify_render
from event_checkin.final_badge_checks import final_settings
from event_checkin.google_reads import ReadScheduler
from event_checkin.header_init import HeaderInitializer, load_plan
from event_checkin.production import preview, private_json
from event_checkin.production_cli import register
from event_checkin.production_sheets import (APPROVAL_HEADERS, FixtureSheets, PRODUCTION_METADATA,
                                           ProductionTable, source_mapping)
from event_checkin.sheets import SheetIntegrityError
from event_checkin.source_config import BASE_SOURCES, SOURCES
from event_checkin.special_source import prepare, special_source
from event_checkin.tiers import resolve_tier
from tests import test_pilot
from tests.sheet_transport import FakeClock, SheetsAPI
from tests.test_header_init import FakeTransport
from tests.test_production import Worksheet, record


def fixture():
    return json.loads((ROOT/'examples/production-registrations-special.json').read_text())


class SpecialPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.s = replace(final_settings(self.tmp.name), profile='production', event_id='synthetic-special',
                         runtime=Path(self.tmp.name)/'runtime')

    def test_both_exact_labels_require_every_approval_without_inference(self):
        for raw, expected in (('Special Ticket (₱180)', 'Special Ticket'), ('Pass', 'Pass')):
            for combo in itertools.product((False, True), repeat=4):
                data = fixture()
                row = data['sources']['special'][0]
                row['tier'] = raw
                row.update(dict(zip(APPROVAL_HEADERS, combo)))
                data['sources']['special'] = [row]
                sheet = FixtureSheets(data)
                before = deepcopy(sheet.data)
                report, plan = preview(self.s, sheet, source='special', limit='all')
                self.assertEqual(report[0]['mapped_tier'], expected)
                self.assertEqual(bool(plan['entries']), all(combo))
                self.assertEqual(sheet.data, before)
            for key in APPROVAL_HEADERS:
                for value in ('', None, 'FALSE', 'false', 'true', 1, 'TRUE '):
                    data = fixture()
                    data['sources']['special'] = [dict(data['sources']['special'][0], tier=raw, **{key: value})]
                    self.assertFalse(preview(self.s, FixtureSheets(data), source='special')[1]['entries'])
        for raw in ('Special Ticket', 'Special Ticket (₱180) ', 'special ticket (₱180)', 'pass', 'Pass ', 'Lite', 'Pro'):
            self.assertIsNone(resolve_tier('special', raw))
        for source in BASE_SOURCES:
            for raw in ('Special Ticket (₱180)', 'Special Ticket', 'Pass'):
                self.assertIsNone(resolve_tier(source, raw))

    def test_optional_source_order_all_selection_and_cross_source_duplicates(self):
        sheet = FixtureSheets(fixture())
        report, plan = preview(self.s, sheet, all_sources=True, limit='all')
        self.assertEqual(plan['source_order'], list(SOURCES))
        self.assertEqual([e['source'] for e in plan['entries']], ['regular', 'special', 'special'])
        self.assertEqual(plan['tracking_scope']['targets'], ['regular:row:2', 'special:row:2', 'special:row:3'])
        for source in BASE_SOURCES:
            data = fixture()
            data['sources'][source] = [record(name='Conflicting Synthetic Person', email='special@example.invalid',
                                             _price_category=source)]
            report, plan = preview(self.s, FixtureSheets(data), source='special', limit='all')
            self.assertFalse(report[0]['in_batch'])
            self.assertIn('conflicting approved', str(report[0]['blocking_reasons']))
            self.assertEqual([e['snapshot_row'] for e in plan['entries']], [3])
        data = fixture()
        del data['sources']['special']
        data['source_order'] = list(BASE_SOURCES)
        self.assertEqual(preview(self.s, FixtureSheets(data), all_sources=True)[1]['source_order'], list(BASE_SOURCES))
        with self.assertRaisesRegex(SheetIntegrityError, 'not configured'):
            preview(self.s, FixtureSheets(data), source='special')
        parser = argparse.ArgumentParser()
        register(parser.add_subparsers(dest='command'))
        args = parser.parse_args(['batch-preview', '--fixture', 'synthetic.json', '--source', 'special'])
        self.assertEqual(args.source, 'special')

    def test_delivery_is_always_C_even_when_F_disagrees_or_C_is_blank(self):
        source = special_source('synthetic-book', 'Full Name')
        headers = ['Synthetic question '+str(i) for i in range(21)]
        for label, column in source_mapping(source).values():
            from event_checkin.production_sheets import column_number
            headers[column_number(column)-1] = label
        headers[5] = 'Email Address'
        headers += list(PRODUCTION_METADATA)
        for address in ('delivery-c@example.invalid', '', 'invalid-address'):
            values = ['']*len(headers)
            values[2], values[3], values[5], values[12] = address, 'Synthetic Special', 'never-deliver-f@example.invalid', 'Pass'
            for index in (0, 18, 19, 20): values[index] = True
            ws = Worksheet(headers, [values])
            table = ProductionTable(self.s, source, ws)
            row = table.rows(require_metadata=True)[0]
            self.assertEqual(row.get('email'), address)
            self.assertEqual(row.get('tier'), 'Pass')
            ranges = next(c[1] for c in ws.calls if c[0] == 'batch_get')
            self.assertIn('C2:C10', ranges)
            self.assertFalse(any(r.startswith('F2:') for r in ranges))
            data = fixture()
            data['sources']['special'] = [row.data]
            self.assertEqual(bool(preview(self.s, FixtureSheets(data), source='special')[1]['entries']),
                             address == 'delivery-c@example.invalid')
            self.assertEqual(ws.values[1][5], 'never-deliver-f@example.invalid')
        for change in ('missing', 'third', 'moved', 'fallback'):
            changed_source, changed_headers = deepcopy(source), list(headers)
            if change == 'missing': changed_headers[5] = 'Other'
            if change == 'third': changed_headers[7] = 'Email Address'
            if change == 'moved': changed_headers[5], changed_headers[6] = changed_headers[6], changed_headers[5]
            if change == 'fallback': changed_source['fields']['email']['column'] = 'F'
            with self.assertRaises(SheetIntegrityError):
                ProductionTable(self.s, changed_source, Worksheet(changed_headers, [])).header()


class SpecialPipelineTests(unittest.TestCase):
    def setUp(self):
        self.f = test_pilot.PilotTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.s = self.f.s
        sources = json.loads((ROOT/'config/sources.production.example.json').read_text())['sources']
        sources.append(special_source('synthetic-special', 'Full Name'))
        self.clock = FakeClock()
        self.api = SheetsAPI(self.f, self.clock, sources=sources)
        self.f.reader.sheet = FixtureSheets(fixture())
        self.f.reader.source_order = list(SOURCES)
        self.f.reader.dataset = self.f.plan['dataset']
        self.plan = preview(self.s, self.f.reader, source='special', limit='all')[1]
        self.path = self.s.runtime/'batch-reviews/special-exact.json'
        private_json(self.path, self.plan)
        self.policy_path = batch.prepare(self.s, self.path)
        self.scope = batch.PlanAuthorization()

    def execute(self, **kwargs):
        return pilot.execute(self.s, self.path, authorization=self.scope, reader=self.f.reader,
            renderer=self.f.renderer, write_transport=self.f.reader.write, reload_settings=lambda: self.s, **kwargs)

    def enable_synthetic(self):
        policy = json.loads(self.policy_path.read_text())
        policy['enabled'] = True
        private_json(self.policy_path, policy)
        self.s.email_enabled = True  # Isolated fake settings and fake SMTP only.

    def test_exact_authorized_special_plan_and_mime_preserve_all_other_sources(self):
        encoded = self.path.read_bytes()
        self.assertFalse(json.loads(self.policy_path.read_text())['enabled'])
        with self.assertRaises(ConfigurationError): self.execute()
        extra = record(name='Synthetic Later Registration', email='later@example.invalid',
                       tier='Pass', _price_category='special')
        self.f.reader.sheet.data['sources']['special'].append(extra)
        before = deepcopy(self.f.reader.sheet.data)
        self.enable_synthetic()
        policy = self.policy_path.read_bytes()
        self.assertEqual(self.execute(), ['sent', 'sent'])
        self.assertEqual(self.execute(), ['completed previously']*2)
        self.assertEqual(len(self.f.smtp.messages), 2)
        self.assertEqual(self.path.read_bytes(), encoded)
        self.assertEqual(self.policy_path.read_bytes(), policy)
        for source in BASE_SOURCES:
            self.assertEqual(self.f.reader.sheet.data['sources'][source], before['sources'][source])
        self.assertEqual(self.f.reader.sheet.data['sources']['special'][2], extra)
        for index, label in enumerate(('Special Ticket', 'Pass')):
            row = self.f.reader.sheet.data['sources']['special'][index]
            self.assertEqual(row['ticket_tier'], label)
            self.assertEqual(pilot.existing(self.s, self.plan, self.plan['entries'][index])[1][0], 'ACCEPTED')
            self.assertIn(label, self.f.smtp.messages[index].get_body(preferencelist=('html',)).get_content())
            self.assertEqual(str(self.f.smtp.messages[index]['To']), before['sources']['special'][index]['email'])
            for key in APPROVAL_HEADERS:
                self.assertEqual(row[key], before['sources']['special'][index][key])

    def test_special_is_read_and_schema_checked_even_for_a_regular_target(self):
        scheduler = ReadScheduler(clock=self.clock.monotonic, sleep=self.clock.sleep, progress=lambda _: None)
        with patch('gspread.service_account', side_effect=self.api.client):
            reader = pilot.PilotReader(self.s, read_scheduler=scheduler)
            self.assertEqual(reader.source_order, list(SOURCES))
            rows = reader.rows()
            self.assertEqual({r.source_id for r in rows}, {'regular', 'special'})
            # The header and data adapter retain empty configured sources too.
            self.assertEqual(preview(self.s, reader, all_sources=True)[1]['source_order'], list(SOURCES))
            self.api.entries[-1]['original_headers'][12] = 'Changed choice header'
            with self.assertRaises(SheetIntegrityError):
                preview(self.s, reader, source='regular')
        self.assertTrue(all(c['method'] == 'get' for c in self.api.calls))
        self.assertFalse(self.f.smtp.messages)

    def test_changed_selection_cannot_be_rebound_or_replace_approved_targets(self):
        self.enable_synthetic()
        before = self.path.read_bytes()
        self.path.write_bytes(before+b'\n')
        with self.assertRaises(ConfigurationError): self.execute()
        self.path.write_bytes(before)
        self.f.reader.sheet.data['sources']['special'][0]['tier'] = 'Pass'
        with self.assertRaises(ConfigurationError): self.execute()
        self.assertFalse(self.f.smtp.messages)
        self.assertFalse(self.f.renderer.calls)
        self.assertFalse((self.s.runtime/'production-batches.sqlite3').exists())

    def test_candidate_audit_and_exact_header_initialization_are_offline_only(self):
        # Preparation starts from the three-source active plan, which remains
        # byte-identical; the fake audit supplies headers for the fourth tab.
        original = json.loads((self.s.root/'config/production-header-init.approved.json').read_text())
        special = original['sources'].pop()
        for e in original['sources']:
            e['source']['spreadsheet_id'] = 'synthetic-shared-book'
        private_json(self.s.sources_file, {'version': 1, 'sources': [e['source'] for e in original['sources']]})
        active = self.s.root/'config/production-header-init.approved.json'
        private_json(active, original)
        base = self.s.sources_file.read_bytes(), active.read_bytes(), self.policy_path.read_bytes()
        folder = self.s.runtime/'source-candidates/special'
        report = prepare(self.s, folder)
        self.assertIsNone(report['tracking_destination_blank'])
        with self.assertRaises(ConfigurationError):
            load_plan(replace(self.s, sources_file=Path(report['manifest'])), report['header_plan'])
        def factory(settings, plan, apply):
            self.assertFalse(apply)
            fake = FakeTransport(plan)
            for e in plan['sources']:
                key = e['source']['id']
                fake.snapshots[key]['headers'] = (special['original_headers'] if key == 'special'
                    else e['original_headers']+list(PRODUCTION_METADATA))
                fake.snapshots[key]['column_count'] = 60
            return fake
        report = prepare(self.s, folder, audit=True, transport_factory=factory)
        self.assertTrue(report['tracking_destination_blank'])
        self.assertEqual((self.s.sources_file.read_bytes(), active.read_bytes(), self.policy_path.read_bytes()), base)
        candidate_settings = replace(self.s, sources_file=Path(report['manifest']))
        plan = load_plan(candidate_settings, report['header_plan'])
        self.assertEqual([e['source']['id'] for e in plan['sources']], list(SOURCES))
        self.assertEqual(plan['sources'][-1]['range'], 'V1:AS1')
        self.assertEqual({e['source']['spreadsheet_id'] for e in plan['sources']}, {'synthetic-shared-book'})
        fake = factory(candidate_settings, plan, False)
        result = HeaderInitializer(candidate_settings, plan, fake).run(apply=True)
        self.assertEqual([e for e in fake.history if e[0] == 'write'], [('write', 'special')])
        area = fake.payloads[0][-1]['updateCells']['range']
        self.assertEqual((area['startColumnIndex'], area['endColumnIndex']), (21, 45))
        self.assertEqual(result[-1]['status'], 'written and verified')
        self.assertEqual(Path(report['manifest']).stat().st_mode & 0o777, 0o600)
        self.assertEqual(Path(report['header_plan']).stat().st_mode & 0o777, 0o600)
        # Formula-looking blank destinations and partial tracking never pass.
        for extra in ('=""', 'unexpected', PRODUCTION_METADATA[0]):
            def occupied(settings, plan, apply):
                fake = factory(settings, plan, apply)
                fake.snapshots['special']['headers'] = special['original_headers']+[extra]
                return fake
            with self.assertRaises((ConfigurationError, SheetIntegrityError)):
                prepare(self.s, folder, audit=True, transport_factory=occupied)
        self.assertEqual((self.s.sources_file.read_bytes(), active.read_bytes(), self.policy_path.read_bytes()), base)
        # An enabled single-entry policy also prevents candidate preparation;
        # preparation never deactivates or rewrites that existing policy.
        single = self.s.root/'config/single-entry-send.approved.json'
        private_json(single, {'enabled': True})
        with self.assertRaises(ConfigurationError):
            prepare(self.s, folder, audit=True, transport_factory=factory)
        self.assertEqual(json.loads(single.read_text()), {'enabled': True})


class ActualSpecialBadges(unittest.TestCase):
    def test_actual_badge_pixels_qr_and_html_for_both_labels(self):
        if not Path(final_settings('/tmp').bun).is_file() or not (ROOT/'freshcoat/node_modules').exists():
            self.skipTest('Bun or FreshCoat dependencies unavailable')
        with tempfile.TemporaryDirectory() as temp:
            settings = final_settings(temp)
            original = settings.template.read_bytes()
            for index, label in enumerate(('Special Ticket', 'Pass')):
                pid = 'SYNTHETIC-SPECIAL-'+str(index)
                name = 'Synthetic '+label
                token = 'TEST-NOT-VALID-FOR-ADMISSION-'+pid
                card = CardRenderer(settings).render(pid, name, token, ticket_tier=label)
                self.assertTrue(verify_render(settings, card, name, label, token)['qr_decoded'])
                message = build_confirmation_message('synthetic@example.invalid', name, pid, card,
                    '<synthetic-%d@example.invalid>' % index, ticket_tier=label)
                self.assertIn(label, message.get_body(preferencelist=('html',)).get_content())
                self.assertIn('Pass type: '+label, message.get_body(preferencelist=('plain',)).get_content())
            self.assertEqual(settings.template.read_bytes(), original)
