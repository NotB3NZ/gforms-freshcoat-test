"""Deliberate duplicate-risk resend: synthetic tickets, fake SMTP, temp journals."""
import io
import json
import smtplib
import sqlite3
import unittest
from contextlib import closing, redirect_stdout
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

from event_checkin import pilot, pilot_resend as resend
from event_checkin.config import ConfigurationError
from tests import test_pilot


class DeliberateResendTests(unittest.TestCase):
    def setUp(self):
        self.f=test_pilot.PilotTests(); self.f.setUp()
        self.addCleanup(self.f.doCleanups); self.addCleanup(self.f.tearDown)
        # Establish a fictional issued/badged ticket whose original intent never
        # reached a confirmed SMTP outcome; the other two actions stay reserved.
        self.f.enable()
        self.f.reader.sheet.fail_before=lambda c:c.get('email_status')=='SENDING'
        with self.assertRaises(ConnectionError): self.f.run_pilot()
        self.assertEqual(self.f.receipt()[0],'INTENT')
        self.f.s.email_enabled=False; self.f.policy['enabled']=False
        pilot.private_json(pilot.policy_path(self.f.s),self.f.policy)
        self.policy=resend.prepare(self.f.s,self.f.path)
        self.original_row=deepcopy(self.f.row())
        self.original_tables=self.tables()
        self.badge=(self.f.s.generated/self.f.row()['participant_id']/'card.png')
        self.badge_bytes=self.badge.read_bytes()
        self.original_renderer_calls=len(self.f.renderer.calls)
        self.original_tracking_updates=len(self.f.reader.sheet.attempts)

    def tables(self):
        with closing(sqlite3.connect(self.f.s.runtime/'production-batches.sqlite3')) as db:
            return {name:db.execute('SELECT * FROM '+name+' ORDER BY rowid').fetchall()
                    for name in ('plans','actions','claims','pilot_bindings','pilot_delivery','pilot_delivery_events')}

    def enable(self):
        self.f.enable(); self.policy['enabled']=True
        pilot.private_json(resend.policy_path(self.f.s),self.policy)

    def execute(self,**kwargs):
        return resend.execute(self.f.s,self.f.path,reader=self.f.reader,reload_settings=lambda:self.f.s,
                              acknowledged=True,**kwargs)

    def resolve(self,outcome):
        return resend.resolve(self.f.s,self.f.path,outcome,'synthetic provider evidence',
                              reader=self.f.reader,reload_settings=lambda:self.f.s)

    def assert_original_unchanged(self):
        self.assertEqual(self.f.row(),self.original_row)
        self.assertEqual(self.tables(),self.original_tables)
        self.assertEqual(self.badge.read_bytes(),self.badge_bytes)
        self.assertEqual(len(self.f.renderer.calls),self.original_renderer_calls)
        self.assertEqual(len(self.f.reader.sheet.attempts),self.original_tracking_updates)
        self.assertEqual(self.f.reader.sheet.data['sources']['discount_15'][1:3],self.f.initial['sources']['discount_15'][1:3])

    def test_preparation_and_diagnosis_leave_scope_disabled_and_history_unchanged(self):
        self.assertFalse(self.policy['enabled']); self.assertFalse(self.f.s.email_enabled)
        report=resend.diagnose(self.f.s,self.f.path,reader=self.f.reader)
        self.assertEqual(report['resend_delivery'],'NOT_STARTED')
        self.assertEqual(report['original_delivery'],'INTENT')
        self.assertIsNone(resend.read_attempt(self.f.s,self.f.plan))
        self.assert_original_unchanged()

    def test_both_sending_gates_and_separate_scope_block_before_io(self):
        before=self.f.reader.reads
        for email,base,extra in ((False,True,True),(True,False,True),(True,True,False)):
            self.f.s.email_enabled=email; self.f.policy['enabled']=base; self.policy['enabled']=extra
            pilot.private_json(pilot.policy_path(self.f.s),self.f.policy)
            pilot.private_json(resend.policy_path(self.f.s),self.policy)
            with self.assertRaises(ConfigurationError): self.execute()
        self.assertEqual(self.f.reader.reads,before)
        self.assertFalse(self.f.smtp.connections)
        self.assert_original_unchanged()

    def test_duplicate_risk_acknowledgement_is_required(self):
        self.enable()
        with self.assertRaises(ConfigurationError): resend.execute(self.f.s,self.f.path)
        self.assertFalse(self.f.smtp.connections)
        self.assert_original_unchanged()

    def test_separate_linked_acceptance_reuses_exact_badge_and_ticket(self):
        self.enable(); outcome=self.execute()
        self.assertIn('SMTP accepted',outcome)
        attempt=resend.read_attempt(self.f.s,self.f.plan)
        original=pilot.existing(self.f.s,self.f.plan,self.f.plan['entries'][0])[0]
        self.assertEqual(attempt['status'],'ACCEPTED')
        self.assertEqual(attempt['parent_message_id'],original[1])
        self.assertNotEqual(attempt['message_id'],original[1])
        self.assertEqual(attempt['parent_record_id'],self.original_row['record_id'])
        self.assertEqual(json.loads(attempt['parent_receipt'])[0],'INTENT')
        message=self.f.smtp.messages[0]
        self.assertEqual(str(message['Message-ID']),attempt['message_id'])
        self.assertIn(self.original_row['participant_id'],message.get_body(preferencelist=('plain',)).get_content())
        self.assertTrue(all(p.get_payload(decode=True)==self.badge_bytes for p in message.walk() if p.get_content_type()=='image/png'))
        self.assertEqual(self.execute(),'accepted previously; no resubmission')
        self.assertEqual(len(self.f.smtp.messages),1)
        self.assert_original_unchanged()

    def test_each_approval_duplicate_cancellation_and_frozen_changes_still_block(self):
        self.enable()
        for key in ('confirmation','reference_matched','price_matched','payment_received'):
            old=self.f.row()[key]; self.f.row()[key]=False
            with self.assertRaises(ConfigurationError): self.execute()
            self.f.row()[key]=old
        self.f.row()['ticket_status']='CANCELLED'
        with self.assertRaises(ConfigurationError): self.execute()
        self.f.row()['ticket_status']='ACTIVE'
        old=self.f.row()['ticket_tier']; self.f.row()['ticket_tier']='Pro'
        with self.assertRaises(ConfigurationError): self.execute()
        self.f.row()['ticket_tier']=old
        duplicate=deepcopy(self.f.row()); duplicate['record_id']='R-conflicting'; duplicate['participant_id']='P-conflicting'; duplicate['ticket_token']='z'*43
        duplicate.update(source_id='discount_10',price_category='10_percent_discount',
                         _price_category='10_percent_discount',tier='Regular (₱261)')
        self.f.reader.sheet.data['sources']['discount_10']=[duplicate]
        with self.assertRaises(Exception): self.execute()
        self.f.reader.sheet.data['sources']['discount_10']=[]
        self.assertFalse(self.f.smtp.messages)
        self.assert_original_unchanged()

    def test_missing_or_changed_badge_is_never_redrawn(self):
        self.enable(); self.badge.write_bytes(b'changed-synthetic-badge')
        with self.assertRaises(ConfigurationError): self.execute()
        self.badge.write_bytes(self.badge_bytes)
        self.assertFalse(self.f.smtp.messages)
        self.assert_original_unchanged()

    def test_uncertain_resend_never_retries_and_original_attempt_stays_uncertain(self):
        self.enable(); self.f.smtp.results=[TimeoutError('synthetic DATA disconnect')]
        self.assertIn('uncertain',self.execute())
        attempt=resend.read_attempt(self.f.s,self.f.plan)
        self.assertEqual(attempt['status'],'UNCERTAIN')
        self.assertIn('provider review',self.execute())
        self.assertIn('provider review',self.execute(retry=True))
        self.assertEqual(len(self.f.smtp.messages),1)
        self.assert_original_unchanged()
        self.f.s.email_enabled=False
        self.resolve('not-sent')
        self.f.s.email_enabled=True
        self.execute(retry=True)
        self.assertEqual(resend.read_attempt(self.f.s,self.f.plan)['message_id'],attempt['message_id'])
        self.assertEqual(len(self.f.smtp.messages),2)
        self.assert_original_unchanged()

    def test_known_unsent_resend_requires_explicit_retry_with_same_message_id(self):
        self.enable(); self.f.smtp.results=[smtplib.SMTPDataError(550,b'synthetic refusal')]
        self.assertIn('known unsent',self.execute())
        old=resend.read_attempt(self.f.s,self.f.plan)
        self.assertEqual(self.execute(),'explicit resend retry required')
        self.execute(retry=True)
        self.assertEqual(resend.read_attempt(self.f.s,self.f.plan)['message_id'],old['message_id'])
        self.assertEqual(len(self.f.smtp.messages),2)
        self.assert_original_unchanged()

    def test_quota_before_submission_retains_intent_and_forbids_automatic_resend(self):
        self.enable()
        original=self.f.reader.rows
        def interrupted():
            attempt=resend.read_attempt(self.f.s,self.f.plan)
            if attempt and attempt['status']=='INTENT': raise RuntimeError('synthetic read quota exhausted')
            return original()
        with patch.object(self.f.reader,'rows',side_effect=interrupted): self.execute()
        self.assertFalse(self.f.smtp.messages)
        self.assertEqual(resend.read_attempt(self.f.s,self.f.plan)['status'],'UNSENT')
        self.assertEqual(self.execute(),'explicit resend retry required')
        self.assert_original_unchanged()

    def test_acceptance_receipt_failure_stays_uncertain_without_automatic_retry(self):
        self.enable(); original=resend.ResendJournal.record
        def fail(journal,settings,plan,status,**kwargs):
            if status=='ACCEPTED': raise OSError('synthetic journal failure')
            return original(journal,settings,plan,status,**kwargs)
        with patch.object(resend.ResendJournal,'record',fail): self.execute()
        self.assertEqual(resend.read_attempt(self.f.s,self.f.plan)['status'],'UNCERTAIN')
        self.execute()
        self.assertEqual(len(self.f.smtp.messages),1)
        self.assert_original_unchanged()

    def test_accepted_receipt_survives_failure_after_commit_without_resubmission(self):
        self.enable(); original=resend.ResendJournal.record
        def commit_then_fail(journal,settings,plan,status,**kwargs):
            result=original(journal,settings,plan,status,**kwargs)
            if status=='ACCEPTED': raise OSError('synthetic lost receipt readback')
            return result
        with patch.object(resend.ResendJournal,'record',commit_then_fail): self.execute()
        self.assertEqual(resend.read_attempt(self.f.s,self.f.plan)['status'],'ACCEPTED')
        self.execute()
        self.assertEqual(len(self.f.smtp.messages),1)
        self.assert_original_unchanged()

    def test_interruption_after_resend_intent_is_never_automatically_repeated(self):
        self.enable(); original=resend.ResendJournal.record
        def interrupt(journal,settings,plan,status,**kwargs):
            result=original(journal,settings,plan,status,**kwargs)
            if status=='INTENT': raise RuntimeError('synthetic interruption before transport')
            return result
        with patch.object(resend.ResendJournal,'record',interrupt):
            with self.assertRaises(RuntimeError): self.execute()
        self.assertEqual(resend.read_attempt(self.f.s,self.f.plan)['status'],'INTENT')
        self.execute(); self.execute(retry=True)
        self.assertFalse(self.f.smtp.messages)
        self.assert_original_unchanged()

    def test_no_google_read_between_acceptance_and_receipt_and_no_final_sheet_write(self):
        self.enable(); self.f.smtp.on_send=lambda:setattr(self.f.reader,'fail_reads',True)
        self.execute()
        self.assertEqual(resend.read_attempt(self.f.s,self.f.plan)['status'],'ACCEPTED')
        self.f.reader.fail_reads=False; self.execute()
        self.assertEqual(len(self.f.smtp.messages),1)
        self.assert_original_unchanged()

    def test_new_approval_or_scope_revocation_after_authentication_blocks_data(self):
        self.enable(); self.f.edited_fields={'confirmation'}
        self.f.smtp.on_login=lambda:self.f.row().__setitem__('confirmation',False)
        self.execute()
        self.assertFalse(self.f.smtp.messages)
        self.assertEqual(resend.read_attempt(self.f.s,self.f.plan)['status'],'UNSENT')
        self.f.row()['confirmation']=self.original_row['confirmation']
        self.assert_original_unchanged()

    def test_resend_scope_disabled_after_authentication_prevents_submission(self):
        self.enable()
        def disable():
            self.policy['enabled']=False
            pilot.private_json(resend.policy_path(self.f.s),self.policy)
        self.f.smtp.on_login=disable
        self.execute()
        self.assertFalse(self.f.smtp.messages)
        self.assertEqual(resend.read_attempt(self.f.s,self.f.plan)['status'],'UNSENT')
        self.assert_original_unchanged()

    def test_email_disabled_after_authentication_prevents_submission(self):
        self.enable(); self.f.smtp.on_login=lambda:setattr(self.f.s,'email_enabled',False)
        self.execute()
        self.assertFalse(self.f.smtp.messages)
        self.assertEqual(resend.read_attempt(self.f.s,self.f.plan)['status'],'UNSENT')
        self.assert_original_unchanged()

    def test_remaining_first_sends_are_available_even_after_uncertain_resend(self):
        self.enable(); self.f.smtp.results=[TimeoutError('synthetic new uncertainty')]
        self.execute(); old=resend.read_attempt(self.f.s,self.f.plan)
        self.f.run_pilot(first_sends_only=True); self.f.run_pilot(first_sends_only=True)
        self.assertEqual(resend.read_attempt(self.f.s,self.f.plan),old)
        self.assertEqual(self.f.receipt()[0],'INTENT')
        self.assertEqual(self.f.row(),self.original_row)
        self.assertEqual(len(self.f.smtp.messages),3)
        self.assertEqual([str(m['To']) for m in self.f.smtp.messages[1:]],
                         [self.f.row(1)['email'],self.f.row(2)['email']])

    def test_provider_acceptance_resolution_is_separate_and_cannot_be_downgraded(self):
        self.enable(); self.f.smtp.results=[TimeoutError('synthetic DATA uncertainty')]
        self.execute(); self.f.s.email_enabled=False
        self.resolve('accepted')
        with self.assertRaises(ConfigurationError): self.resolve('not-sent')
        self.assertEqual(resend.read_attempt(self.f.s,self.f.plan)['status'],'ACCEPTED')
        self.assert_original_unchanged()

    def test_provider_acceptance_is_recorded_even_after_later_cancellation_without_clearing_it(self):
        self.enable(); self.f.smtp.results=[TimeoutError('synthetic uncertainty')]
        self.execute(); self.f.s.email_enabled=False
        self.f.row()['ticket_status']='CANCELLED'; self.f.reader.fail_reads=True
        self.resolve('accepted')
        self.assertEqual(self.f.row()['ticket_status'],'CANCELLED')
        self.assertEqual(resend.read_attempt(self.f.s,self.f.plan)['status'],'ACCEPTED')
        self.assertEqual(self.f.receipt()[0],'INTENT')
        self.f.row()['ticket_status']='ACTIVE'; self.f.reader.fail_reads=False
        self.assert_original_unchanged()

    def test_rows_three_and_four_resume_normally_without_touching_either_row_two_attempt(self):
        self.enable(); self.execute()
        before_resend=resend.read_attempt(self.f.s,self.f.plan)
        self.f.policy['enabled']=True
        result=self.f.run_pilot(first_sends_only=True)
        self.assertIn('outside remaining first sends',result[0])
        self.assertEqual(result[1:],['sent','sent'])
        self.assertEqual(self.f.row(),self.original_row)
        self.assertEqual(resend.read_attempt(self.f.s,self.f.plan),before_resend)
        self.assertEqual(self.f.receipt()[0],'INTENT')
        self.f.run_pilot(first_sends_only=True)
        self.assertEqual(len(self.f.smtp.messages),3)
        self.assertEqual(len(self.f.renderer.calls),self.original_renderer_calls+2)

    def test_cli_narrow_target_acknowledgement_and_local_activation_controls(self):
        defaults={key:None for key in resend.OPERATIONS}
        args=SimpleNamespace(**{**defaults,'resend':'discount_15:row:3'},plan=self.f.path,
                             outcome=None,evidence=None,authorize_pilot=True,acknowledge_duplicate=True)
        with self.assertRaises(ConfigurationError): resend.run_command(self.f.s,args)
        args.resend=resend.TARGET; args.acknowledge_duplicate=False
        with self.assertRaises(ConfigurationError): resend.run_command(self.f.s,args)
        args.resend=None; args.activate_resend=resend.TARGET; args.acknowledge_duplicate=True
        with redirect_stdout(io.StringIO()): resend.run_command(self.f.s,args)
        self.assertTrue(json.loads(resend.policy_path(self.f.s).read_text())['enabled'])
        self.assertFalse(self.f.s.email_enabled)
        args.activate_resend=None; args.deactivate_resend=resend.TARGET
        with redirect_stdout(io.StringIO()): resend.run_command(self.f.s,args)
        self.assertFalse(json.loads(resend.policy_path(self.f.s).read_text())['enabled'])
        self.assert_original_unchanged()


