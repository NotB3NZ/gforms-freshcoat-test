"""Finite selection phase: synthetic rows, temporary journals and no network."""
import argparse
import io
import json
import tempfile
import unittest
import uuid
from contextlib import redirect_stdout, redirect_stderr
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from event_checkin.config import ROOT, Settings
from event_checkin.production import BatchProcessor, is_registration, preview
from event_checkin.production_cli import register
from event_checkin.production_sheets import FixtureSheets, APPROVAL_HEADERS, PRODUCTION_METADATA
from event_checkin.sheets import Row, SheetIntegrityError
from tests.test_production import Renderer, Sheet, record

FIXTURE = ROOT/'examples/batch-selection-phase1.json'
REPORTING_FIXTURE = ROOT/'examples/batch-preview-reporting.json'


class BatchSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        root=Path(self.temp.name)
        self.s=Settings(root=root,profile='production',event_id='synthetic-phase1',runtime=root/'runtime-production',
                        generated=root/'generated-production',email_enabled=False)
        self.sheet=FixtureSheets(json.loads(FIXTURE.read_text()))
        self.renderer=Renderer(self.s)

    def plan(self,**scope):
        return preview(self.s,self.sheet,**scope)

    def execute(self,plan):
        with patch('socket.create_connection',side_effect=AssertionError('No network')), \
             patch('smtplib.SMTP') as smtp,patch('smtplib.SMTP_SSL') as smtp_ssl:
            result=BatchProcessor(self.s,self.sheet,self.renderer,None).execute(plan)
            smtp.assert_not_called();smtp_ssl.assert_not_called()
            return result

    def test_one_several_qualified_ids_and_existing_row_locators(self):
        for ids,expected in ((['regular:R-SYN-REG-002'],['regular:R-SYN-REG-002']),
                (['discount_15:R-SYN-D15-012','regular:R-SYN-REG-004'],['regular:R-SYN-REG-004','discount_15:R-SYN-D15-012']),
                (['regular:row:4'],['regular:R-SYN-REG-004'])):
            with self.subTest(ids=ids):
                _,plan=self.plan(ids=ids,limit='all')
                self.assertEqual([e['target'] for e in plan['entries']],expected)
        report,plan=self.plan(ids=['discount_15:P-SYN-D15-011'])
        self.assertFalse(plan['entries']);self.assertTrue(report[0]['sent'])

    def test_limit_counts_eligible_attempts_and_returns_fewer_when_unavailable(self):
        report,plan=self.plan(source='regular',limit=2)
        self.assertEqual([e['snapshot_row'] for e in plan['entries']],[2,4])
        self.assertEqual(len([r for r in report if r['selection_status']=='blocked']),1)
        _,plan=self.plan(source='regular',limit=100)
        self.assertEqual(len(plan['entries']),2)
        for source,count in (('regular',2),('discount_15',3),('discount_10',1)):
            with self.subTest(source=source):
                _,plan=self.plan(source=source,limit=100)
                self.assertEqual(len(plan['entries']),count)

    def test_global_limit_and_explicit_all_eligible_snapshot(self):
        _,plan=self.plan(all_sources=True,limit=3)
        self.assertEqual([e['source'] for e in plan['entries']],['regular','regular','discount_15'])
        _,plan=self.plan(all_sources=True,limit='all')
        self.assertEqual(len(plan['entries']),6);self.assertEqual(plan['limit'],6)
        self.assertEqual(plan['requested_limit'],'all')
        self.assertEqual(plan['tracking_scope']['targets'],[e['target'] for e in plan['entries']])
        self.assertTrue(all(e['process'] is True for e in plan['entries']))

    def test_inclusive_rows_10_through_13_exclude_sent_11(self):
        report,plan=self.plan(source='discount_15',start_row=10,end_row=13,limit='all')
        self.assertEqual([e['snapshot_row'] for e in plan['entries']],[10,12,13])
        self.assertEqual([r['row'] for r in report],[10,11,12,13])
        sent=next(r for r in report if r['row']==11)
        self.assertIn('already sent; explicit resend required',sent['blocking_reasons'])
        _,one=self.plan(source='discount_15',start_row=12,end_row=12,limit=5)
        self.assertEqual([e['snapshot_row'] for e in one['entries']],[12])

    def test_configured_source_order_then_physical_row_order_even_for_id_list(self):
        self.sheet.source_order=['discount_10','discount_15','regular']
        _,plan=self.plan(all_sources=True,limit=2)
        self.assertEqual([e['source'] for e in plan['entries']],['discount_10','discount_15'])
        _,plan=self.plan(ids=['regular:R-SYN-REG-004','discount_10:R-SYN-D10-002','regular:R-SYN-REG-002'])
        self.assertEqual([e['target'] for e in plan['entries']],['discount_10:R-SYN-D10-002','regular:R-SYN-REG-002','regular:R-SYN-REG-004'])
        self.assertIn('physical row',plan['ordering'])

    def test_empty_numeric_all_and_out_of_range_create_no_journal_or_writes(self):
        for scope in ({'source':'regular','start_row':3,'end_row':3,'limit':5},
                      {'source':'regular','start_row':100,'end_row':110,'limit':'all'}):
            before=deepcopy(self.sheet.data)
            _,plan=self.plan(**scope)
            self.assertEqual(plan['entries'],[]);self.assertEqual(self.execute(plan),[])
            self.assertEqual(self.sheet.data,before)
            self.assertFalse(self.s.runtime.exists());self.assertFalse(self.renderer.calls)

    def test_new_approvals_and_submissions_never_expand_the_saved_selection(self):
        _,plan=self.plan(source='regular',limit='all')
        waiting=self.sheet.data['sources']['regular'][1];waiting['confirmation']='TRUE'
        new=record(email='new@example.invalid',record_id='R-NEW');self.sheet.data['sources']['regular'].append(new)
        before_waiting=deepcopy(waiting);before_new=deepcopy(new)
        self.assertEqual(self.execute(plan),['ready','ready'])
        self.assertEqual(waiting,before_waiting);self.assertEqual(new,before_new)
        _,fresh=self.plan(source='regular',limit='all')
        self.assertIn('regular:R-SYN-REG-003',[e['target'] for e in fresh['entries']])
        self.assertIn('regular:R-NEW',[e['target'] for e in fresh['entries']])

    def test_revoked_approvals_do_not_backfill_over_limit_entries(self):
        for key in APPROVAL_HEADERS:
            with self.subTest(approval=key):
                sheet=Sheet([record(email='one@example.invalid'),record(email='two@example.invalid')])
                _,plan=preview(self.s,sheet,all_sources=True,limit=1,selection_version=2)
                sheet.data['sources']['regular'][0][key]='FALSE'
                second=deepcopy(sheet.data['sources']['regular'][1])
                renderer=Renderer(self.s)
                self.assertEqual(BatchProcessor(self.s,sheet,renderer,None).execute(plan),['blocked'])
                self.assertEqual(sheet.data['sources']['regular'][1],second)
                self.assertFalse(renderer.calls)
                self.assertNotIn('ticket_token',sheet.data['sources']['regular'][0])

    def test_protected_fields_sent_history_and_all_unselected_rows_unchanged(self):
        before=deepcopy(self.sheet.data)
        report,plan=self.plan(source='discount_15',start_row=10,end_row=13,limit=2)
        self.assertEqual(self.execute(plan),['ready','ready'])
        targets={(e['source'],e['snapshot_row']) for e in plan['entries']}
        for source,records in self.sheet.data['sources'].items():
            for index,row in enumerate(records,2):
                original=before['sources'][source][index-2]
                if (source,index) not in targets:self.assertEqual(row,original)
                else:
                    for key in ('name','email','tier',*APPROVAL_HEADERS):self.assertEqual(row[key],original[key])
        identities=[(r.get('participant_id'),r.get('ticket_token')) for r in self.sheet.rows() if r.get('state')=='READY']
        self.assertEqual(self.execute(plan),['completed previously','completed previously'])
        self.assertEqual(identities,[(r.get('participant_id'),r.get('ticket_token')) for r in self.sheet.rows() if r.get('state')=='READY'])

    def test_changed_fingerprint_or_missing_selected_target_stops_without_substitution(self):
        for changed in ('email','missing'):
            sheet=Sheet([record(email='one@example.invalid'),record(email='two@example.invalid')])
            _,plan=preview(self.s,sheet,source='regular',limit=1,selection_version=2)
            if changed=='email':sheet.data['sources']['regular'][0]['email']='changed@example.invalid'
            else:sheet.data['sources']['regular'].pop(0)
            before=deepcopy(sheet.data)
            with self.assertRaises(SheetIntegrityError):BatchProcessor(self.s,sheet,Renderer(self.s),None).execute(plan)
            self.assertEqual(sheet.data,before)

    def test_cross_source_duplicate_conflicts_still_block_narrow_selection(self):
        sheet=Sheet([record(email='duplicate@example.invalid')],discount_10=[record(email='duplicate@example.invalid',tier='Lite')])
        report,plan=preview(self.s,sheet,source='regular',selection_version=2)
        self.assertEqual(plan['entries'],[])
        self.assertIn('conflicting approved registrations; staff review required',report[0]['blocking_reasons'])

    def test_invalid_limits_ranges_and_repeated_ids_fail_before_read(self):
        for arguments in ({'limit':0},{'limit':-1},{'limit':True},{'limit':1.5},{'limit':'ALL'},
                {'start_row':2},{'start_row':10,'end_row':3},{'start_row':1,'end_row':3},
                {'start_row':True,'end_row':3}):
            with patch.object(self.sheet,'rows',side_effect=AssertionError('No read')):
                with self.assertRaises(SheetIntegrityError):self.plan(all_sources=True,**arguments)
        with self.assertRaises(SheetIntegrityError):self.plan(ids=['regular:R-SYN-REG-002']*2)

    def test_tampered_execution_scope_is_rejected_before_journal(self):
        _,plan=self.plan(source='regular')
        plan['tracking_scope']['targets'].append('discount_15:row:11')
        with self.assertRaises(SheetIntegrityError):self.execute(plan)
        self.assertFalse(self.s.runtime.exists())

    def test_reporting_excludes_only_empty_rows_and_preserves_partial_history(self):
        self.sheet = Sheet()
        self.sheet.data = json.loads(REPORTING_FIXTURE.read_text())
        before = deepcopy(self.sheet.data)
        report, plan, summary = self.plan(all_sources=True, limit=1, include_summary=True)
        self.assertEqual([r['row'] for r in report if r['source'] == 'regular'], list(range(5, 14)))
        self.assertEqual([e['snapshot_row'] for e in plan['entries']], [11])
        self.assertEqual(summary['regular'], {
            'actual_registrations': 9, 'selected_eligible_registrations': 1,
            'eligible_beyond_limit': 1, 'already_sent_registrations': 1,
            'blocked_registrations': 7, 'empty_checkbox_only_rows_excluded': 3,
            'blocked_by_reason': {
                'waiting for approval: ' + ', '.join(APPROVAL_HEADERS): 6,
                'review invalid name/email or unknown raw tier/price category': 6,
                'failed job requires explicit retry': 1,
                'duplicate of approved canonical registration': 1,
            }})
        self.assertEqual(summary['discount_15']['eligible_beyond_limit'], 1)
        self.assertEqual(summary['discount_10']['actual_registrations'], 0)
        self.assertEqual(summary['discount_10']['empty_checkbox_only_rows_excluded'], 1)
        self.assertNotIn('regular:row:11', json.dumps(summary))
        self.assertEqual(self.sheet.data, before)
        self.assertFalse(self.sheet.attempts)
        self.assertFalse(self.s.runtime.exists())
        self.assertFalse(self.s.generated.exists())

    def test_every_stored_metadata_field_preserves_a_registration(self):
        for key in ('name', 'email', 'tier', *PRODUCTION_METADATA):
            for value in ('history', 0, False, ' '):
                with self.subTest(key=key, value=value):
                    self.assertTrue(is_registration(Row(2, {key: value}, 'regular')))
        self.assertFalse(is_registration(Row(2, dict.fromkeys(PRODUCTION_METADATA, None), 'regular')))
        self.assertFalse(is_registration(Row(2, {'name': '', 'email': None, 'tier': '',
            '_price_category': 'regular', **dict.fromkeys(APPROVAL_HEADERS, True)}, 'regular')))

    def test_exclusion_counts_follow_source_range_ids_and_empty_scopes(self):
        self.sheet = FixtureSheets(json.loads(REPORTING_FIXTURE.read_text()))
        report, plan, summary = self.plan(source='regular', start_row=2, end_row=5, include_summary=True)
        self.assertEqual(list(summary), ['regular'])
        self.assertEqual(summary['regular']['actual_registrations'], 1)
        self.assertEqual(summary['regular']['empty_checkbox_only_rows_excluded'], 3)
        self.assertEqual([r['row'] for r in report], [5])
        self.assertFalse(plan['entries'])
        report, plan, summary = self.plan(ids=['regular:row:2'], include_summary=True)
        self.assertFalse(report); self.assertFalse(plan['entries'])
        self.assertEqual(summary['regular']['empty_checkbox_only_rows_excluded'], 1)
        _, _, summary = self.plan(source='discount_15', start_row=99, end_row=100, include_summary=True)
        self.assertEqual(list(summary), ['discount_15'])
        self.assertFalse(any(value for value in summary['discount_15'].values()))

    def test_summary_sent_counts_preserve_send_retry_and_resend_rules(self):
        for action, selected, blocked in (('send', 0, 0), ('retry', 0, 1), ('resend', 1, 0)):
            with self.subTest(action=action):
                _, plan, summary = self.plan(source='discount_15', start_row=11, end_row=11,
                                             action=action, include_summary=True)
                counts = summary['discount_15']
                self.assertEqual(counts['already_sent_registrations'], 1)
                self.assertEqual(counts['selected_eligible_registrations'], selected)
                self.assertEqual(counts['blocked_registrations'], blocked)
                self.assertEqual(len(plan['entries']), selected)

    def test_blank_or_checkbox_rows_do_not_change_selected_plan(self):
        # Appending empty rows must not alter identities, duplicate claims, ordering,
        # saved entries or the finite tracking scope (including a global limit).
        for limit in (1, 5, 'all'):
            with self.subTest(limit=limit), patch('event_checkin.production.uuid.uuid4', return_value='fixed-plan-id'):
                _, before = self.plan(all_sources=True, limit=limit)
                modified = deepcopy(self.sheet.data)
                for records in modified['sources'].values():
                    records.extend([{}, dict.fromkeys(APPROVAL_HEADERS, False)])
                report, after, summary = preview(self.s, FixtureSheets(modified), all_sources=True,
                                                limit=limit, include_summary=True)
                self.assertEqual(before, after)
                self.assertTrue(all(s['empty_checkbox_only_rows_excluded'] == 2 for s in summary.values()))

    def test_identity_validation_still_fails_closed_even_outside_scope(self):
        self.sheet.data['sources']['discount_10'].append({'participant_id': 'P-SYN-PARTIAL'})
        with self.assertRaisesRegex(SheetIntegrityError, 'Partial production ticket identity'):
            self.plan(source='regular', include_summary=True)


