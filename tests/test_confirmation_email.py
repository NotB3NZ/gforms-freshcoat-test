"""Offline synthetic composition and mocked-transport checks, never live mail."""
import hashlib
import smtplib
import tempfile
import unittest
from email import policy
from email.parser import BytesParser
from html import escape, unescape
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from event_checkin.config import Settings, ConfigurationError
from event_checkin.confirmation_email import (TEMPLATE, SUBJECT, MessageBuildError, build_confirmation_message,
                                             browser_preview_html)
from event_checkin.confirmation_previews import generate_previews, synthetic_examples
from event_checkin.mailer import SMTPMailer, EmailNotSent, EmailAmbiguous
from event_checkin.test_identity import is_test_token


class ConfirmationEmailTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.card = self.root / "personalized.png"
        Image.new("RGB", (1200, 900), "#123456").save(self.card)
        self.args = ("frozen.recipient@example.invalid", "Élodie O'Connell & Lane", "P-SYNTHETIC", self.card,
                     "<durable-synthetic@example.invalid>")

    def build(self, tier="Pro", **kwargs):
        return build_confirmation_message(*self.args, ticket_tier=tier, **kwargs)

    def test_all_tiers_roundtrip_actual_mime_and_full_resolution_contents(self):
        for tier in ("Lite", "Regular", "Pro"):
            with self.subTest(tier=tier):
                message = BytesParser(policy=policy.default).parsebytes(self.build(tier).as_bytes(policy=policy.SMTP))
                self.assertFalse(message.defects)
                self.assertEqual(message.get_content_type(), "multipart/mixed")
                alternative, attachment = message.get_payload()
                self.assertEqual(alternative.get_content_type(), "multipart/alternative")
                plain, related = alternative.get_payload()
                self.assertEqual(related.get_content_type(), "multipart/related")
                html_part, inline = related.get_payload()
                html = html_part.get_content()
                self.assertEqual(message["To"].addresses[0].addr_spec, self.args[0])
                self.assertEqual(message["Message-ID"], self.args[-1])
                self.assertEqual(message["Subject"], SUBJECT)
                self.assertIn("You’re in, Élodie!", html)
                self.assertIn(escape(self.args[1]), html)
                self.assertIn(self.args[0], html)
                self.assertIn(">" + tier + "</span>", html)
                self.assertIn("Pass type: " + tier, plain.get_content())
                self.assertIn("Name: " + self.args[1], plain.get_content())
                self.assertIn("Email: " + self.args[0], plain.get_content())
                self.assertIn("Participant ID: " + self.args[2], plain.get_content())
                self.assertEqual(inline.get_content_disposition(), "inline")
                self.assertEqual(attachment.get_content_disposition(), "attachment")
                self.assertEqual(attachment.get_filename(), "event-badge.png")
                self.assertEqual(html.count('src="cid:'), 1)
                self.assertIn('src="cid:' + inline["Content-ID"].strip("<>") + '"', html)
                for part in (inline, attachment):
                    self.assertEqual(part.get_content_type(), "image/png")
                    self.assertEqual(part.get_payload(decode=True), self.card.read_bytes())
                    with Image.open(BytesIO(part.get_payload(decode=True))) as image:
                        self.assertEqual(image.size, (1200, 900))
                self.assertNotIn(str(self.card), html)
                self.assertNotIn("border:2px dashed", html)
                self.assertNotIn("{{", html)

    def test_participant_text_is_escaped_including_alt_attribute(self):
        name = 'Éva <img src=x> "O\'Neil" & {{full_name}}'
        recipient = "o'connor&team@example.invalid"
        self.args = (recipient, name, *self.args[2:])
        message = self.build()
        html = message.get_body(preferencelist=("html",)).get_content()
        self.assertIn('alt="Event badge for ' + escape(name).replace("{", "&#123;").replace("}", "&#125;") + '"', html)
        self.assertNotIn("<img src=x>", html)
        self.assertIn("o&#x27;connor&amp;team@example.invalid", html)
        self.assertIn(name, unescape(html))
        self.assertNotIn("{{full_name}}", html)
        self.assertIn(name, message.get_body(preferencelist=("plain",)).get_content())

    def test_required_unknown_and_malformed_placeholders_are_rejected(self):
        original = TEMPLATE.read_text(encoding="utf-8")
        broken = [original.replace("{{" + key + "}}", "") for key in ("first_name", "full_name", "email", "pass_type")]
        broken += [original + "{{unknown_required}}", original + "{{full_name", original + "unclosed}}",
                   original.replace("border:2px dashed", "border:2px solid"), original.replace("{{badge_image_url}}", "")]
        path = self.root / "bad.html"
        for text in broken:
            path.write_text(text, encoding="utf-8")
            with self.subTest(text=text[-50:]), self.assertRaises(MessageBuildError):
                self.build(template_path=path)

    def test_invalid_tiers_missing_files_and_header_injection_fail_before_smtp(self):
        for tier in (None, "", "pro", "VIP", "Lite (₱170)"):
            with self.subTest(tier=tier), self.assertRaises(MessageBuildError):
                self.build(tier)
        for index, value in ((0, "a@example.invalid\r\nBcc: x@example.invalid"),
                             (0, "a@example.invalid,b@example.invalid"), (1, "\n"),
                             (2, "unsafe/path"), (4, "<x@example.invalid>\nBcc: x")):
            args = list(self.args)
            args[index] = value
            with self.subTest(index=index), self.assertRaises(MessageBuildError):
                build_confirmation_message(*args, ticket_tier="Lite")
        self.card.write_bytes(b"not a PNG")
        with self.assertRaises(MessageBuildError):
            self.build()

    def test_offline_builder_needs_no_credentials_or_enabled_flag(self):
        settings = Settings(profile="production", email_enabled=False)
        with patch("socket.create_connection", side_effect=AssertionError("Network prohibited")), \
                patch("event_checkin.mailer.smtplib.SMTP") as smtp, \
                patch("event_checkin.mailer.smtplib.SMTP_SSL") as smtp_ssl:
            message = SMTPMailer(settings).build_message(*self.args, ticket_tier="Regular")
            self.assertEqual(message["To"].addresses[0].addr_spec, self.args[0])
            self.assertIsNone(message["From"])
            smtp.assert_not_called()
            smtp_ssl.assert_not_called()
            with self.assertRaises(MessageBuildError):
                SMTPMailer(settings).build_message(*self.args)  # Never silently send legacy production mail.

    def test_browser_paths_are_separate_from_content_ids(self):
        message = self.build()
        original = message.as_bytes()
        html = browser_preview_html(message, "INVALID-SYNTHETIC-BADGE.png")
        self.assertIn('src="INVALID-SYNTHETIC-BADGE.png"', html)
        self.assertNotIn("cid:", html)
        self.assertEqual(message.as_bytes(), original)
        self.assertIn("cid:", message.get_body(preferencelist=("html",)).get_content())
        for path in ("../badge.png", "https://example.invalid/badge.png", 'bad".png'):
            with self.assertRaises(MessageBuildError):
                browser_preview_html(message, path)

    def test_poster_copy_and_fallbacks_are_preserved_without_changing_sources(self):
        original = TEMPLATE.read_bytes()
        html = self.build().get_body(preferencelist=("html",)).get_content()
        for literal in ('https://lh3.googleusercontent.com/d/1JutOyh5ZU608Lp6jYisr6NC2bL0mQrDf=w1200',
                        "https://fonts.googleapis.com/css2?family=Playfair+Display:ital,wght@1,600;1,700;1,800&amp;display=swap",
                        "font-family:'Playfair Display',Georgia,'Times New Roman',serif",
                        "Saturday, November 28, 2026", "Finster Auditorium", "<!--[if mso]>"):
            self.assertIn(literal.replace("&amp;display", "&display"), html)
        self.assertEqual(TEMPLATE.read_bytes(), original)

    def test_rebuild_reuses_durable_message_id_and_cid(self):
        a, b = self.build(), self.build()
        self.assertEqual(a["Message-ID"], b["Message-ID"])
        inline = lambda m: next(part for part in m.walk() if part.get_content_disposition() == "inline")
        self.assertEqual(inline(a)["Content-ID"], inline(b)["Content-ID"])
        self.assertEqual(inline(a).get_payload(decode=True), inline(b).get_payload(decode=True))

    def test_legacy_builder_and_five_argument_interface_still_work(self):
        message = SMTPMailer(Settings()).build_message(*self.args)
        self.assertEqual(message["Subject"], "Your event registration card")
        self.assertEqual(message["Message-ID"], self.args[-1])
        self.assertIsNone(message.get_body(preferencelist=("html",)))
        self.assertEqual(len(list(message.iter_attachments())), 1)
        self.assertEqual(next(message.iter_attachments()).get_filename(), "event-card.png")

    def test_html_transport_keeps_rejection_uncertainty_and_message_ids(self):
        mailer = SMTPMailer(Settings(email_enabled=True))  # Fake transport still requires explicit opt-in.
        for exception, expected in ((smtplib.SMTPDataError(550, b"synthetic rejection"), EmailNotSent),
                                    (smtplib.SMTPServerDisconnected("synthetic disconnect"), EmailAmbiguous),
                                    (None, None)):
            with patch.object(mailer.settings, "require_email"), patch("event_checkin.mailer.smtplib.SMTP") as smtp:
                smtp.return_value.send_message.side_effect = exception
                if expected:
                    with self.assertRaises(expected):
                        mailer.send(*self.args, ticket_tier="Lite")
                else:
                    mailer.send(*self.args, ticket_tier="Lite")
                sent = smtp.return_value.send_message.call_args.args[0]
                self.assertEqual(sent["Message-ID"], self.args[-1])
                self.assertIn("cid:", sent.get_body(preferencelist=("html",)).get_content())
                smtp.return_value.close.assert_called_once()

    def test_composition_failure_is_known_unsent_and_disabled_send_stays_locked(self):
        mailer = SMTPMailer(Settings(profile="production", email_enabled=False))
        with patch("event_checkin.mailer.smtplib.SMTP") as smtp, patch("event_checkin.mailer.smtplib.SMTP_SSL") as smtp_ssl:
            with self.assertRaises(ConfigurationError):
                mailer.send(*self.args, ticket_tier="Lite")
            with patch.object(mailer.settings, "require_email"):
                with self.assertRaises(ConfigurationError):
                    mailer.send(*self.args, ticket_tier="Unknown")
                mailer.settings.email_enabled = True
                with self.assertRaises(EmailNotSent):
                    mailer.send(*self.args, ticket_tier="Unknown")
            smtp.assert_not_called()
            smtp_ssl.assert_not_called()


