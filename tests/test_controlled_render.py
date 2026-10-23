import itertools
import io
import json
import sqlite3
import tempfile
import unittest
import uuid
from argparse import Namespace
from contextlib import redirect_stdout
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from unittest.mock import Mock

from event_checkin.checkin import CheckinService
from event_checkin.config import ROOT, Settings, ConfigurationError
from event_checkin.controlled_render import (
    ControlledRenderTest, ControlledTestBlocked, archive_invalid, assert_unissued,
    changed_snapshot, literal, recover, verify_render, LiveTestSheet,
)
from event_checkin.production_sheets import APPROVAL_HEADERS, PRODUCTION_METADATA
from event_checkin.sheets import Row
from event_checkin.test_identity import TEST_TOKEN_PREFIX


def registration(**changes):
    data = {key: '' for key in PRODUCTION_METADATA}
    data.update(name='Synthetic Person', email='synthetic@example.test', tier='Lite (₱170)',
                _price_category='15_percent_discount', **{key: True for key in APPROVAL_HEADERS})
    data.update(changes)
    return data


class FakeSheet:
    def __init__(self):
        self.data = {'regular': [registration(name='Other Person', email='other@example.test', tier='Pro', _price_category='regular')],
                     'discount_15': [registration()], 'discount_10': []}
        self.writes = []
        self.raw_overrides = {}
        self.before_write = None
        self.after_write = None
        self.fail_read = False

    def rows(self):
        if self.fail_read:
            raise ConnectionError('synthetic unavailable')
        return [Row(i, deepcopy(data), source) for source, rows in self.data.items() for i, data in enumerate(rows, 2)]

    def capture(self, source, number):
        data = self.data[source][number - 2]
        keys = ['name', 'email', 'tier'] + list(APPROVAL_HEADERS) + list(PRODUCTION_METADATA)
        values = {}
        for key in keys:
            value = data.get(key, '')
            values[key] = ({} if value == '' else {'boolValue': value} if isinstance(value, bool) else literal(value))
        values.update({key: value for (s, n, key), value in self.raw_overrides.items() if s == source and n == number})
        return {'source': source, 'row': number, 'headers': {'synthetic': True},
                'cells': dict(zip(keys, range(1, len(keys) + 1))), 'values': values}

    def write(self, expected, changes):
        if self.capture(expected['source'], expected['row']) != expected:
            raise ControlledTestBlocked('Concurrent change')
        if self.before_write:
            self.before_write(changes)
        self.writes.append(deepcopy(changes))
        data = self.data[expected['source']][expected['row'] - 2]
        for key, raw in changes.items():
            data[key] = next(iter(raw.values())) if raw else ''
            self.raw_overrides.pop((expected['source'], expected['row'], key), None)
            if 'formulaValue' in raw:
                self.raw_overrides[(expected['source'], expected['row'], key)] = raw
                data[key] = 'computed synthetic value'
        if self.after_write:
            self.after_write(changes)


class Renderer:
    def __init__(self, directory):
        self.directory, self.calls, self.hook, self.fail = directory, [], None, False

    def render(self, pid, name, token, ticket_tier=None):
        from PIL import Image
        self.calls.append((pid, name, token, ticket_tier))
        if self.hook:
            self.hook()
        if self.fail:
            raise RuntimeError('synthetic renderer failed')
        path = self.directory / pid / 'card.png'
        path.parent.mkdir(parents=True)
        Image.new('RGB', (1200, 900), 'white').save(path)
        return path


def verified(*args):
    return {'name_pixels_verified': True, 'tier_pixels_verified': True, 'qr_decoded': True}


class ControlledRenderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.s = Settings(root=root, profile='production', event_id='aws-scd-mindanao-2026',
                          runtime=root / 'runtime-production', generated=root / 'generated-production', email_enabled=False)
        self.sheet = FakeSheet()
        self.renderer = Renderer(root / 'synthetic-render')
        self.service = ControlledRenderTest(self.s, self.sheet, self.renderer, verified)
        self.other = deepcopy(self.sheet.data['regular'])

    def tearDown(self):
        self.assertEqual(self.sheet.data['regular'], self.other)
        if not getattr(self, 'journal_created', False):
            self.assertFalse((self.s.runtime / 'production-batches.sqlite3').exists())
            self.assertFalse((self.s.runtime / 'issuance.sqlite3').exists())
        self.temp.cleanup()

    def execute(self):
        selection = self.service.prepare('discount_15', 2)
        return self.service.execute(selection)

    def test_success_restores_exact_original_and_retires_isolated_identity(self):
        original = self.sheet.capture('discount_15', 2)
        result = self.execute()
        self.assertEqual(result['test_result'], 'VERIFIED')
        self.assertEqual(result['cleanup'], 'RESTORED_AND_VERIFIED')
        self.assertEqual(result['reservation_status'], 'RETIRED')
        self.assertFalse(result['email_sent'])
        self.assertEqual(self.sheet.capture('discount_15', 2), original)
        self.assertEqual(len(self.renderer.calls), 1)
        self.assertEqual(self.renderer.calls[0][3], 'Lite')
        self.assertTrue(self.renderer.calls[0][2].startswith(TEST_TOKEN_PREFIX))
        self.assertTrue(Path(result['card_preview']).is_file())
        self.assertEqual(Path(result['card_preview']).stat().st_mode & 0o777, 0o600)
        self.assertEqual(Path(result['audit']).stat().st_mode & 0o777, 0o600)
        audit = json.loads(Path(result['audit']).read_text())
        self.assertEqual(audit['original'], original)
        self.assertEqual(audit['reservation_status'], 'RETIRED')
        for changes in self.sheet.writes:
            self.assertLessEqual(set(changes), set(PRODUCTION_METADATA))
            if changes.get('ticket_status'):
                self.assertEqual(changes['ticket_status'], literal('CANCELLED'))
        # Normal processing eligibility is identical after cleanup.
        assert_unissued(self.sheet.rows()[1], self.sheet.rows(), self.s.event_id)

    def test_all_16_approval_combinations_fail_before_any_write_except_all_true(self):
        for combo in itertools.product((True, False), repeat=4):
            self.sheet.data['discount_15'][0].update(zip(APPROVAL_HEADERS, combo))
            if all(combo):
                self.service.prepare('discount_15', 2)
            else:
                with self.assertRaises(ControlledTestBlocked):
                    self.service.prepare('discount_15', 2)
        self.assertFalse(self.sheet.writes)

    def test_unknown_tier_and_wrong_source_alias_block(self):
        for label in ('Lite (₱199)', 'Lite (₱171)', 'lite', ' Lite ', 'VIP'):
            self.sheet.data['discount_15'][0]['tier'] = label
            with self.assertRaises(ControlledTestBlocked):
                self.service.prepare('discount_15', 2)
        self.assertFalse(self.sheet.writes)

    def test_issued_or_prior_processing_history_blocks_without_mutation(self):
        for key, value in (('participant_id', 'P-existing'), ('ticket_token', 'existing'), ('state', 'READY'),
                           ('render_status', 'RENDERED'), ('email_attempts', '1'), ('checked_in_at', 'old')):
            self.sheet.data['discount_15'][0] = registration(**{key: value})
            with self.assertRaises(ControlledTestBlocked):
                self.service.prepare('discount_15', 2)
        self.assertFalse(self.sheet.writes)

    def test_ambiguous_locator_and_conflicting_duplicate_block(self):
        with self.assertRaises(ControlledTestBlocked):
            self.service.prepare('discount_15', 99)
        self.sheet.data['discount_15'].append(registration())
        with self.assertRaises(ControlledTestBlocked):
            self.service.prepare('discount_15', 2)
        self.sheet.data['discount_15'][1]['tier'] = 'Pro (₱297)'
        with self.assertRaises(ControlledTestBlocked):
            self.service.prepare('discount_15', 2)
        self.assertFalse(self.sheet.writes)

    def test_source_unavailable_blocks(self):
        self.sheet.fail_read = True
        with self.assertRaises(ConnectionError):
            self.service.prepare('discount_15', 2)
        self.assertFalse(self.sheet.writes)

    def test_saved_selection_cannot_repeat_even_after_successful_cleanup(self):
        selection = self.service.prepare('discount_15', 2)
        selection['selection_id'] = str(uuid.uuid4())
        result = self.service.execute(selection)
        self.assertEqual(result['cleanup'], 'RESTORED_AND_VERIFIED')
        writes = len(self.sheet.writes)
        with self.assertRaises(ControlledTestBlocked):
            self.service.execute(selection)
        self.assertEqual(len(self.sheet.writes), writes)
        self.assertEqual(len(self.renderer.calls), 1)

    def test_cli_prepares_private_selection_and_applies_only_once_without_pii_output(self):
        from event_checkin.controlled_render import run_command
        path = self.s.runtime / 'selection.json'
        args = Namespace(source='discount_15', row=2, selection=path, apply=False, recover=None)
        output = io.StringIO()
        with patch('event_checkin.controlled_render.LiveTestSheet', return_value=self.sheet), \
                patch('event_checkin.controlled_render.ControlledRenderTest', return_value=self.service), redirect_stdout(output):
            run_command(self.s, args)
            args.source, args.row, args.apply = None, None, True
            run_command(self.s, args)
            with self.assertRaises(ControlledTestBlocked):
                run_command(self.s, args)
        self.assertTrue(json.loads(path.read_text())['selection_id'])
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        for secret in ('Synthetic Person', 'synthetic@example.test', self.renderer.calls[0][2]):
            self.assertNotIn(secret, output.getvalue())
        self.assertEqual(len(self.renderer.calls), 1)

    def test_cli_malformed_selection_stops_before_google_access(self):
        from event_checkin.controlled_render import run_command
        cases = [Namespace(source=None, row=None, selection=None, apply=False, recover=None),
                 Namespace(source='discount_15', row=1, selection=self.s.runtime / 'bad.json', apply=False, recover=None),
                 Namespace(source=None, row=None, selection=None, apply=True, recover=None)]
        with patch('event_checkin.controlled_render.LiveTestSheet') as adapter:
            for args in cases:
                with self.assertRaises(ControlledTestBlocked):
                    run_command(self.s, args)
            adapter.assert_not_called()

    def test_changed_registration_after_selection_blocks(self):
        selection = self.service.prepare('discount_15', 2)
        self.sheet.data['discount_15'][0]['email'] = 'changed@example.test'
        with self.assertRaises(ControlledTestBlocked):
            self.service.execute(selection)
        self.assertFalse(self.sheet.writes)

    def test_approval_rechecked_after_durable_backup_before_first_write(self):
        selection = self.service.prepare('discount_15', 2)
        from event_checkin.production import private_json
        def save(path, payload):
            private_json(path, payload)
            if payload.get('reservation_status') == 'RESERVED_TEST_ONLY':
                self.sheet.data['discount_15'][0]['confirmation'] = False
        with patch('event_checkin.controlled_render.private_json', side_effect=save):
            result = self.service.execute(selection)
        self.assertFalse(self.sheet.writes)
        self.assertEqual(result['test_result'], 'STOPPED')
        self.assertEqual(result['cleanup'], 'RECOVERY_REQUIRED')

    def test_renderer_failure_restores_and_retires(self):
        original = self.sheet.capture('discount_15', 2)
        self.renderer.fail = True
        result = self.execute()
        self.assertEqual(result['test_result'], 'STOPPED')
        self.assertEqual(result['cleanup'], 'RESTORED_AND_VERIFIED')
        self.assertEqual(self.sheet.capture('discount_15', 2), original)
        self.assertEqual(result['reservation_status'], 'RETIRED')

    def test_verification_failure_still_archives_invalid_preview_and_cleans_up(self):
        self.service.verifier = lambda *args: (_ for _ in ()).throw(RuntimeError('bad QR'))
        result = self.execute()
        self.assertEqual(result['test_result'], 'STOPPED')
        self.assertEqual(result['cleanup'], 'RESTORED_AND_VERIFIED')
        self.assertTrue(Path(result['card_preview']).is_file())

    def test_original_blank_numeric_boolean_formula_tracking_values_round_trip(self):
        row = self.sheet.data['discount_15'][0]
        row.update(record_id='R-existing-unissued', last_error='old validation history', updated_at='old timestamp', duplicate_of='old-reviewed')
        # Preserve a formula as a formula, rather than its computed value.
        self.sheet.raw_overrides[('discount_15', 2, 'updated_at')] = {'formulaValue': '="old timestamp"'}
        before = self.sheet.capture('discount_15', 2)
        result = self.execute()
        self.assertEqual(result['cleanup'], 'RESTORED_AND_VERIFIED')
        self.assertEqual(self.sheet.capture('discount_15', 2), before)
        self.assertEqual(row['record_id'], 'R-existing-unissued')
        self.assertEqual(row['last_error'], 'old validation history')

    def test_concurrent_tracking_change_never_overwritten_and_recovery_is_idempotent(self):
        self.renderer.hook = lambda: self.sheet.data['discount_15'][0].update(last_error='external change')
        result = self.execute()
        self.assertEqual(result['cleanup'], 'RECOVERY_REQUIRED')
        self.assertEqual(result['reservation_status'], 'RETIRED')
        self.assertEqual(self.sheet.data['discount_15'][0]['last_error'], 'external change')
        before = len(self.sheet.writes)
        self.assertFalse(recover(self.s, self.sheet, result['audit']))
        self.assertEqual(len(self.sheet.writes), before)
        # Staff restores only their concurrent edit; cleanup can now verify the
        # earlier expected state and restore all test metadata exactly once.
        self.sheet.data['discount_15'][0]['last_error'] = ''
        self.assertTrue(recover(self.s, self.sheet, result['audit']))
        before = len(self.sheet.writes)
        self.assertTrue(recover(self.s, self.sheet, result['audit']))
        self.assertEqual(len(self.sheet.writes), before)

    def test_concurrent_form_or_checkbox_change_never_overwritten(self):
        for key, value in (('name', 'Changed Name'), ('confirmation', False)):
            self.sheet = FakeSheet()
            self.service.sheet = self.sheet
            self.renderer.hook = lambda k=key, v=value: self.sheet.data['discount_15'][0].update({k: v})
            result = self.execute()
            self.assertEqual(result['cleanup'], 'RECOVERY_REQUIRED')
            self.assertEqual(self.sheet.data['discount_15'][0][key], value)
            self.assertEqual(len(self.sheet.writes), 1)

    def test_uncertain_identity_write_before_or_after_commit_is_reconciled(self):
        for phase in ('before', 'after'):
            self.sheet = FakeSheet()
            self.service.sheet = self.sheet
            def fail(changes):
                setattr(self.sheet, phase + '_write', None)
                raise ConnectionError('uncertain write')
            setattr(self.sheet, phase + '_write', fail)
            original = self.sheet.capture('discount_15', 2)
            result = self.execute()
            self.assertEqual(result['cleanup'], 'RESTORED_AND_VERIFIED')
            self.assertEqual(self.sheet.capture('discount_15', 2), original)

    def test_uncertain_render_metadata_write_before_or_after_commit_is_reconciled(self):
        for phase in ('before', 'after'):
            self.sheet = FakeSheet()
            self.service.sheet = self.sheet
            def fail(changes):
                if 'card_path' in changes:
                    setattr(self.sheet, phase + '_write', None)
                    raise ConnectionError('uncertain render metadata write')
            setattr(self.sheet, phase + '_write', fail)
            original = self.sheet.capture('discount_15', 2)
            result = self.execute()
            self.assertEqual(result['cleanup'], 'RESTORED_AND_VERIFIED')
            self.assertEqual(self.sheet.capture('discount_15', 2), original)

    def test_uncertain_cleanup_write_after_commit_read_back_confirms_restored(self):
        def fail(changes):
            if changes.get('ticket_token') == {}:
                self.sheet.after_write = None
                raise ConnectionError('lost cleanup response')
        self.sheet.after_write = fail
        result = self.execute()
        self.assertEqual(result['cleanup'], 'RESTORED_AND_VERIFIED')

    def test_partial_write_preserves_recovery_without_blind_restore(self):
        def partial(changes):
            self.sheet.before_write = None
            self.sheet.data['discount_15'][0]['participant_id'] = changes['participant_id']['stringValue']
            raise ConnectionError('partial')
        self.sheet.before_write = partial
        result = self.execute()
        self.assertEqual(result['cleanup'], 'RECOVERY_REQUIRED')
        self.assertFalse(self.sheet.writes)
        self.assertEqual(result['reservation_status'], 'RETIRED')

    def test_test_qr_is_always_invalid_even_if_copied_as_active_ticket(self):
        service = object.__new__(CheckinService)
        for suffix in ('retired', 'pending', 'any'):
            self.assertEqual(service.check_in(TEST_TOKEN_PREFIX + suffix, 'request'),
                             {'status': 'invalid', 'request_id': 'request'})
        # No settings/adapter/journal needed: the reserved namespace fails before access.
        from event_checkin.production import reasons
        row = Row(2, registration(ticket_token=TEST_TOKEN_PREFIX + 'retired'), 'discount_15')
        self.assertIn('reserved controlled-test identity; cleanup/review required', reasons(row, [row], self.s.event_id))

    def test_original_tracking_value_types_are_preserved(self):
        for value in (0, False, '', 'old timestamp'):
            self.sheet = FakeSheet()
            self.service.sheet = self.sheet
            self.sheet.data['discount_15'][0]['updated_at'] = value
            original = self.sheet.capture('discount_15', 2)
            result = self.execute()
            self.assertEqual(result['cleanup'], 'RESTORED_AND_VERIFIED')
            self.assertEqual(self.sheet.capture('discount_15', 2), original)

    def test_existing_local_reservation_and_other_history_remain_unchanged(self):
        self.journal_created = True
        self.s.runtime.mkdir()
        path = self.s.runtime / 'production-batches.sqlite3'
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE claims (event_id TEXT,email TEXT)')
            db.execute('CREATE TABLE actions (record_id TEXT)')
            db.execute('INSERT INTO claims VALUES (?,?)', (self.s.event_id, 'other@example.test'))
        before = path.read_bytes()
        result = self.execute()
        self.assertEqual(result['cleanup'], 'RESTORED_AND_VERIFIED')
        self.assertEqual(path.read_bytes(), before)
        with sqlite3.connect(path) as db:
            db.execute('INSERT INTO claims VALUES (?,?)', (self.s.event_id, 'synthetic@example.test'))
        before = path.read_bytes()
        with self.assertRaises(ControlledTestBlocked):
            self.service.prepare('discount_15', 2)
        self.assertEqual(path.read_bytes(), before)

    def test_email_enabled_and_wrong_profile_block_before_adapter_access(self):
        for settings in (replace(self.s, email_enabled=True), replace(self.s, profile='test'), replace(self.s, event_id='other')):
            with self.assertRaises(ConfigurationError):
                ControlledRenderTest(settings, self.sheet, self.renderer)
        with self.assertRaises(ConfigurationError):
            self.s.require_operational()
        with self.assertRaises(ConfigurationError):
            self.s.require_email()


class LiveWriteBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.s = Settings(profile='production', event_id='aws-scd-mindanao-2026', email_enabled=False)
        self.sheet = object.__new__(LiveTestSheet)
        self.sheet.settings = self.s
        self.sheet.plan = {'sources': [{'source': {'id': 'discount_15', 'tab_id': 555, 'spreadsheet_id': 'synthetic-book'}}]}
        self.sheet.headers = Mock()
        self.sheet.writer = Mock()
        self.snapshot = FakeSheet().capture('discount_15', 2)
        self.sheet.capture = Mock(return_value=deepcopy(self.snapshot))

    def test_write_and_restore_only_value_fields_of_selected_metadata_cells(self):
        changes = {'ticket_token': literal(TEST_TOKEN_PREFIX + 'synthetic'), 'updated_at': {}}
        self.sheet.write(self.snapshot, changes)
        book, body = self.sheet.writer.batch_update.call_args.args
        self.assertEqual(book, 'synthetic-book')
        self.assertEqual(len(body['requests']), 2)
        for request, key in zip(body['requests'], changes):
            update = request['updateCells']
            self.assertEqual(update['fields'], 'userEnteredValue')
            self.assertEqual(update['range'], {'sheetId': 555, 'startRowIndex': 1, 'endRowIndex': 2,
                                              'startColumnIndex': self.snapshot['cells'][key] - 1,
                                              'endColumnIndex': self.snapshot['cells'][key]})
        self.assertEqual(body['requests'][1]['updateCells']['rows'], [{'values': [{}]}])
        self.assertEqual(self.sheet.capture.call_count, 2)

    def test_other_source_header_failure_stops_before_any_participant_reads(self):
        from event_checkin.sheets import SheetIntegrityError
        from event_checkin.diagnostics import safe_diagnostic
        self.sheet.plan = {'sources': [{'source': {'id': 'discount_10'}}]}
        self.sheet.reader = Mock()
        self.sheet.headers.read.return_value = {}
        with patch('event_checkin.controlled_render.classify', side_effect=SheetIntegrityError('DO_NOT_PRINT')):
            with self.assertRaises(SheetIntegrityError) as captured:
                LiveTestSheet.rows(self.sheet)
        self.assertEqual(safe_diagnostic(captured.exception)['stage'], 'validate_header_snapshot_discount_10')
        self.assertNotIn('DO_NOT_PRINT', json.dumps(safe_diagnostic(captured.exception)))
        self.sheet.reader.rows.assert_not_called()
        self.sheet.writer.batch_update.assert_not_called()

    def test_form_and_approval_writes_are_rejected(self):
        for key in ('name', 'email', 'tier', *APPROVAL_HEADERS, 'checked_in_at', 'checked_in_by', 'message_id', 'email_attempts'):
            with self.assertRaises(ControlledTestBlocked):
                self.sheet.write(self.snapshot, {key: literal('changed')})
        self.sheet.writer.batch_update.assert_not_called()

    def test_raw_snapshot_reads_only_exact_mapped_and_tracking_cells_and_keeps_formulas(self):
        from tests.test_production import ProductionAdapterTests
        from event_checkin.production_sheets import source_mapping, column_number
        fixture = ProductionAdapterTests()
        fixture.setUp()
        source = next(s for s in fixture.sources if s['id'] == 'discount_15')
        table = fixture.table(source, tracking=True)
        original_header = table.ws.values[0][:-24]
        snapshot = {'title': source['tab_name'], 'tab_id': source['tab_id'],
                    'column_count': len(table.ws.values[0]), 'headers': table.ws.values[0]}
        entry = {'source': source, 'original_headers': original_header}
        self.sheet.plan = {'sources': [entry]}
        self.sheet.headers.read = Mock(return_value=snapshot)
        positions = {key: column_number(col) for key, (_, col) in source_mapping(source).items()}
        positions.update({key: snapshot['headers'].index(key) + 1 for key in PRODUCTION_METADATA})
        grids = [{'startRow': 37, 'startColumn': col - 1} for col in positions.values()]
        for grid, key in zip(grids, positions):
            if key == 'updated_at':
                grid['rowData'] = [{'values': [{'userEnteredValue': {'formulaValue': '="old"'}}]}]
        response = {'spreadsheetId': source['spreadsheet_id'], 'sheets': [{
            'properties': {'sheetId': source['tab_id'], 'title': source['tab_name']}, 'data': grids}]}
        self.sheet.headers.http.fetch_sheet_metadata = Mock(return_value=response)
        captured = LiveTestSheet.capture(self.sheet, 'discount_15', 38)
        self.assertEqual(captured['values']['updated_at'], {'formulaValue': '="old"'})
        self.assertEqual(captured['values']['ticket_token'], {})
        request = self.sheet.headers.http.fetch_sheet_metadata.call_args.kwargs['params']
        self.assertEqual(len(request['ranges']), len(positions))
        self.assertTrue(all(r.endswith('38') for r in request['ranges']))
        self.assertEqual(set(captured['values']), set(positions))
        self.assertNotIn('payment_proof', captured['values'])
        self.sheet.writer.batch_update.assert_not_called()
        grids.pop()
        with self.assertRaises(ControlledTestBlocked):
            LiveTestSheet.capture(self.sheet, 'discount_15', 38)

    def test_concurrent_edit_at_either_comparison_prevents_write(self):
        changed = changed_snapshot(self.snapshot, {'last_error': literal('external')})
        for responses in ([changed], [self.snapshot, changed]):
            self.sheet.capture = Mock(side_effect=responses)
            with self.assertRaises(ControlledTestBlocked):
                self.sheet.write(self.snapshot, {'updated_at': literal('now')})
        self.sheet.writer.batch_update.assert_not_called()


