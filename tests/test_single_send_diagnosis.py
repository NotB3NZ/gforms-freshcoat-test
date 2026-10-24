"""Recovery diagnostics use only synthetic data, fake HTTP and mocked SMTP."""
import io
import json
import sqlite3
import hashlib
import unittest
from contextlib import closing, redirect_stdout
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from event_checkin.config import ROOT
from event_checkin.diagnostics import diagnostic_context, safe_diagnostic
from event_checkin.production import private_json
from event_checkin.production_sheets import PRODUCTION_METADATA, source_mapping, column_number
from event_checkin.header_init import APPROVED_RANGES
from event_checkin.single_send import (SingleSettings, read_policy, load_selection, diagnose_existing,
                                      delivery_evidence, run_command, scope_path, safe_tracking_status, safe_attempt_count)
from tests import test_single_send as fixtures
from tests.test_diagnostics import ProviderError


class SingleEntryDiagnosisTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.SingleSendTests(); self.f.setUp(); self.addCleanup(self.f.doCleanups)

    def partial_attempt(self):
        self.f.ready()
        original=SingleSettings.require_email
        def fail_after_render(settings):
            original(settings)
            row=self.f.reader.sheet.data['sources']['regular'][0]
            if row.get('render_status')=='RENDERED' and not row.get('message_id'):
                raise ValueError('DO_NOT_PRINT private@example.invalid credential token-secret')
        with patch.object(SingleSettings,'require_email',fail_after_render),patch('smtplib.SMTP') as smtp:
            with self.assertRaises(ValueError): self.f.run_mode()
            smtp.assert_not_called()
        return self.f.reader.sheet.data['sources']['regular'][0]

    def journal_action(self):
        selection=load_selection(self.f.s,self.f.selection,read_policy(self.f.s))
        with closing(sqlite3.connect((self.f.s.runtime/'production-batches.sqlite3').resolve().as_uri()+'?mode=ro',uri=True)) as db:
            return db.execute('SELECT record_id,message_id,done FROM actions WHERE plan_id=?',
                              (selection['plan']['plan_id'],)).fetchone()

    def test_resume_reuses_reserved_message_id_identity_and_card(self):
        row=self.partial_attempt()
        reserved=self.journal_action()[1]
        identity=(row['participant_id'],row['ticket_token'])
        card=Path(row['card_path']); before=card.read_bytes(); selection=self.f.selection.read_bytes()
        with patch('smtplib.SMTP') as smtp:
            self.assertEqual(self.f.run_mode(),['sent'])
            self.assertEqual(smtp.return_value.send_message.call_args.args[0]['Message-ID'],reserved)
            smtp.return_value.send_message.assert_called_once()
        self.assertEqual((row['participant_id'],row['ticket_token']),identity)
        self.assertEqual(card.read_bytes(),before); self.assertEqual(self.f.selection.read_bytes(),selection)
        self.assertEqual(len(self.f.renderer.calls),1)

    def test_cli_retains_stage_source_and_allowlisted_http_reason_without_private_values(self):
        failure=ProviderError(429,{'status':'RESOURCE_EXHAUSTED','errors':[{'reason':'rateLimitExceeded'}],
                                   'message':'DO_NOT_PRINT private@example.invalid token-secret'})
        def fail(*args,**kwargs):
            try:
                with diagnostic_context('read_production_source','discount_15'): raise failure
            except ProviderError:
                with diagnostic_context('single_entry_execution','regular'):
                    raise RuntimeError('DO_NOT_PRINT wrapper') from None
        args=SimpleNamespace(prepare=False,diagnose=False,resolve=None,retry=False,authorize_send=True,selection=self.f.selection)
        output=io.StringIO()
        with patch('event_checkin.single_send.execute',side_effect=fail),redirect_stdout(output):
            self.assertEqual(run_command(self.f.s,args),1)
        diagnostic=json.loads(output.getvalue())['diagnostic']
        self.assertEqual(diagnostic['stage'],'read_production_source')
        self.assertEqual(diagnostic['source'],'discount_15')
        self.assertEqual(diagnostic['exception_class'],'ProviderError')
        self.assertEqual(diagnostic['http_status'],429)
        self.assertEqual(diagnostic['google_api_reason'],'RESOURCE_EXHAUSTED')
        for secret in ('DO_NOT_PRINT','private@','token-secret','credential'):
            self.assertNotIn(secret,output.getvalue())
        self.assertFalse(self.f.selection.exists())
        self.assertFalse((self.f.s.runtime/'production-batches.sqlite3').exists())
        self.assertEqual(len(list(self.f.selection.parent.glob('selection.diagnostic-*.json'))),1)

    def transport(self,row):
        entries=[]; snapshots={}
        for source in json.loads((ROOT/'config/sources.production.example.json').read_text())['sources']:
            count,span=APPROVED_RANGES[source['id']]
            original=['Synthetic question '+str(i) for i in range(count)]
            for label,col in source_mapping(source).values(): original[column_number(col)-1]=label
            entry={'source':source,'original_headers':original,'range':span}; entries.append(entry)
            snapshots[source['id']]={'title':source['tab_name'],'tab_id':source['tab_id'],
                'column_count':count+24,'headers':original+list(PRODUCTION_METADATA)}
        plan={'sources':entries}
        http=Mock()
        keys=('email','name','tier',*PRODUCTION_METADATA)
        http.values_batch_get.return_value={'valueRanges':[{'values':[[row.get(k,'')]]} for k in keys]}
        transport=SimpleNamespace(read=lambda e:deepcopy(snapshots[e['source']['id']]),pace=lambda:None,http=http)
        return plan,transport

    def test_diagnosis_reads_only_headers_and_exact_target_without_history_changes(self):
        row=self.partial_attempt()
        self.f.s.email_enabled=False
        policy=read_policy(self.f.s); policy['enabled']=False; private_json(scope_path(self.f.s),policy)
        plan,transport=self.transport(row)
        files=(self.f.selection,self.f.s.runtime/'production-batches.sqlite3',Path(row['card_path']))
        before={p:p.read_bytes() for p in files}; calls=deepcopy(self.f.reader.sheet.attempts)
        with patch('event_checkin.single_send.load_plan',return_value=plan), \
             patch('event_checkin.production.BatchJournal.__init__',side_effect=AssertionError('No journal creation')), \
             patch('smtplib.SMTP') as smtp, patch('smtplib.SMTP_SSL') as smtp_ssl:
            result=diagnose_existing(self.f.s,self.f.selection,transport)
            smtp.assert_not_called();smtp_ssl.assert_not_called()
        self.assertEqual(result['state'],'PROCESSING'); self.assertEqual(result['render_status'],'RENDERED')
        self.assertEqual(result['email_attempts'],'0'); self.assertEqual(result['email_status'],'')
        self.assertFalse(result['completion_recorded']); self.assertEqual(result['smtp_submission'],'NOT_SUBMITTED')
        self.assertTrue(result['participant_identity_matches_reservation'])
        for p in files: self.assertEqual(p.read_bytes(),before[p])
        self.assertEqual(self.f.reader.sheet.attempts,calls)
        ranges=transport.http.values_batch_get.call_args.args[1]
        self.assertEqual(len(ranges),27)
        self.assertTrue(all(span.split('!')[1].split(':')[0].endswith('2') and
                            span.split(':')[1].endswith('2') for span in ranges))
        self.assertNotIn('one@example.invalid',json.dumps(result))
        self.assertNotIn(row['ticket_token'],json.dumps(result))

    def test_missing_completion_alone_never_proves_non_submission(self):
        status={k:True for k in ('bound_registration_matches','record_id_matches_action','participant_identity_matches_reservation',
            'frozen_ticket_details_match_reservation','frozen_recipient_matches_reservation','journal_plan_digest_matches')}
        status.update(state='PROCESSING',render_status='RENDERED',email_status='SENDING',email_attempts='1',
                      message_id_present=True,email_sent_at_present=False,completion_recorded=False,any_related_action_completed=False)
        self.assertEqual(delivery_evidence(status,{})[0],'UNCERTAIN')
        status.update(email_status='SENT',email_sent_at_present=True)
        self.assertEqual(delivery_evidence(status,{})[0],'CONFIRMED_ACCEPTED')
        status.update(email_status='SENDING',email_sent_at_present=False,durable_smtp_acceptance_recorded=True)
        self.assertEqual(delivery_evidence(status,{})[0],'CONFIRMED_ACCEPTED')
        status.update(durable_smtp_acceptance_recorded=False,email_status='',email_attempts='0',message_id_present=False)
        self.assertEqual(delivery_evidence(status,{})[0],'NOT_SUBMITTED')
        self.assertEqual(delivery_evidence(status,{'message_id':'previous-private-id'})[0],'UNCERTAIN')
        status['any_related_action_completed']=True
        self.assertEqual(delivery_evidence(status,{})[0],'UNCERTAIN')

    def test_cleanup_exception_cannot_hide_confirmed_smtp_acceptance(self):
        self.f.ready()
        with patch('smtplib.SMTP') as smtp:
            smtp.return_value.close.side_effect=OSError('DO_NOT_PRINT')
            self.assertEqual(self.f.run_mode(),['sent'])
            smtp.return_value.send_message.assert_called_once()
        events=[json.loads(p.read_text()) for p in self.f.selection.parent.glob('selection.smtp-evidence-*.json')]
        self.assertTrue(any(e['accepted'] for e in events))
        self.assertNotIn('DO_NOT_PRINT',json.dumps(events))

    def test_corrupt_status_and_counts_are_withheld(self):
        self.assertEqual(safe_tracking_status('private@example.invalid',{'SENT','PROCESSING'}),'UNRECOGNIZED_VALUE_WITHHELD')
        self.assertEqual(safe_attempt_count('credential-token'),'INVALID_COUNT_WITHHELD')
        self.assertEqual(safe_attempt_count('1'),'1')

    def test_untrusted_source_is_not_echoed(self):
        error=ValueError('DO_NOT_PRINT');error._safe_source='private@example.invalid'
        result=safe_diagnostic(error,source='regular')
        self.assertEqual(result['source'],'regular');self.assertNotIn('private@',json.dumps(result))
