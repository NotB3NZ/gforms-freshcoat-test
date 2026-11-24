"""Regression coverage for preview startup with a real email-enabled profile."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dotenv import dotenv_values
from event_checkin.config import ConfigurationError, load_settings

try:
    from fastapi.testclient import TestClient
    from event_checkin.web.app import create_app
    from event_checkin.web.preview import prepare_preview
    from event_checkin.web.security import hash_password
    from event_checkin.web.settings import WebSettings
    WEB_AVAILABLE = True
except (ImportError, RuntimeError):
    WEB_AVAILABLE = False


@unittest.skipUnless(WEB_AVAILABLE, "Install requirements-web.txt")
class PreviewStartupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.production = self.root / ".env.production"
        self.original = ("EVENT_ID=fixture-production-event\nEMAIL_ENABLED=true\n"
                         "SMTP_HOST=smtp.example.invalid\nSMTP_USERNAME=fixture-user\n"
                         "SMTP_PASSWORD=fixture-password\nSMTP_FROM=fixture@example.invalid\n")
        self.production.write_text(self.original)
        (self.root / ".env.test").write_text("EVENT_ID=fixture-test-event\n")
        self.password_hash = hash_password("fixture-preview-password")
        (self.root / ".env.web").write_text("WEB_PASSWORD_HASH='" + self.password_hash + "'\n")

    def settings(self):
        return WebSettings(database_url="sqlite:///" + str(self.root / "preview.sqlite3"),
                           password_hash=self.password_hash, origin="http://localhost:8081",
                           event_id="fixture-production-event", synthetic=False,
                           read_only_preview=True, live_config=str(self.root))

    def test_startup_reads_email_enabled_profile_without_granting_write_access(self):
        # Only the external Google boundary is mocked. Configuration and lifespan
        # run normally, reproducing the user's formerly failing startup path.
        with patch("event_checkin.web.adapters.ProductionSheets") as sheets:
            with TestClient(create_app(self.settings(), background=False),
                            base_url="http://localhost:8081") as client:
                self.assertEqual(client.get("/api/session").json(), {"authenticated": False})
            sheets.assert_called_once()
            self.assertEqual(sheets.call_args.kwargs, {"writable": False})
            loaded = sheets.call_args.args[0]
            self.assertFalse(loaded.email_enabled)
            self.assertEqual(loaded.profile, "production")
            self.assertEqual(loaded.event_id, "fixture-production-event")
            self.assertEqual([loaded.smtp_host, loaded.smtp_user, loaded.smtp_password, loaded.smtp_from],
                             ["", "", "", ""])
        self.assertEqual(self.production.read_text(), self.original)
        with self.assertRaisesRegex(ConfigurationError, "General production remains locked"):
            load_settings("production", self.root)

    def test_preview_setup_reads_email_enabled_profile_without_editing_it(self):
        target = prepare_preview(self.root, self.root)
        values = dotenv_values(target, interpolate=False)
        self.assertEqual(values["WEB_EVENT_ID"], "fixture-production-event")
        self.assertEqual(values["WEB_PASSWORD_HASH"], self.password_hash)
        self.assertEqual(values["WEB_READ_ONLY_PREVIEW"], "true")
        self.assertEqual(values["WEB_LIVE_EXPORT"], "false")
        self.assertEqual(self.production.read_text(), self.original)

    def test_read_settings_cannot_authorize_sending_or_change_environment(self):
        before = dict(os.environ)
        loaded = load_settings("production", self.root, registration_read_only=True)
        self.assertEqual(dict(os.environ), before)
        self.assertFalse(loaded.email_enabled)
        for gate in (loaded.require_operational, loaded.require_email, loaded.require_sheet):
            with self.assertRaises(ConfigurationError):
                gate()
        for scope in ("single_entry", "inbox_test", "pilot", "authorized_batch"):
            with self.subTest(scope=scope), self.assertRaises(ConfigurationError):
                load_settings("production", self.root, registration_read_only=True, **{scope: True})
            # Existing authorized send configuration is unaffected by a preview read.
            scoped = load_settings("production", self.root, **{scope: True})
            self.assertTrue(scoped.email_enabled)
            self.assertEqual(scoped.smtp_password, "fixture-password")
        self.assertEqual(self.production.read_text(), self.original)

    def test_read_settings_keep_profile_and_storage_isolation(self):
        with self.assertRaises(ConfigurationError):
            load_settings("test", self.root, registration_read_only=True)
        for extra in ("EVENT_ID=fixture-test-event\n", "RUNTIME_DIR=runtime\n"):
            self.production.write_text(self.original + extra)
            with self.subTest(extra=extra), self.assertRaises(ConfigurationError):
                load_settings("production", self.root, registration_read_only=True)


if __name__ == "__main__":
    unittest.main()