class BatchSelectionCLITests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        root=Path(self.temp.name)
        self.s=Settings(root=root,profile='production',event_id='offline-cli-test',runtime=root/'runtime-production',generated=root/'generated-production')

    def cli(self,*args):
        from event_checkin.__main__ import main
        with patch('sys.argv',['event_checkin','--profile','production',*map(str,args)]), \
             patch('event_checkin.__main__.load_settings',return_value=self.s), \
             redirect_stdout(io.StringIO()) as out,redirect_stderr(io.StringIO()) as err:
            result=main()
        return result,out.getvalue(),err.getvalue()

    def test_saved_plan_preview_separates_categories_and_offline_execution(self):
        plan=self.s.runtime/'offline/range-plan.json';state=self.s.runtime/'offline/range-state.json'
        with patch('gspread.service_account',side_effect=AssertionError('No Google')), \
             patch('smtplib.SMTP') as smtp,patch('smtplib.SMTP_SSL') as smtp_ssl, \
             patch('event_checkin.production_cli.CardRenderer',Renderer):
            result,out,err=self.cli('batch-preview','--fixture',FIXTURE,'--source','discount_15','--start-row','10','--end-row','13','--limit','2','--details','--plan-out',plan)
            self.assertIsNone(result,err)
            report=json.JSONDecoder().raw_decode(out)[0]
            self.assertEqual([r['row'] for r in report['selected_entries']],[10,12])
            self.assertEqual([r['row'] for r in report['blocked_entries']],[11])
            self.assertEqual([r['row'] for r in report['eligible_beyond_limit']],[13])
            self.assertEqual(report['waiting_snapshot_entries'],0)
            self.assertIsNone(self.cli('batch-run','--fixture',FIXTURE,'--plan',plan,'--state-out',state)[0])
            self.assertIsNone(self.cli('batch-run','--fixture',state,'--plan',plan,'--state-out',state)[0])
            smtp.assert_not_called();smtp_ssl.assert_not_called()
        saved=json.loads(plan.read_text());self.assertEqual(saved['version'],2)
        self.assertTrue((self.s.runtime/'offline'/saved['fixture_session']/'production-batches.sqlite3').is_file())
        self.assertFalse((self.s.runtime/'production-batches.sqlite3').exists())

    def test_cli_all_remains_source_scope_and_limit_all_is_distinct(self):
        result,out,err=self.cli('batch-preview','--fixture',FIXTURE,'--all','--limit','all')
        self.assertIsNone(result,err)
        report=json.loads(out);self.assertEqual(report['delivery_attempts'],6)
        self.assertTrue(report['scope']['all_sources'])
        result,out,err=self.cli('batch-preview','--fixture',FIXTURE,'--all','--limit','1')
        self.assertIsNone(result,err);self.assertEqual(json.loads(out)['delivery_attempts'],1)

    def test_summary_is_default_and_details_do_not_change_summary_or_plan(self):
        paths = [self.s.runtime/'offline/summary.json', self.s.runtime/'offline/details.json']
        saved, outputs = [], []
        with patch('gspread.service_account', side_effect=AssertionError('No Google')), \
             patch('socket.create_connection', side_effect=AssertionError('No network')), \
             patch('smtplib.SMTP') as smtp, patch('smtplib.SMTP_SSL') as smtp_ssl, \
             patch('event_checkin.production_cli.CardRenderer') as renderer, \
             patch('uuid.uuid4', return_value=uuid.UUID('00000000-0000-0000-0000-000000000001')):
            for path, details in zip(paths, ([], ['--details'])):
                result, out, err = self.cli('batch-preview', '--fixture', REPORTING_FIXTURE,
                                            '--all', '--limit', '1', '--plan-out', path, *details)
                self.assertIsNone(result, err)
                outputs.append(json.JSONDecoder().raw_decode(out)[0])
                saved.append(json.loads(path.read_text()))
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            smtp.assert_not_called(); smtp_ssl.assert_not_called(); renderer.assert_not_called()
        default, detailed = outputs
        self.assertEqual(default['summary'], detailed['summary'])
        self.assertEqual(saved[0], saved[1])
        self.assertEqual(saved[0]['version'], 2)
        for key in ('selected_entries', 'blocked_entries', 'eligible_beyond_limit', 'tracking_scope'):
            self.assertNotIn(key, default)
            self.assertIn(key, detailed)
        for private in ('Fictional', '@example.invalid', 'R-SYN-HISTORY', 'regular:row:11'):
            self.assertNotIn(private, json.dumps(default))
        self.assertEqual(len(detailed['selected_entries']), 1)
        self.assertEqual(len(detailed['blocked_entries']), 7)
        self.assertEqual(len(detailed['eligible_beyond_limit']), 2)
        self.assertEqual(default['live_execution'], 'LOCKED')
        self.assertFalse((self.s.runtime/'production-batches.sqlite3').exists())
        self.assertFalse(self.s.generated.exists())

    def test_invalid_cli_arguments_never_construct_google_reader(self):
        for flags in (('--source','regular','--start-row','3'),('--all','--start-row','3','--end-row','5'),
                      ('--source','regular','--start-row','5','--end-row','3')):
            with patch('event_checkin.production_cli.ProductionSheets') as reader:
                self.assertEqual(self.cli('batch-preview','--live',*flags)[0],1);reader.assert_not_called()
        parser=argparse.ArgumentParser();register(parser.add_subparsers(dest='command',required=True))
        for flags in (('--all','--limit','0'),('--all','--limit','-1'),('--all','--limit','1.5'),
                      ('--all','--limit','ALL'),('--source','regular','--start-row','1','--end-row','3'),
                      ('--source','regular','--all')):
            with self.subTest(flags=flags),redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):
                parser.parse_args(['batch-preview','--fixture',str(FIXTURE),*flags])

    def test_live_run_remains_locked_before_any_adapter_or_plan_read(self):
        with patch('event_checkin.production_cli.ProductionSheets') as reader,patch('smtplib.SMTP') as smtp:
            result,out,err=self.cli('batch-run','--plan','missing.json')
            self.assertEqual(result,1);self.assertIn('locked',err)
            reader.assert_not_called();smtp.assert_not_called()


if __name__=='__main__':unittest.main()
