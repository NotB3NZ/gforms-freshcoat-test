import io
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import Mock, patch

from event_checkin.config import Settings, ROOT, ConfigurationError
from event_checkin.header_init import (
    EVENT_ID, APPROVED_RANGES, HeaderInitializer, HeaderTransport,
    approved_requests, classify, load_plan,
)
from event_checkin.production_sheets import PRODUCTION_METADATA, source_mapping, column_number
from event_checkin.sheets import SheetIntegrityError


class FakeTransport:
    def __init__(self, plan):
        self.plan = plan
        self.snapshots = {e['source']['id']: {
            'title': e['source']['tab_name'], 'tab_id': e['source']['tab_id'],
            'column_count': 27 if e['source']['id'] == 'regular' else 24,
            'headers': deepcopy(e['original_headers'])} for e in plan['sources']}
        self.history, self.payloads = [], []
        self.hook, self.fail_before, self.fail_after = None, None, None
        # Unread form rows, checkbox states, notes and formatting must survive.
        self.protected = {'participant_rows': [['private fake response', True, False]],
                          'formats': {'A1': 'staff checkbox format', 'X1': 'existing style'},
                          'notes': {'X1': 'existing note'}, 'validations': {'A2': 'CHECKBOX'}}

    def read(self, entry):
        key = entry['source']['id']
        self.history.append(('read', key))
        if self.hook:
            self.hook(self)
        return deepcopy(self.snapshots[key])

    def write(self, entry, snapshot):
        key = entry['source']['id']
        self.history.append(('write', key))
        if self.fail_before == key:
            self.fail_before = None
            raise ConnectionError('Fake request failed before commit')
        self.payloads.append(approved_requests(entry, snapshot['column_count']))
        self.snapshots[key]['column_count'] = max(snapshot['column_count'], len(entry['original_headers']) + 24)
        self.snapshots[key]['headers'] = entry['original_headers'] + list(PRODUCTION_METADATA)
        if self.fail_after == key:
            self.fail_after = None
            raise ConnectionError('Fake response lost after atomic commit')


class HeaderInitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        sources = json.loads((ROOT / 'config/sources.production.example.json').read_text())['sources']
        entries = []
        for source in sources:
            source['spreadsheet_id'] = 'synthetic-' + source['id']
            count, span = APPROVED_RANGES[source['id']]
            headers = ['Question %d' % i for i in range(count)]
            for label, col in source_mapping(source).values():
                headers[column_number(col) - 1] = label
            entries.append({'source': source, 'range': span, 'original_headers': headers})
        self.plan = {'version': 1, 'event_id': EVENT_ID, 'headers': list(PRODUCTION_METADATA), 'sources': entries}
        self.path = root / 'approved.json'
        self.path.write_text(json.dumps(self.plan))
        manifest = root / 'sources.json'
        manifest.write_text(json.dumps({'version': 1, 'sources': sources}))
        credentials = root / 'fake-credentials.json'
        credentials.write_text('{}')
        self.s = Settings(root=root, profile='production', event_id=EVENT_ID, email_enabled=False,
                          runtime=root / 'runtime-production', sources_file=manifest, service_account=credentials)
        self.transport = FakeTransport(self.plan)

    def tearDown(self):
        self.temp.cleanup()

    def run_init(self, apply=True):
        return HeaderInitializer(self.s, self.plan, self.transport).run(apply=apply)

    def test_exact_payload_masks_preserve_all_other_cell_fields(self):
        original = deepcopy(self.transport.protected)
        report = self.run_init()
        self.assertEqual([r['grid_columns_added'] for r in report], [20, 21, 21])
        self.assertEqual([r['range'] for r in report], ['X1:AU1', 'V1:AS1', 'V1:AS1'])
        self.assertEqual(original, self.transport.protected)
        for entry, payload in zip(self.plan['sources'], self.transport.payloads):
            resize, values = payload
            self.assertEqual(resize['updateSheetProperties']['fields'], 'gridProperties.columnCount')
            update = values['updateCells']
            self.assertEqual(update['fields'], 'userEnteredValue')
            self.assertEqual(update['range'], {'sheetId': entry['source']['tab_id'], 'startRowIndex': 0, 'endRowIndex': 1,
                                              'startColumnIndex': len(entry['original_headers']),
                                              'endColumnIndex': len(entry['original_headers']) + 24})
            self.assertEqual(update['rows'], [{'values': [{'userEnteredValue': {'stringValue': h}} for h in PRODUCTION_METADATA]}])

    def test_dry_run_reads_all_sources_without_writes(self):
        report = self.run_init(apply=False)
        self.assertTrue(all(r['status'] == 'validated; no writes' for r in report))
        self.assertEqual(self.transport.payloads, [])
        self.assertEqual({key for method, key in self.transport.history}, set(APPROVED_RANGES))

    def test_rerun_never_adds_duplicate_columns_or_rewrites_headers(self):
        self.run_init()
        original = deepcopy(self.transport.snapshots)
        before = len(self.transport.payloads)
        report = self.run_init()
        self.assertEqual(before, len(self.transport.payloads))
        self.assertEqual(original, self.transport.snapshots)
        self.assertTrue(all(r['status'] == 'already initialized; unchanged' for r in report))

    def test_no_grid_expansion_when_already_large_enough(self):
        for value in self.transport.snapshots.values():
            value['column_count'] = 60
        report = self.run_init()
        self.assertTrue(all(r['grid_columns_added'] == 0 for r in report))
        self.assertTrue(all(len(payload) == 1 and 'updateCells' in payload[0] for payload in self.transport.payloads))

    def test_missing_access_to_any_source_prevents_first_write(self):
        def hook(transport):
            if transport.history[-1] == ('read', 'discount_10'):
                raise ConnectionError('Fake missing source')
        self.transport.hook = hook
        with self.assertRaises(ConnectionError):
            self.run_init()
        self.assertFalse(self.transport.payloads)

    def test_changed_original_headers_mappings_or_destinations_stop_all_writes(self):
        base = deepcopy(self.transport.snapshots)
        for change in ('original', 'approval', 'duplicate', 'destination', 'formula', 'partial', 'extra', 'tab'):
            with self.subTest(change=change):
                self.transport.snapshots = deepcopy(base)
                row = self.transport.snapshots['discount_15']
                if change == 'original':
                    row['headers'][4] = 'Different original question'
                elif change == 'approval':
                    row['headers'][0] = 'Different Confirmation'
                elif change == 'duplicate':
                    row['headers'][12] = row['headers'][11]
                elif change == 'tab':
                    row['title'] = 'Changed tab'
                elif change == 'destination':
                    row['headers'].append('Existing content')
                elif change == 'formula':
                    row['headers'].append('=""')
                elif change == 'partial':
                    row['headers'] += list(PRODUCTION_METADATA[:2])
                else:
                    row['headers'] += [''] * 24 + ['Other far-right header']
                with self.assertRaises(SheetIntegrityError):
                    self.run_init()
                self.assertFalse(self.transport.payloads)

    def test_revalidates_all_sources_immediately_before_first_write(self):
        def hook(transport):
            if len(transport.history) == 4:
                transport.snapshots['discount_10']['headers'][4] = 'Changed after initial preflight'
        self.transport.hook = hook
        with self.assertRaises(SheetIntegrityError):
            self.run_init()
        self.assertFalse(self.transport.payloads)

    def test_target_change_after_global_revalidation_stops_write(self):
        def hook(transport):
            if len(transport.history) == 7:
                transport.snapshots['regular']['headers'].append('Competing destination content')
        self.transport.hook = hook
        with self.assertRaises(SheetIntegrityError):
            self.run_init()
        self.assertFalse(self.transport.payloads)

    def test_post_write_readback_error_stops_before_next_sheet(self):
        def hook(transport):
            if transport.payloads:
                transport.snapshots['regular']['headers'][-1] = 'Unexpected result'
        self.transport.hook = hook
        with self.assertRaises(SheetIntegrityError):
            self.run_init()
        self.assertEqual(len(self.transport.payloads), 1)

    def test_lost_response_before_or_after_commit_can_resume_safely(self):
        for after in (False, True):
            with self.subTest(after=after):
                self.transport = FakeTransport(self.plan)
                if after:
                    self.transport.fail_after = 'discount_15'
                else:
                    self.transport.fail_before = 'discount_15'
                with self.assertRaises(ConnectionError):
                    self.run_init()
                self.run_init()
                self.assertEqual(len(self.transport.payloads), 3)
                self.assertTrue(all(classify(e, self.transport.snapshots[e['source']['id']]) == 'initialized' for e in self.plan['sources']))

    def test_local_plan_and_config_must_match_before_authentication(self):
        self.assertEqual(load_plan(self.s, self.path), self.plan)
        for key in ('range', 'source', 'headers'):
            with self.subTest(key=key):
                changed = deepcopy(self.plan)
                if key == 'range':
                    changed['sources'][0]['range'] = 'A1:X1'
                elif key == 'source':
                    changed['sources'][0]['source']['fields']['email']['column'] = 'E'
                else:
                    changed['headers'].reverse()
                self.path.write_text(json.dumps(changed))
                with self.assertRaises(ConfigurationError):
                    load_plan(self.s, self.path)

    def test_email_profile_and_processing_lock_guards(self):
        for attr, value in (('profile', 'test'), ('event_id', 'another-event'), ('email_enabled', True)):
            old = getattr(self.s, attr)
            setattr(self.s, attr, value)
            with self.assertRaises(ConfigurationError):
                self.run_init()
            setattr(self.s, attr, old)
        with patch.object(self.s, 'require_operational', return_value=None):
            with self.assertRaises(ConfigurationError):
                self.run_init()
        self.assertFalse(self.transport.history)
        with self.assertRaises(ConfigurationError):
            self.s.require_operational()
        with self.assertRaises(ConfigurationError):
            self.s.require_email()

    def test_transport_only_reads_metadata_and_formula_row_one_and_exact_atomic_write(self):
        entry = self.plan['sources'][0]
        with patch('gspread.service_account') as auth, patch('event_checkin.header_init.time.sleep'):
            http = auth.return_value.http_client
            http.fetch_sheet_metadata.return_value = {'spreadsheetId': entry['source']['spreadsheet_id'], 'sheets': [
                {'properties': {'sheetId': entry['source']['tab_id'], 'title': entry['source']['tab_name'], 'gridProperties': {'columnCount': 27}}}]}
            http.values_get.return_value = {'values': [entry['original_headers']]}
            transport = HeaderTransport(self.s, self.plan, apply=True)
            snapshot = transport.read(entry)
            transport.write(entry, snapshot)
            self.assertEqual(auth.call_args.kwargs['scopes'], ['https://www.googleapis.com/auth/spreadsheets'])
            self.assertEqual(http.values_get.call_args.args, (entry['source']['spreadsheet_id'], "'Participants'!1:1"))
            self.assertEqual(http.values_get.call_args.kwargs['params'], {'valueRenderOption': 'FORMULA'})
            self.assertEqual(http.fetch_sheet_metadata.call_args.kwargs['params']['includeGridData'], 'false')
            self.assertEqual(http.batch_update.call_args.args, (entry['source']['spreadsheet_id'], {'requests': approved_requests(entry, 27)}))
            self.assertEqual([call[0] for call in http.method_calls], ['fetch_sheet_metadata', 'values_get', 'batch_update'])
            with self.assertRaises(SheetIntegrityError):
                transport.write({**entry, 'range': 'A1:X1'}, snapshot)

    def test_read_only_transport_never_writes_and_uses_read_only_scope(self):
        with patch('gspread.service_account') as auth:
            transport = HeaderTransport(self.s, self.plan)
            with self.assertRaises(SheetIntegrityError):
                transport.write(self.plan['sources'][0], self.transport.snapshots['regular'])
            auth.return_value.http_client.batch_update.assert_not_called()
            self.assertEqual(auth.call_args.kwargs['scopes'], ['https://www.googleapis.com/auth/spreadsheets.readonly'])

    def test_cli_preview_apply_never_build_participant_renderer_or_smtp(self):
        from event_checkin.__main__ import main
        for apply in (False, True):
            argv = ['event_checkin', '--profile', 'production', 'production-init-headers', '--plan', str(self.path)]
            if apply:
                argv.append('--apply')
            with patch('sys.argv', argv), patch('event_checkin.__main__.load_settings', return_value=self.s), \
                    patch('event_checkin.header_init.HeaderTransport', return_value=self.transport), \
                    patch('event_checkin.production_cli.ProductionSheets') as participants, \
                    patch('event_checkin.production_cli.CardRenderer') as renderer, \
                    patch('event_checkin.production_cli.SMTPMailer') as mailer, \
                    redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()):
                self.assertIsNone(main())
                self.assertIn('LOCKED', output.getvalue())
                participants.assert_not_called()
                renderer.assert_not_called()
                mailer.assert_not_called()


if __name__ == '__main__':
    unittest.main()
