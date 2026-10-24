"""Organization inbox tests use fictional data, temporary artifacts and fake SMTP."""
import argparse
import io
import json
import smtplib
import tempfile
import unittest
from copy import copy
from email import policy
from email.parser import BytesParser
from pathlib import Path
from unittest.mock import patch

import qrcode
from PIL import Image

from event_checkin.config import ROOT, Settings, ConfigurationError, load_settings
from event_checkin.mailer import SMTPMailer
from event_checkin.organization_inbox import (BADGE, NOTICE, SENDER, InboxSettings, build_test_message,
    prepare, send_prepared, register, validate_recipient)
from event_checkin.test_identity import is_test_token


class FakeRenderer:
    def __init__(self,settings): self.settings = settings

    def render(self,pid,name,token,ticket_tier=None):
        directory = self.settings.generated/pid; directory.mkdir(parents=True,mode=0o700)
        path = directory/'card.png'
        image = Image.new('RGB',(1200,900),'white')
        qr = qrcode.make(token).convert('RGB')
        image.paste(qr,(378,228)); image.save(path)
        return path


class OrganizationInboxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.s = Settings(root=ROOT,profile='production',runtime=self.root/'runtime-production',
            generated=self.root/'generated-production',template=ROOT/'templates/event-badge-final.coat',
            qr_x=378,qr_y=228,qr_size=444,smtp_user=SENDER,smtp_from=SENDER,
            smtp_host='smtp.example.invalid',smtp_password='FICTIONAL-ONLY',email_enabled=False)
        self.output = self.s.runtime/'organization-inbox-tests/new-test'
        self.recipient = 'reviewer@example.invalid'

    def prepared(self,tier='Regular',actual=False):
        if actual:
            return prepare(self.s,self.recipient,self.output,tier)
        with patch('event_checkin.organization_inbox.CardRenderer',FakeRenderer), \
             patch('event_checkin.organization_inbox.verify_render',return_value={'name_pixels_verified':True,'tier_pixels_verified':True}):
            return prepare(self.s,self.recipient,self.output,tier)

    def send(self,authorized=True,reload=None):
        return send_prepared(self.s,self.recipient,self.output,authorized,reload_settings=reload or (lambda:self.s))

    def test_actual_final_render_invalid_qr_fonts_mime_and_no_network(self):
        with patch('socket.create_connection',side_effect=AssertionError('Network forbidden')), \
             patch('gspread.service_account',side_effect=AssertionError('Sheets forbidden')), \
             patch('smtplib.SMTP') as smtp,patch('smtplib.SMTP_SSL') as smtp_ssl:
            result = self.prepared(actual=True)
            smtp.assert_not_called(); smtp_ssl.assert_not_called()
        self.assertEqual(result['status'],'PREPARED'); self.assertFalse(result['google_accessed'])
        manifest = json.loads((self.output/'manifest.json').read_text())
        self.assertTrue(manifest['badge_verification']['name_pixels_verified'])
        self.assertTrue(manifest['badge_verification']['tier_pixels_verified'])
        self.assertTrue(manifest['invalid_qr_verified']); self.assertTrue(is_test_token(manifest['test_token']))
        self.assertEqual(set(manifest['font_sha256']),{'400','700'})
        self.assertTrue(all(v=='UNVERIFIED' for v in manifest['remote_assets'].values()))
        with Image.open(self.output/BADGE) as image: self.assertEqual(image.size,(1200,974))
        self.assertEqual((self.output/'manifest.json').stat().st_mode&0o777,0o600)
        for path in (self.output,*self.output.rglob('*')):
            self.assertEqual(path.stat().st_mode&0o777,0o700 if path.is_dir() else 0o600)
        self.assertFalse(list(self.output.glob('attempt*')))

    def test_all_tiers_offline_test_notice_escaping_cid_and_attachment(self):
        self.prepared()
        for tier in ('Lite','Regular','Pro'):
            with patch('socket.create_connection',side_effect=AssertionError('No network')):
                message = build_test_message(self.recipient,self.output/BADGE,'3eecbd77-01f1-45ec-a0ee-8fefbf991dad',tier)
            parsed = BytesParser(policy=policy.default).parsebytes(message.as_bytes(policy=policy.SMTP))
            self.assertEqual(parsed['From'].addresses[0].addr_spec,SENDER)
            self.assertEqual(parsed['To'].addresses[0].addr_spec,self.recipient)
            self.assertTrue(str(parsed['Subject']).startswith('[TEST — INVALID BADGE]'))
            plain=parsed.get_body(preferencelist=('plain',)).get_content()
            html=parsed.get_body(preferencelist=('html',)).get_content()
            self.assertIn(NOTICE,plain); self.assertIn(NOTICE,html)
            self.assertIn("Avery O&#x27;Connell &amp; Lane",html); self.assertIn('Pass type: '+tier,plain)
            self.assertIn('cid:',html); self.assertNotIn('{{',html)
            images=[p for p in parsed.walk() if p.get_content_type()=='image/png']
            self.assertEqual({p.get_content_disposition() for p in images},{'inline','attachment'})
            self.assertEqual(len(images),2)
            self.assertTrue(all(p.get_payload(decode=True)==(self.output/BADGE).read_bytes() for p in images))
        self.assertNotIn('cid:',(self.output/'preview.html').read_text())
        self.assertIn('src="'+BADGE+'"',(self.output/'preview.html').read_text())

    def test_false_flag_blocks_before_transport_even_if_require_email_is_overridden(self):
        self.prepared()
        with patch('smtplib.SMTP') as smtp,patch('smtplib.SMTP_SSL') as smtp_ssl:
            with self.assertRaises(ConfigurationError): self.send()
            with patch.object(self.s,'require_email'):
                with self.assertRaises(ConfigurationError): SMTPMailer(self.s).submit_message(object())
            smtp.assert_not_called();smtp_ssl.assert_not_called()
        self.assertFalse((self.output/'attempt.json').exists())

    def test_authorization_and_exact_sender_required_before_any_smtp(self):
        self.prepared();self.s.email_enabled=True
        with patch('smtplib.SMTP') as smtp,patch('smtplib.SMTP_SSL') as smtp_ssl:
            with self.assertRaises(ConfigurationError):self.send(False)
            for field,value in (('smtp_user','personal@example.invalid'),('smtp_from','Personal <'+SENDER+'>')):
                old=getattr(self.s,field);setattr(self.s,field,value)
                with self.assertRaises(ConfigurationError):self.send()
                setattr(self.s,field,old)
            smtp.assert_not_called();smtp_ssl.assert_not_called()
        self.assertFalse((self.output/'attempt.json').exists())

    def test_one_accepted_submission_and_repeated_command_is_blocked(self):
        self.prepared();self.s.email_enabled=True
        before={p:p.read_bytes() for p in self.output.glob('*') if p.is_file()}
        with patch('smtplib.SMTP') as smtp:
            self.assertEqual(self.send()['status'],'SMTP_ACCEPTED')
            sent=smtp.return_value.send_message.call_args.args[0]
            self.assertEqual(sent['To'].addresses[0].addr_spec,self.recipient)
            self.assertEqual(sent['Message-ID'],json.loads((self.output/'manifest.json').read_text())['message_id'])
            with self.assertRaises(ConfigurationError):self.send()
            smtp.return_value.send_message.assert_called_once()
            smtp.assert_called_once()
        for path,data in before.items():self.assertEqual(path.read_bytes(),data)
        self.assertTrue(json.loads((self.output/'smtp-single_entry_smtp_accepted.json').read_text())['accepted'])

    def test_known_rejection_and_uncertainty_do_not_resubmit(self):
        for index,(failure,status) in enumerate(((smtplib.SMTPDataError(550,b'PRIVATE-RESPONSE'),'NOT_ACCEPTED'),
                (smtplib.SMTPServerDisconnected('PRIVATE-RESPONSE'),'UNCERTAIN'))):
            self.s.email_enabled=False; self.output=self.output.with_name('case-'+str(index));self.prepared()
            self.s.email_enabled=True
            with patch('smtplib.SMTP') as smtp:
                smtp.return_value.send_message.side_effect=failure
                self.assertEqual(self.send()['status'],status)
                with self.assertRaises(ConfigurationError):self.send()
                smtp.return_value.send_message.assert_called_once();smtp.assert_called_once()
            for path in self.output.glob('smtp-*.json'): self.assertNotIn('PRIVATE-RESPONSE',path.read_text())

    def test_connect_failure_known_unsent_and_close_error_does_not_hide_acceptance(self):
        self.prepared();self.s.email_enabled=True
        with patch('smtplib.SMTP',side_effect=OSError('PRIVATE-RESPONSE')) as smtp:
            self.assertEqual(self.send()['status'],'NOT_ACCEPTED');smtp.assert_called_once()
        self.s.email_enabled=False;self.output=self.output.with_name('close-failure');self.prepared()
        self.s.email_enabled=True
        with patch('smtplib.SMTP') as smtp:
            smtp.return_value.close.side_effect=OSError('PRIVATE-RESPONSE')
            self.assertEqual(self.send()['status'],'SMTP_ACCEPTED')
            smtp.return_value.send_message.assert_called_once()

    def test_revocation_after_authentication_prevents_data(self):
        self.prepared();self.s.email_enabled=True
        current=copy(self.s)
        with patch('smtplib.SMTP') as smtp:
            smtp.return_value.login.side_effect=lambda *args:setattr(current,'email_enabled',False)
            self.assertEqual(self.send(reload=lambda:current)['status'],'NOT_ACCEPTED')
            smtp.return_value.send_message.assert_not_called()

    def test_sender_change_after_authentication_prevents_data(self):
        self.prepared();self.s.email_enabled=True
        current=copy(self.s)
        with patch('smtplib.SMTP') as smtp:
            smtp.return_value.login.side_effect=lambda *args:setattr(current,'smtp_from','personal@example.invalid')
            self.assertEqual(self.send(reload=lambda:current)['status'],'NOT_ACCEPTED')
            smtp.return_value.send_message.assert_not_called()

    def test_recipient_mismatch_or_tampered_artifacts_never_connect(self):
        self.prepared();self.s.email_enabled=True
        with patch('smtplib.SMTP') as smtp:
            with self.assertRaises(ConfigurationError):
                send_prepared(self.s,'different@example.invalid',self.output,True,reload_settings=lambda:self.s)
            (self.output/'confirmation.eml').write_bytes(b'tampered')
            with self.assertRaises(ConfigurationError):self.send()
            smtp.assert_not_called()

    def test_interrupted_attempt_without_completion_is_not_resumed(self):
        self.prepared();self.s.email_enabled=True
        (self.output/'attempt.json').write_text('{"status":"STARTED"}')
        with patch('smtplib.SMTP') as smtp:
            with self.assertRaises(ConfigurationError):self.send()
            smtp.assert_not_called()

    def test_general_operations_and_old_scope_stay_locked(self):
        self.s.email_enabled=True
        scoped=InboxSettings(self.s,lambda:self.s)
        scoped.require_email()
        with self.assertRaises(ConfigurationError):scoped.require_operational()
        with self.assertRaises(ConfigurationError):scoped.require_sheet()
        with self.assertRaises(ConfigurationError):scoped.require_staff()

    def test_recipient_lists_injection_and_missing_explicit_option_are_rejected(self):
        for value in ('a@example.invalid,b@example.invalid','a@example.invalid;b@example.invalid',
                      'Name <a@example.invalid>','a@example.invalid\nBcc: b@example.invalid',''):
            with self.assertRaises(ConfigurationError):validate_recipient(value)
        parser=argparse.ArgumentParser();commands=parser.add_subparsers(dest='command',required=True);register(commands)
        with patch('sys.stderr',io.StringIO()),self.assertRaises(SystemExit):
            parser.parse_args(['production-inbox-test','--send','--out',str(self.output),'--authorize-send'])
        with patch('sys.stderr',io.StringIO()),self.assertRaises(SystemExit):
            parser.parse_args(['production-inbox-test','--send','--out',str(self.output),
                              '--to','one@example.invalid','--to','two@example.invalid','--authorize-send'])

    def test_new_output_required_and_existing_preview_preserved(self):
        self.prepared();saved=(self.output/'confirmation.eml').read_bytes()
        with self.assertRaises(FileExistsError):self.prepared()
        self.assertEqual((self.output/'confirmation.eml').read_bytes(),saved)
        self.output=self.s.runtime/'single-entry-regular-row2'
        with self.assertRaises(ConfigurationError):self.prepared()

    def test_enabled_loading_is_narrow_and_keeps_operational_lock(self):
        root=self.root/'config-test';root.mkdir()
        (root/'.env.production').write_text('EMAIL_ENABLED=true\nEVENT_ID=production-synthetic\n')
        with self.assertRaises(ConfigurationError):load_settings('production',root)
        settings=load_settings('production',root,inbox_test=True)
        self.assertTrue(settings.email_enabled)
        with self.assertRaises(ConfigurationError):settings.require_operational()
        with self.assertRaises(ConfigurationError):settings.require_email()


if __name__=='__main__':unittest.main()