class InstrumentedResendTests(unittest.TestCase):
    def test_real_read_pipeline_reuses_ticket_and_mime_without_any_row_two_write(self):
        from tests.sheet_transport import FakeClock,SheetsAPI,quota_error
        from event_checkin.google_reads import ReadScheduler
        f=test_pilot.PilotTests(); f.setUp()
        self.addCleanup(f.doCleanups); self.addCleanup(f.tearDown)
        clock=FakeClock(); api=SheetsAPI(f,clock)
        scheduler=ReadScheduler(clock=clock.monotonic,sleep=clock.sleep,progress=lambda message:None)
        with patch.object(pilot,'PLAN_SHA256',f.policy['plan_sha256']),patch('gspread.service_account',side_effect=api.client):
            reader=pilot.PilotReader(f.s,read_scheduler=scheduler)
            f.enable()
            def interrupt_original(call):
                if (f.s.runtime/'production-batches.sqlite3').exists():
                    receipt=pilot.existing(f.s,f.plan,f.plan['entries'][0])[1]
                    if receipt and receipt[0]=='INTENT': raise quota_error()
            api.hook=interrupt_original
            with self.assertRaises(Exception):
                pilot.execute(f.s,f.path,reader=reader,renderer=f.renderer,reload_settings=lambda:f.s)
            self.assertEqual(f.receipt()[0],'INTENT')
            self.assertFalse(f.smtp.messages)
            api.hook=None; f.s.email_enabled=False; f.policy['enabled']=False
            pilot.private_json(pilot.policy_path(f.s),f.policy)
            scope=resend.prepare(f.s,f.path)
            original=deepcopy(f.reader.sheet.data); oldwrites=sum(c['method']!='get' for c in api.calls)
            f.enable(); scope['enabled']=True; pilot.private_json(resend.policy_path(f.s),scope)
            result=resend.execute(f.s,f.path,acknowledged=True,reader=reader,reload_settings=lambda:f.s)
            self.assertIn('SMTP accepted',result)
            self.assertEqual(f.reader.sheet.data,original)
            self.assertEqual(sum(c['method']!='get' for c in api.calls),oldwrites)
            self.assertEqual(f.receipt()[0],'INTENT')
            self.assertEqual(len(f.smtp.messages),1)
            self.assertEqual(resend.read_attempt(f.s,f.plan)['status'],'ACCEPTED')
            self.assertTrue(all(b['time']-a['time']>=1.25 for a,b in zip(
                [c for c in api.calls if c['method']=='get'],[c for c in api.calls if c['method']=='get'][1:])))


if __name__=='__main__': unittest.main()