class ActualRenderVerificationTests(unittest.TestCase):
    def test_current_template_synthetic_card_name_tier_qr_and_invalid_archive(self):
        from event_checkin.cards import CardRenderer
        from event_checkin.config import load_settings
        from PIL import Image
        configured = load_settings('production', ROOT)
        if not Path(configured.bun).is_file() or not (ROOT / 'freshcoat/node_modules').exists():
            self.skipTest('Bun or FreshCoat dependencies unavailable')
        with tempfile.TemporaryDirectory() as temp:
            settings = replace(configured, generated=Path(temp))
            template = settings.template.read_bytes()
            token = TEST_TOKEN_PREFIX + 'synthetic_0123456789_abcdef'
            path = CardRenderer(settings).render('TEST-OFFLINE', 'Synthetic Example', token, ticket_tier='Pro')
            proof = verify_render(settings, path, 'Synthetic Example', 'Pro', token)
            self.assertTrue(all(proof[k] for k in ('name_pixels_verified', 'tier_pixels_verified', 'qr_decoded')))
            with self.assertRaises(ControlledTestBlocked):
                verify_render(settings, path, 'Wrong Synthetic Name', 'Pro', token)
            with self.assertRaises(ControlledTestBlocked):
                verify_render(settings, path, 'Synthetic Example', 'Lite', token)
            with self.assertRaises(ControlledTestBlocked):
                verify_render(settings, path, 'Synthetic Example', 'Pro', token + 'wrong')
            archive = Path(temp) / 'INVALID-TEST-TICKET.png'
            archive_invalid(path, archive)
            self.assertFalse(path.exists())
            with Image.open(archive) as image:
                self.assertEqual(image.size, (1200, 974))
                self.assertEqual(image.getpixel((0, 950)), (139, 0, 0))
            self.assertEqual(settings.template.read_bytes(), template)


if __name__ == '__main__':
    unittest.main()
