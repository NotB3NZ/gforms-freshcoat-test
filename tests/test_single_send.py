"""Fake Sheets, fake SMTP, private temporary journals: no live sending or issuance."""
import json
import smtplib
import tempfile
import unittest
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image

from event_checkin.config import ROOT, Settings, ConfigurationError, load_settings
from event_checkin.header_init import EVENT_ID
from event_checkin.mailer import SMTPMailer, EmailNotSent, EmailAmbiguous
from event_checkin.production import private_json
from event_checkin.production_sheets import PRODUCTION_METADATA, ProductionSheets
from event_checkin.single_send import (TEMPLATE_SHA256, SOURCE, NUMBER, SingleSettings, SingleSheet,
    checksum, scope_path, prepare, read_policy, execute, load_selection, original_action)
from tests.test_production import Sheet, record


class Reader:
    live = False
    dataset = 'synthetic-single-entry-tests'

    def __init__(self, settings):
        self.settings = settings
        self.sheet = Sheet(regular=[record(name='Synthetic One Entry', email='one@example.invalid', event_id=EVENT_ID)])

    def rows(self):
        return self.sheet.rows()

    def capture(self, source, number):
        if (source,number)!=(SOURCE,NUMBER): raise AssertionError('Wrong row')
        row=self.rows()[0]
        return {'source':source,'row':number,'headers':{'synthetic':True},
                'cells':{key:index+24 for index,key in enumerate(PRODUCTION_METADATA)},
                'values':deepcopy(row.data)}

    def write(self, snapshot, changes):
        if snapshot!=self.capture(SOURCE,NUMBER): raise AssertionError('Changed cells')
        self.sheet.update(self.rows()[0],changes)


class Renderer:
    def __init__(self, settings): self.settings=settings; self.calls=[]; self.hook=None

    def render(self,pid,name,token,ticket_tier=None):
        self.calls.append((pid,name,token,ticket_tier))
        if self.hook: self.hook()
        path=self.settings.generated/pid/'card.png'; path.parent.mkdir(parents=True,exist_ok=True)
        Image.new('RGB',(1200,900),'white').save(path)
        return path


class SingleSendTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name); (self.root/'config').mkdir()
        (self.root/'config/sources.json').write_text('{}')
        (self.root/'config/production-header-init.approved.json').write_text('{}')
        self.s=Settings(root=self.root,profile='production',event_id=EVENT_ID,
            runtime=self.root/'runtime-production',generated=self.root/'generated-production',
            template=ROOT/'templates/event-badge-final.coat',sources_file=self.root/'config/sources.json',
            qr_x=378,qr_y=228,qr_size=444,email_enabled=False,
            smtp_from='personal@example.invalid',smtp_user='personal@example.invalid',
            smtp_host='smtp.example.invalid',smtp_password='synthetic-password')
        self.policy={'version':1,'enabled':False,'event_id':EVENT_ID,'source':SOURCE,'row':NUMBER,
            'sender':'personal@example.invalid','template_sha256':TEMPLATE_SHA256,
            'manifest_sha256':checksum(self.s.sources_file),
            'headers_sha256':checksum(self.root/'config/production-header-init.approved.json')}
        private_json(scope_path(self.s),self.policy)
        self.reader=Reader(self.s); self.renderer=Renderer(self.s)
        self.selection=self.s.runtime/'single-entry/selection.json'
        self.verify=patch('event_checkin.single_send.verify_render',return_value={'qr_decoded':True})
        self.verify.start(); self.addCleanup(self.verify.stop)

    def ready(self):
        report=prepare(self.s,self.selection,self.reader)
        self.assertTrue(report['eligible'])
        policy=read_policy(self.s); policy['enabled']=True; private_json(scope_path(self.s),policy)
        self.s.email_enabled=True

    def run_mode(self,mode='apply'):
        return execute(self.s,self.selection,mode,reader=self.reader,renderer=self.renderer,write_transport=self.reader.write)

    def test_preparation_is_read_only_without_credentials_or_identity_journal(self):
        self.s.smtp_password=''
        report=prepare(self.s,self.selection,self.reader)
        self.assertTrue(report['eligible'])
        self.assertEqual(set(report['approvals'].values()),{'TRUE'})
        self.assertFalse(self.reader.sheet.attempts)
        self.assertFalse((self.s.runtime/'production-batches.sqlite3').exists())
        self.assertFalse((self.s.runtime/'issuance.sqlite3').exists())
        self.assertFalse(self.renderer.calls)

    def test_every_approval_and_duplicate_precondition_blocks_preparation(self):
        for key in ('confirmation','reference_matched','price_matched','payment_received'):
            for value in (False,'FALSE','',1,'true'):
                with self.subTest(key=key,value=value):
                    self.reader=Reader(self.s); self.reader.sheet.data['sources']['regular'][0][key]=value
                    report=prepare(self.s,self.selection,self.reader)
                    self.assertTrue(report['blockers']); self.assertEqual(report['approvals'][key],'not TRUE')
                    self.assertFalse(self.selection.exists()); self.assertFalse(self.reader.sheet.attempts)
        self.reader=Reader(self.s)
        other=record(name='Different Synthetic',email='one@example.invalid',event_id=EVENT_ID)
        self.reader.sheet.data['sources']['discount_15']=[other]
        self.assertTrue(prepare(self.s,self.selection,self.reader)['blockers'])

    def test_false_flag_blocks_all_send_modes_before_io_even_if_require_email_is_overridden(self):
        self.ready(); self.s.email_enabled=False
        for mode in ('apply','retry'):
            with patch('event_checkin.single_send.read_adapter') as read, patch('smtplib.SMTP') as smtp:
                with self.assertRaises(ConfigurationError): execute(self.s,self.selection,mode)
                read.assert_not_called(); smtp.assert_not_called()
        self.assertFalse((self.s.runtime/'production-batches.sqlite3').exists())
        mailer=SMTPMailer(self.s)
        with patch.object(self.s,'require_email'), patch('smtplib.SMTP') as smtp, patch('smtplib.SMTP_SSL') as ssl:
            with self.assertRaises(ConfigurationError): mailer.send('one@example.invalid','Name','P-X',Path('missing'),'<x@example.invalid>')
            smtp.assert_not_called(); ssl.assert_not_called()

    def test_false_scope_wrong_sender_and_general_production_locks(self):
        self.ready()
        policy=read_policy(self.s); policy['enabled']=False; private_json(scope_path(self.s),policy)
        with self.assertRaises(ConfigurationError): self.run_mode()
        policy['enabled']=True; private_json(scope_path(self.s),policy)
        self.s.smtp_from='organization@example.invalid'
        with self.assertRaises(ConfigurationError): self.run_mode()
        scoped=SingleSettings(self.s,policy,'apply')
        with self.assertRaises(ConfigurationError): scoped.require_operational()
        with self.assertRaises(ConfigurationError): ProductionSheets(scoped,writable=True)

    def test_one_send_cid_attachment_and_repeat_are_idempotent(self):
        self.ready()
        with patch('smtplib.SMTP') as smtp:
            self.assertEqual(self.run_mode(),['sent'])
            message=smtp.return_value.send_message.call_args.args[0]
            self.assertEqual(message['From'].addresses[0].addr_spec,self.policy['sender'])
            self.assertEqual(message['To'].addresses[0].addr_spec,'one@example.invalid')
            self.assertIn('cid:',message.get_body(preferencelist=('html',)).get_content())
            self.assertEqual(len([p for p in message.walk() if p.get_content_type()=='image/png']),2)
            self.assertEqual(self.run_mode(),['completed previously'])
            smtp.return_value.send_message.assert_called_once()
        self.assertEqual(len(self.renderer.calls),1)
        self.assertEqual(self.reader.sheet.data['sources']['regular'][0]['email_status'],'SENT')

    def test_known_unsent_retry_keeps_identity_card_and_message_id(self):
        self.ready()
        with patch('smtplib.SMTP') as smtp:
            smtp.return_value.send_message.side_effect=smtplib.SMTPDataError(550,b'synthetic rejection')
            self.assertEqual(self.run_mode(),['email error'])
            first=smtp.return_value.send_message.call_args.args[0]['Message-ID']
            identity=self.renderer.calls[0][:3]
            smtp.return_value.send_message.side_effect=None
            self.assertEqual(self.run_mode('retry'),['sent'])
            self.assertEqual(smtp.return_value.send_message.call_args.args[0]['Message-ID'],first)
            self.assertEqual(self.renderer.calls[0][:3],identity); self.assertEqual(len(self.renderer.calls),1)

    def test_uncertain_delivery_never_retries_and_can_be_resolved_without_smtp(self):
        self.ready()
        with patch('smtplib.SMTP') as smtp:
            smtp.return_value.send_message.side_effect=smtplib.SMTPServerDisconnected('synthetic uncertainty')
            self.assertEqual(self.run_mode(),['review'])
            with self.assertRaises(ConfigurationError): self.run_mode('retry')
            self.assertEqual(smtp.return_value.send_message.call_count,1)
        self.s.email_enabled=False
        with patch('smtplib.SMTP') as smtp:
            processor=self.run_mode('resolve')
            selection=load_selection(self.s,self.selection,read_policy(self.s))
            with self.assertRaises(ConfigurationError): processor.execute(selection['plan'])
            with self.assertRaises(ConfigurationError): processor.resolve_email('discount_15:unrelated','sent')
            rid=original_action(self.s,selection)[0]
            processor.resolve_email('regular:'+rid,'sent')
            smtp.assert_not_called()
        self.assertEqual(self.reader.sheet.data['sources']['regular'][0]['email_status'],'SENT')

    def test_revocation_after_render_and_after_authentication_prevents_submission(self):
        self.ready()
        self.renderer.hook=lambda:self.reader.sheet.data['sources']['regular'][0].update(payment_received=False)
        with patch('smtplib.SMTP') as smtp:
            self.assertEqual(self.run_mode(),['blocked']); smtp.assert_not_called()

    def test_disabled_after_authentication_never_calls_send_message(self):
        self.ready()
        with patch('smtplib.SMTP') as smtp:
            smtp.return_value.login.side_effect=lambda *a:setattr(self.s,'email_enabled',False)
            with self.assertRaises(ConfigurationError): self.run_mode()
            smtp.return_value.send_message.assert_not_called()

    def test_approval_revoked_during_authentication_never_submits_or_overwrites_checkbox(self):
        self.ready()
        with patch('smtplib.SMTP') as smtp:
            smtp.return_value.login.side_effect=lambda *a:self.reader.sheet.data['sources']['regular'][0].update(confirmation=False)
            with self.assertRaises(ConfigurationError): self.run_mode()
            smtp.return_value.send_message.assert_not_called()
        self.assertIs(self.reader.sheet.data['sources']['regular'][0]['confirmation'],False)

    def test_lost_sent_write_stays_uncertain_and_never_auto_resends(self):
        self.ready()
        self.reader.sheet.fail_before=lambda changes:changes.get('email_status')=='SENT'
        with patch('smtplib.SMTP') as smtp:
            with self.assertRaises(ConnectionError): self.run_mode()
            self.assertEqual(self.run_mode(),['review'])
            with self.assertRaises(ConfigurationError): self.run_mode('retry')
            smtp.return_value.send_message.assert_called_once()

    def test_lost_identity_write_reuses_durable_claim_on_resume(self):
        self.ready()
        self.reader.sheet.fail_before=lambda changes:'ticket_token' in changes
        with patch('smtplib.SMTP') as smtp:
            with self.assertRaises(ConnectionError): self.run_mode()
            self.assertEqual(self.run_mode(),['sent'])
            smtp.return_value.send_message.assert_called_once()
        self.assertEqual(len(self.renderer.calls),1)

    def test_other_target_policy_and_multi_entry_plan_cannot_be_enabled(self):
        for change in ({'source':'discount_15'},{'row':3},{'sender':'awscc.upmindanao@gmail.com'}):
            policy=deepcopy(self.policy); policy.update(change); private_json(scope_path(self.s),policy)
            with self.assertRaises(ConfigurationError): read_policy(self.s)
        private_json(scope_path(self.s),self.policy)
        self.ready()
        data=json.loads(self.selection.read_text()); data['plan']['limit']=2
        data['plan']['entries']*=2
        private_json(self.selection,data)
        policy=read_policy(self.s); policy['selection_sha256']=checksum(self.selection); private_json(scope_path(self.s),policy)
        with self.assertRaises(ConfigurationError): self.run_mode()

    def test_changed_target_selection_other_rows_and_protected_cells_are_rejected(self):
        self.ready()
        selection=load_selection(self.s,self.selection,read_policy(self.s))
        scoped=SingleSettings(self.s,read_policy(self.s),'apply')
        sheet=SingleSheet(scoped,self.reader,selection,self.reader.write)
        row=self.reader.rows()[0]
        for key in ('name','email','confirmation','payment_received','checked_in_at','checked_in_by'):
            with self.assertRaises(ConfigurationError): sheet.update(row,{key:'changed'})
        row.number=3
        with self.assertRaises(ConfigurationError): sheet.update(row,{'last_error':'changed'})
        self.reader.sheet.data['sources']['regular'][0]['name']='Moved or changed'
        with self.assertRaises(ConfigurationError): self.run_mode()
        self.assertFalse(self.reader.sheet.attempts)
        data=json.loads(self.selection.read_text()); data['row']=3
        private_json(self.selection,data)
        with self.assertRaises(ConfigurationError): self.run_mode()

    def test_external_identity_and_local_reservation_cannot_be_adopted(self):
        self.ready()
        row=self.reader.sheet.data['sources']['regular'][0]
        row.update(record_id='R-external',participant_id='P-external',ticket_token='z'*43,
                   normalized_email=row['email'],ticket_name=row['name'],ticket_tier='Regular',
                   source_id='regular',price_category='regular')
        with self.assertRaises(ConfigurationError): self.run_mode()
        self.assertFalse(self.reader.sheet.attempts)

    def test_single_loader_does_not_unlock_general_production(self):
        (self.root/'.env.production').write_text('EVENT_ID='+EVENT_ID+'\nEMAIL_ENABLED=true\n')
        with self.assertRaises(ConfigurationError): load_settings('production',self.root)
        settings=load_settings('production',self.root,single_entry=True)
        self.assertTrue(settings.email_enabled)
        with self.assertRaises(ConfigurationError): settings.require_operational()
        with self.assertRaises(ConfigurationError): settings.require_email()
