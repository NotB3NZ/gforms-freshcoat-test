"""Preview uses synthetic records with real-format tokens; never contacts Google."""
import base64
import hashlib
import os
import tempfile
import unittest
import uuid
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch, Mock

from dotenv import dotenv_values
WEB_AVAILABLE = True
try:
    from fastapi.testclient import TestClient
    from sqlalchemy import text
except (ImportError, RuntimeError):
    WEB_AVAILABLE = False

if WEB_AVAILABLE:
    from event_checkin.config import Settings
    from event_checkin.web.adapters import FakeSheets, LiveSheets
    from event_checkin.web.app import create_app, cookie_name, COOKIE
    from event_checkin.web.db import Database, one
    from event_checkin.web.preview import prepare_preview
    from event_checkin.web.security import hash_password
    from event_checkin.web.seed import payload
    from event_checkin.web.service import AdmissionService, PreviewReadOnly
    from event_checkin.web.settings import WebSettings

PASSWORD = "preview-test-password-only"
HASH = hash_password(PASSWORD) if WEB_AVAILABLE else ""


def issued_token(label):
    return base64.urlsafe_b64encode(hashlib.sha256(("preview-fixture:" + label).encode()).digest()).decode().rstrip("=")


@unittest.skipUnless(WEB_AVAILABLE, "Install requirements-web.txt in the isolated web environment")
class PreviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.s = WebSettings(database_url="sqlite:///" + str(self.root / "preview.db"),
                             password_hash=HASH, origin="http://localhost:8081",
                             event_id="preview-fixture-event", synthetic=False, read_only_preview=True,
                             live_config=str(self.root), fixture=self.root / "fixture.json")
        self.fake = FakeSheets(self.s)
        data = payload(self.s.event_id)
        for rows in data["sources"].values():
            for row in rows:
                if not row["ticket_token"].startswith("INVALID_TEST_"):
                    row["ticket_token"] = issued_token(row["participant_id"])
        self.fake.write(data)
        self.fake.export_batch = Mock(side_effect=AssertionError("Preview must never export"))
        self.app = create_app(self.s, self.fake, background=False)
        self.client = TestClient(self.app, base_url=self.s.origin)
        self.client.__enter__()
        self.service, self.db = self.app.state.service, self.app.state.db
        self.service.import_rows(self.fake.rows())
        login = self.client.post("/api/login", json={"password": PASSWORD}, headers={"origin": self.s.origin})
        self.assertEqual(login.status_code, 200)
        self.headers = {"origin": self.s.origin, "x-csrf-token": login.json()["csrf"]}
        self.assertEqual(self.client.post("/api/nickname", json={"nickname": "Preview staff"}, headers=self.headers).status_code, 200)

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temp.cleanup()

    def counts(self):
        with self.db.transaction() as conn:
            return {t: one(conn, "SELECT COUNT(*) AS n FROM " + t)["n"] for t in ("attendance", "requests", "outbox")}

    def test_real_format_lookup_decode_search_and_dashboard(self):
        before = self.counts()
        token = issued_token("DEMO-P-lite")
        response = self.client.post("/api/lookup", json={"token": token}, headers=self.headers)
        self.assertEqual(response.json()["participant"]["name"], "Alex Rivera")
        self.assertTrue(response.json()["participant"]["eligible"])
        self.assertEqual(self.client.get("/api/participants?q=alex.rivera").json()["total"], 1)
        self.assertNotIn(token, self.client.get("/api/participants").text)
        import qrcode
        import io
        output = io.BytesIO()
        qrcode.make(token).save(output, format="PNG")
        decoded = self.client.post("/api/decode", headers=self.headers,
                                   json={"image": "data:image/png;base64," + base64.b64encode(output.getvalue()).decode()})
        self.assertEqual(decoded.json()["participant"]["participant_id"], "DEMO-P-lite")
        dashboard = self.client.get("/api/dashboard").json()
        self.assertEqual(dashboard["mode"], "preview")
        self.assertTrue(dashboard["read_only_preview"])
        self.assertFalse(dashboard["synthetic"])
        self.assertFalse(dashboard["admissions_open"])
        self.assertEqual(dashboard["counts"]["eligible"], 7)
        self.assertEqual(self.counts(), before)

    def test_backend_blocks_both_checkin_methods_even_if_event_opened(self):
        before = self.counts()
        with self.db.transaction() as conn:
            conn.execute(text("UPDATE events SET admissions_open=1"))
        for method in ("qr", "manual"):
            response = self.client.post("/api/checkins", headers=self.headers,
                                        json={"participant_id": "DEMO-P-lite", "method": method, "request_id": str(uuid.uuid4())})
            self.assertEqual(response.status_code, 403)
            self.assertIn("Read-only preview", response.json()["detail"])
        self.assertEqual(self.counts(), before)
        self.assertFalse(self.service.dashboard()["admissions_open"])

    def test_service_and_cli_cannot_open_admissions(self):
        before = self.counts()
        with self.assertRaises(PreviewReadOnly):
            self.service.open_admissions()
        with self.assertRaises(PreviewReadOnly):
            self.service.confirm("DEMO-P-lite", str(uuid.uuid4()), "qr", {})
        from event_checkin.web.cli import main
        with patch("event_checkin.web.cli.WebSettings.load", return_value=self.s), patch("event_checkin.web.cli.Database") as db:
            with patch("sys.argv", ["web", "open-admissions", "--reconciled"]), self.assertRaises(SystemExit) as error:
                main()
            self.assertEqual(error.exception.code, 2)
            db.assert_not_called()
        self.assertEqual(self.counts(), before)

    def test_refresh_imports_but_never_exports_even_with_existing_outbox(self):
        # Simulate an accidental queued export to prove the guard is unconditional.
        with self.db.transaction() as conn:
            conn.execute(text("INSERT INTO outbox(event_id,participant_id,request_id) VALUES(:e,'DEMO-P-arrived','legacy-demo-arrived')"),
                         {"e": self.s.event_id})
        updated = self.fake.read_payload()
        updated["sources"]["regular"][0]["ticket_status"] = "CANCELLED"
        self.fake.write(updated)
        result = self.app.state.sync.run_once(force=True)
        self.assertEqual(result["status"], "complete")
        self.app.state.sync.export("unused-owner")
        self.fake.export_batch.assert_not_called()
        with self.db.transaction() as conn:
            self.assertEqual(one(conn, "SELECT attempts FROM outbox")["attempts"], 0)
        p = self.client.get("/api/participants/DEMO-P-lite").json()
        self.assertFalse(p["eligible"])
        self.assertEqual(self.counts()["attendance"], 1)
        self.assertEqual(self.counts()["requests"], 0)

    def test_preview_database_cannot_be_reused_for_operations(self):
        with self.assertRaisesRegex(ValueError, "different data mode"):
            self.db.migrate(replace(self.s, read_only_preview=False, synthetic=True))
        other = Database("sqlite:///" + str(self.root / "demo.db"))
        try:
            other.migrate(replace(self.s, read_only_preview=False, synthetic=True))
            with self.assertRaisesRegex(ValueError, "different data mode"):
                other.migrate(self.s)
            # Existing installations without a mode marker also cannot be adopted.
            with other.transaction() as conn:
                conn.execute(text("DELETE FROM web_database_mode"))
            with self.assertRaisesRegex(ValueError, "fresh, separate"):
                other.migrate(self.s)
        finally:
            other.engine.dispose()

    def test_preview_session_identity_cookie_and_logout(self):
        session = self.client.get("/api/session").json()
        self.assertTrue(session["read_only_preview"])
        self.assertEqual(session["mode"], "preview")
        self.assertIsNotNone(self.client.cookies.get(cookie_name(self.s)))
        self.assertIsNone(self.client.cookies.get(COOKIE))
        self.assertEqual(self.client.post("/api/logout", headers=self.headers, json={}).status_code, 200)
        self.assertEqual(self.client.get("/api/participants").status_code, 401)

    def test_preview_is_local_only_and_live_requirements_remain(self):
        self.s.validate()
        for settings in (replace(self.s, synthetic=True), replace(self.s, live_export=True),
                         replace(self.s, origin="https://staff.example.invalid"),
                         replace(self.s, event_id="freshcoat-synthetic"), replace(self.s, live_config="")):
            with self.subTest(settings=settings.mode), self.assertRaises(ValueError):
                settings.validate()
        with self.assertRaisesRegex(ValueError, "HTTPS and PostgreSQL"):
            replace(self.s, read_only_preview=False).validate()
        with TestClient(create_app(self.s, self.fake, background=False), base_url=self.s.origin,
                        client=("192.0.2.10", 1234)) as remote:
            self.assertEqual(remote.get("/api/session").status_code, 403)

    def test_preview_reader_reuses_mappings_without_changing_email_config(self):
        legacy = Settings(root=self.root, runtime=self.root / "legacy-runtime",
                          event_id=self.s.event_id, email_enabled=True)
        with patch("event_checkin.web.adapters.load_settings", return_value=legacy), \
             patch("event_checkin.web.adapters.ProductionSheets") as source, \
             patch("event_checkin.web.adapters.apply_approved_groups", return_value=["reviewed"]) as groups:
            reader = LiveSheets(self.s)
            self.assertTrue(legacy.email_enabled)
            self.assertFalse(reader.legacy.email_enabled)
            self.assertEqual(source.call_args.kwargs, {"writable": False})
            self.assertEqual(reader.rows(), ["reviewed"])
            groups.assert_called_once_with(reader.legacy, reader.sheet.rows.return_value)
            self.s.live_export = True  # Even a later settings mistake cannot enable this writer.
            with self.assertRaises(PermissionError):
                reader.export_batch([])
            self.assertIsNone(reader.writer_tables)

    def test_setup_preserves_demo_config_and_selects_actual_event(self):
        source_root = self.root / "original"
        source_root.mkdir()
        original = "WEB_PASSWORD_HASH='" + HASH + "'\nWEB_SYNTHETIC=true\n"
        (source_root / ".env.web").write_text(original)
        legacy = Settings(event_id="actual-event-from-config", timezone="Asia/Manila", email_enabled=True)
        with patch("event_checkin.web.preview.load_settings", return_value=legacy):
            target = prepare_preview(self.root, source_root)
            with self.assertRaises(FileExistsError):
                prepare_preview(self.root, source_root)
        values = dotenv_values(target, interpolate=False)
        self.assertEqual(values["WEB_PASSWORD_HASH"], HASH)
        self.assertEqual(values["WEB_EVENT_ID"], legacy.event_id)
        self.assertEqual(values["WEB_LIVE_CONFIG"], str(source_root.resolve()))
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertEqual((source_root / ".env.web").read_text(), original)
        with patch.dict(os.environ, {"WEB_ENV_FILE": str(target)}, clear=True):
            loaded = WebSettings.load()
        self.assertTrue(loaded.read_only_preview)
        self.assertFalse(loaded.synthetic)
        self.assertFalse(loaded.live_export)
        self.assertEqual(loaded.event_id, legacy.event_id)
        self.assertEqual(loaded.password_hash, HASH)
        self.assertTrue(legacy.email_enabled)

    def test_invalid_boolean_config_rejected(self):
        config = self.root / "invalid.env"
        config.write_text("WEB_READ_ONLY_PREVIEW=treu\nWEB_PASSWORD_HASH='" + HASH + "'\n")
        with patch.dict(os.environ, {"WEB_ENV_FILE": str(config)}, clear=True), self.assertRaisesRegex(ValueError, "exactly true or false"):
            WebSettings.load()

    def test_connection_diagnostics_never_print_private_exception_content(self):
        import socket
        from event_checkin.web.preview import failure_message
        outer = ValueError("private source URL must not be displayed")
        outer.__cause__ = socket.gaierror(8, "private hostname")
        message = failure_message(outer)
        self.assertIn("DNS name resolution failed", message)
        self.assertNotIn("private", message)
        for status in (401, 403, 404):
            error = RuntimeError("secret response body")
            error.response = Mock(status_code=status)
            self.assertNotIn("secret", failure_message(error))
            self.assertIn("Google", failure_message(error))


if __name__ == "__main__":
    unittest.main()
