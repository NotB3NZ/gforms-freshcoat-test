import json
import subprocess
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from event_checkin.config import ROOT, Settings, DEFAULT_HEADERS, ConfigurationError
from event_checkin.sheets import GoogleSheet, SheetIntegrityError
from event_checkin.mailer import SMTPMailer, EmailNotSent, EmailAmbiguous
from tests.test_system import Base


class SheetAdapterTests(unittest.TestCase):
    def adapter(self, rows=None):
        class Worksheet:
            col_count = 2
            values = deepcopy(rows or [["Full Name", "Email Address"], ["Example", "example@example.test"]])
            calls = []
            def row_values(inner, number):
                return deepcopy(inner.values[number - 1])
            def get_all_values(inner):
                return deepcopy(inner.values)
            def add_cols(inner, count):
                inner.col_count += count
            def update(inner, range_name, values, value_input_option):
                inner.calls.append((range_name, values, value_input_option))
                inner.values[0] += values[0]
            def batch_update(inner, data, value_input_option):
                inner.calls.append((deepcopy(data), value_input_option))
        adapter = GoogleSheet.__new__(GoogleSheet)
        adapter.ws = Worksheet()
        adapter.settings = Settings()
        return adapter

    def test_initialization_is_idempotent_and_does_not_queue(self):
        sheet = self.adapter()
        original_form_data = deepcopy(sheet.ws.values[1])
        sheet.initialize()
        sheet.initialize()
        self.assertEqual(sheet.ws.values[1], original_form_data)
        self.assertEqual(len(sheet.ws.calls), 1)
        self.assertEqual(sheet.ws.calls[0][-1], "RAW")
        self.assertEqual(sheet.rows()[0].get("state"), "")

    def test_metadata_updates_are_raw_and_do_not_overwrite_attendance(self):
        sheet = self.adapter()
        sheet.initialize()
        row = sheet.rows()[0]
        sheet.update(row, {"participant_id": "P-test", "ticket_token": "a" * 43, "state": "PROCESSING"})
        data, mode = sheet.ws.calls[-1]
        self.assertEqual(mode, "RAW")
        self.assertEqual(len(data), 3)
        checked_column = list(DEFAULT_HEADERS).index("checked_in_at") + 1
        from gspread.utils import rowcol_to_a1
        self.assertNotIn(rowcol_to_a1(2, checked_column), [d["range"] for d in data])
        with self.assertRaises(SheetIntegrityError):
            sheet.update(row, {"name": "Cannot overwrite response"})

    def test_unknown_duplicate_or_changed_headers_fail_closed(self):
        with self.assertRaises(SheetIntegrityError):
            self.adapter([["Full Name", "Full Name"]]).initialize()
        with self.assertRaises(SheetIntegrityError):
            self.adapter([["Unknown Name", "Email Address"]]).initialize()
        sheet = self.adapter()
        sheet.initialize()
        row = sheet.rows()[0]
        sheet.ws.values[1][0] = "Changed manually"
        with self.assertRaises(SheetIntegrityError):
            sheet.update(row, {"state": "PROCESSING"})

    def test_permanent_id_is_looked_up_again_after_row_order_changes(self):
        headers = list(DEFAULT_HEADERS.values())
        def record(name, pid):
            d = {"name": name, "email": name + "@example.test", "participant_id": pid, "event_id": "test-event"}
            return [d.get(k, "") for k in DEFAULT_HEADERS]
        sheet = self.adapter([headers, record("one", "P-one"), record("two", "P-two")])
        first = sheet.rows()[0]
        sheet.ws.values[1], sheet.ws.values[2] = sheet.ws.values[2], sheet.ws.values[1]
        sheet.update(first, {"state": "SENT"})
        updates, _ = sheet.ws.calls[-1]
        self.assertTrue(updates[0]["range"].endswith("3"))


class MailAdapterTests(Base):
    def mailer(self):
        path = self.s.generated / "fake.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fake")
        return SMTPMailer(self.s), ("test@example.test", "Example", "P-fake", path, "<fake@example.test>")

    def test_smtp_connection_failure_is_retryable(self):
        mailer, args = self.mailer()
        with patch("event_checkin.mailer.smtplib.SMTP", side_effect=ConnectionError("fake")):
            with self.assertRaises(EmailNotSent):
                mailer.send(*args)

    def test_smtp_send_disconnect_is_ambiguous(self):
        import smtplib
        mailer, args = self.mailer()
        with patch("event_checkin.mailer.smtplib.SMTP") as smtp:
            smtp.return_value.send_message.side_effect = smtplib.SMTPServerDisconnected("fake")
            with self.assertRaises(EmailAmbiguous):
                mailer.send(*args)

    def test_smtp_explicit_rejection_is_retryable(self):
        import smtplib
        mailer, args = self.mailer()
        with patch("event_checkin.mailer.smtplib.SMTP") as smtp:
            smtp.return_value.send_message.side_effect = smtplib.SMTPDataError(550, b"fake rejection")
            with self.assertRaises(EmailNotSent):
                mailer.send(*args)

    def test_success_has_attachment_and_quit_is_not_needed(self):
        mailer, args = self.mailer()
        with patch("event_checkin.mailer.smtplib.SMTP") as smtp:
            mailer.send(*args)
            message = smtp.return_value.send_message.call_args.args[0]
            self.assertEqual(message["Message-ID"], args[-1])
            self.assertEqual(len(list(message.iter_attachments())), 1)
            smtp.return_value.starttls.assert_called_once()
            smtp.return_value.quit.assert_not_called()

    def test_disabled_email_never_opens_smtp(self):
        self.s.email_enabled = False
        mailer, args = self.mailer()
        with patch("event_checkin.mailer.smtplib.SMTP") as smtp:
            with self.assertRaises(ConfigurationError):
                mailer.send(*args)
            smtp.assert_not_called()


class IgnoreTests(unittest.TestCase):
    def test_private_paths_are_ignored_and_examples_remain_trackable(self):
        # Use a temporary repo with the actual ignore file to verify patterns
        # independently of the user's index, tracked files and local Git state.
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / ".gitignore").write_bytes((ROOT / ".gitignore").read_bytes())
            subprocess.run(["git", "init", "-q", str(tmp)], check=True, capture_output=True)
            paths = [".env", ".env.local", "secrets/google-service-account.json", "generated/P-test/card.png",
                     "runtime/checkin.sqlite3", "runtime/issuance.sqlite3", "runtime/scanner-pending.json", ".venv/bin/python", ".venv312/bin/python",
                     ".env.test", ".env.production", "config/sources.production.json",
                     "runtime-production/issuance.sqlite3", "generated-production/P-test/card.png"]
            result = subprocess.run(["git", "-C", str(tmp), "check-ignore", "--stdin"], input="\n".join(paths) + "\n",
                                    text=True, capture_output=True, check=True)
            self.assertEqual(set(result.stdout.splitlines()), set(paths))
            public = subprocess.run(["git", "-C", str(tmp), "check-ignore", ".env.example", ".env.test.example", ".env.production.example",
                                     "config/sources.production.example.json", "README.md"], capture_output=True)
            self.assertEqual(public.returncode, 1)
