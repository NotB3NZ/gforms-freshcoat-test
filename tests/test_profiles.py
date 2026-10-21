import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch, sentinel

from event_checkin.config import ConfigurationError, Settings, load_settings, production_sources


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "secrets").mkdir()
        (self.root / "secrets/google-service-account.json").write_text("{}")
        (self.root / "config").mkdir()
        self.source = {
            "id": "regular", "spreadsheet_id": "production-sheet", "tab_id": 123,
            "tab_name": "Participants", "price_category": "regular",
            "fields": {key: {"header": label, "column": col} for key, label, col in
                       (("email", "Email", "C"), ("name", "Full Name", "D"), ("tier", "Select Ticket Type", "H"))},
            "approvals": {key: key for key in ("confirmation", "reference_matched", "price_matched", "payment_received")},
        }
        (self.root / "config/sources.production.json").write_text(json.dumps({"version": 1, "sources": [self.source]}))
        (self.root / ".env.test").write_text("EVENT_ID=test-event\nSPREADSHEET_ID=test-sheet\nSHEET_TAB=Responses\nEMAIL_ENABLED=false\nSMTP_PASSWORD=private-test-value\n")
        (self.root / ".env.production").write_text("EVENT_ID=production-event\nEMAIL_ENABLED=false\n")

    def tearDown(self):
        self.temp.cleanup()

    def load(self, profile):
        return load_settings(profile, root=self.root)

    def test_configuration_and_environment_do_not_leak_between_profiles(self):
        (self.root / ".env").write_text("SMTP_PASSWORD=legacy-secret\nEMAIL_ENABLED=true\n")
        with patch.dict(os.environ, {"SMTP_PASSWORD": "shell-secret", "EMAIL_ENABLED": "true", "SPREADSHEET_ID": "wrong-sheet"}):
            original = dict(os.environ)
            test = self.load("test")
            production = self.load("production")
            test_again = self.load("test")
            self.assertEqual(dict(os.environ), original)
        self.assertEqual(test.smtp_password, "private-test-value")
        self.assertEqual(test_again.smtp_password, test.smtp_password)
        self.assertEqual(test.spreadsheet_id, "test-sheet")
        self.assertEqual(production.smtp_password, "")
        self.assertEqual(production.spreadsheet_id, "")
        self.assertFalse(test.email_enabled)
        self.assertFalse(production.email_enabled)
        self.assertNotEqual(test.runtime, production.runtime)
        self.assertNotEqual(test.generated, production.generated)
        self.assertEqual(len(production_sources(production)), 1)

    def test_production_never_falls_back_to_legacy_file(self):
        (self.root / ".env.production").unlink()
        (self.root / ".env").write_text("EVENT_ID=legacy\nSMTP_PASSWORD=legacy-secret\n")
        with self.assertRaises(ConfigurationError):
            self.load("production")

    def test_legacy_test_configuration_still_works_without_test_file(self):
        (self.root / ".env.test").rename(self.root / ".env")
        self.assertEqual(self.load("test").event_id, "test-event")

    def test_dotenv_interpolation_cannot_import_a_shell_secret(self):
        with (self.root / ".env.production").open("a") as config:
            config.write("SMTP_PASSWORD=${SMTP_PASSWORD}\n")
        with patch.dict(os.environ, {"SMTP_PASSWORD": "shell-secret"}):
            self.assertEqual(self.load("production").smtp_password, "${SMTP_PASSWORD}")

    def test_unknown_profile_and_shared_event_id_are_refused(self):
        with self.assertRaises(ConfigurationError):
            self.load("prod-typo")
        (self.root / ".env.production").write_text("EVENT_ID=test-event\n")
        for profile in ("test", "production"):
            with self.assertRaises(ConfigurationError):
                self.load(profile)

    def test_shared_or_nested_storage_is_refused_in_both_directions(self):
        for path in ("runtime", "runtime/nested", ".", "generated"):
            with self.subTest(path=path):
                (self.root / ".env.production").write_text("EVENT_ID=production-event\nRUNTIME_DIR=" + path + "\n")
                for profile in ("test", "production"):
                    with self.assertRaises(ConfigurationError):
                        self.load(profile)

    def test_test_profile_cannot_point_to_a_production_source(self):
        with (self.root / ".env.test").open("a") as config:
            config.write("SPREADSHEET_ID=production-sheet\n")
        with self.assertRaises(ConfigurationError):
            self.load("test")

    def test_test_guard_uses_the_selected_production_manifest_path(self):
        (self.root / "config/sources.production.json").rename(self.root / "config/custom-sources.json")
        with (self.root / ".env.production").open("a") as config:
            config.write("SOURCES_FILE=config/custom-sources.json\n")
        with (self.root / ".env.test").open("a") as config:
            config.write("SPREADSHEET_ID=production-sheet\n")
        with self.assertRaises(ConfigurationError):
            self.load("test")

    def test_production_email_cannot_be_enabled_in_setup_phase(self):
        with (self.root / ".env.production").open("a") as config:
            config.write("EMAIL_ENABLED=true\n")
        with self.assertRaises(ConfigurationError):
            self.load("production")

    def test_production_readiness_block_reaches_sheet_staff_and_mail_adapters(self):
        settings = self.load("production")
        for method in (settings.require_sheet, settings.require_staff, settings.require_email):
            with self.assertRaisesRegex(ConfigurationError, "locked"):
                method()

    def test_missing_confirmation_in_manifest_is_rejected(self):
        del self.source["approvals"]["confirmation"]
        (self.root / "config/sources.production.json").write_text(json.dumps({"version": 1, "sources": [self.source]}))
        with self.assertRaises(ConfigurationError):
            production_sources(self.load("production"))

    def test_production_live_commands_stop_before_any_adapter_or_side_effect(self):
        from event_checkin.__main__ import main
        settings = self.load("production")
        commands = [
            ["init-sheet"], ["check-sheet"], ["worker", "--once"], ["queue-unprocessed"],
            ["retry", "P-example"], ["resend", "P-example"], ["cancel", "P-example"],
            ["server"], ["scanner"], ["send-test", "--to", "example@example.test", "--authorize-send"],
        ]
        for command in commands:
            with self.subTest(command=command), patch("sys.argv", ["event_checkin", "--profile", "production"] + command), \
                    patch("event_checkin.__main__.load_settings", return_value=settings), \
                    patch("event_checkin.__main__.GoogleSheet") as sheet, \
                    patch("event_checkin.__main__.SMTPMailer") as mailer, \
                    patch("event_checkin.__main__.CardRenderer") as renderer, \
                    redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as error:
                self.assertEqual(main(), 1)
                self.assertIn("locked", error.getvalue())
                sheet.assert_not_called()
                mailer.assert_not_called()
                renderer.assert_not_called()
        self.assertFalse((self.root / "runtime-production").exists())
        self.assertFalse((self.root / "generated-production").exists())

    def test_profile_info_is_local_and_does_not_display_secrets(self):
        from event_checkin.__main__ import main
        settings = self.load("test")
        with patch("sys.argv", ["event_checkin", "profile-info"]), \
                patch("event_checkin.__main__.load_settings", return_value=settings), \
                patch("event_checkin.__main__.GoogleSheet") as sheet, \
                patch("event_checkin.__main__.SMTPMailer") as mailer, redirect_stdout(io.StringIO()) as output:
            self.assertIsNone(main())
        self.assertIn("Profile: test", output.getvalue())
        self.assertNotIn("private-test-value", output.getvalue())
        sheet.assert_not_called()
        mailer.assert_not_called()

    def test_server_uses_selected_settings_instead_of_reloading_another_profile(self):
        from event_checkin.__main__ import main
        settings = self.load("test")
        settings.staff_key = "x" * 40
        with patch("sys.argv", ["event_checkin", "--profile", "test", "server"]), \
                patch("event_checkin.__main__.load_settings", return_value=settings), \
                patch("event_checkin.server.create_app", return_value=sentinel.app) as create, \
                patch("uvicorn.run") as run:
            self.assertIsNone(main())
        create.assert_called_once_with(settings=settings)
        self.assertEqual(run.call_args.args[0], sentinel.app)