class ActualConfirmationPreviews(unittest.TestCase):
    def test_three_synthetic_tiers_verified_cards_and_mime(self):
        import cv2
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "new-previews"
            with patch("socket.create_connection", side_effect=AssertionError("No network")), \
                    patch("event_checkin.mailer.smtplib.SMTP") as smtp, \
                    patch("event_checkin.mailer.smtplib.SMTP_SSL") as smtp_ssl:
                report = generate_previews(root)
                smtp.assert_not_called()
                smtp_ssl.assert_not_called()
            self.assertEqual(len(report["examples"]), 3)
            self.assertTrue(all(value == "UNVERIFIED" for value in report["remote_assets"].values()))
            for example, (tier, name, recipient) in zip(report["examples"], synthetic_examples()):
                self.assertTrue(example["badge_verification"]["name_pixels_verified"])
                self.assertTrue(example["badge_verification"]["tier_pixels_verified"])
                token = cv2.QRCodeDetector().detectAndDecode(cv2.imread(example["badge"]))[0]
                self.assertTrue(is_test_token(token))
                self.assertTrue(20 <= len(name) <= 25)
                parsed = BytesParser(policy=policy.default).parsebytes(Path(example["eml"]).read_bytes())
                self.assertEqual(parsed["To"].addresses[0].addr_spec, recipient)
                self.assertIn(escape(name), parsed.get_body(preferencelist=("html",)).get_content())
                self.assertIn("Pass type: " + tier, parsed.get_body(preferencelist=("plain",)).get_content())
                self.assertTrue(recipient.endswith("@example.invalid"))
                for part in parsed.walk():
                    if part.get_content_type() == "image/png":
                        self.assertEqual(hashlib.sha256(part.get_payload(decode=True)).hexdigest(), example["png_sha256"])
                self.assertNotIn("cid:", Path(example["html"]).read_text())
            saved = (root / "report.json").read_bytes()
            with self.assertRaises(FileExistsError):
                generate_previews(root)
            self.assertEqual((root / "report.json").read_bytes(), saved)
