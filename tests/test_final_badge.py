"""Synthetic-only final badge checks; no profile, Sheets, email or journals."""
import hashlib
import json
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from event_checkin.cards import (CardRenderer, RenderError, assert_no_font_fallback,
                                fitted_template, prepared_template, read_template, render_values)
from event_checkin.config import ROOT
from event_checkin.controlled_render import ControlledTestBlocked, verify_render
from event_checkin.final_badge_checks import final_settings, synthetic_names


class NameAndFontTests(unittest.TestCase):
    def test_only_rendering_copy_widens_schema_and_keeps_box(self):
        source = ROOT / "templates/event-badge-final.coat"
        original = source.read_bytes()
        template = read_template(source)
        before = json.dumps(template, sort_keys=True)
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / "derived.coat"
            fitted_template(source, destination, template, "displayName", "W" * 160)
            derived = read_template(destination)
            self.assertEqual(derived["fields"]["properties"]["displayName"]["maxLength"], 160)
            a, b = template["template_data"][0]["elements"][0], derived["template_data"][0]["elements"][0]
            self.assertEqual(a["pos"], b["pos"])
            self.assertEqual(a["size"], b["size"])
            self.assertEqual(b["properties"]["fit"], "shrink")
            self.assertLess(b["properties"]["font"]["size"], a["properties"]["font"]["size"])
            with zipfile.ZipFile(source) as old, zipfile.ZipFile(destination) as new:
                for item in old.namelist():
                    if item != "template.json":
                        self.assertEqual(old.read(item), new.read(item))
        self.assertEqual(json.dumps(template, sort_keys=True), before)
        self.assertEqual(source.read_bytes(), original)

    def test_values_never_truncate_primary_or_legacy_name(self):
        for source in ("card.coat", "event-badge-final.coat"):
            template = read_template(ROOT / "templates" / source)
            for _, name, tier in synthetic_names():
                values = render_values(template, "displayName", name, "TEST-SYNTHETIC", "ticketTier", tier)
                self.assertEqual(values["displayName"], name)
                if "name" in values:
                    self.assertEqual(values["name"], name)
            with self.assertRaises(RenderError):
                render_values(template, "displayName", "x" * 161, "TEST", "ticketTier", "Pro")

    def test_success_exit_does_not_hide_font_or_glyph_warnings(self):
        for message in ("warning: no font data for Google Sans; text in them uses a fallback face",
                        "warning: the fonts have no glyphs for U+FFFF in front; they print as boxes",
                        "warning: fonts not declared by the template, found on Google Fonts by name: Google Sans",
                        "missing-font: synthetic", "warning: glyph fallback"):
            for output in (True, False):
                with self.subTest(message=message, stdout=output):
                    result = subprocess.CompletedProcess([], 0, message.encode() if output else b"",
                                                         b"" if output else message.encode())
                    with self.assertRaises(RenderError):
                        assert_no_font_fallback(result)
        assert_no_font_fallback(subprocess.CompletedProcess([], 0, b"front.png", b"warning: low image DPI"))
        assert_no_font_fallback(subprocess.CompletedProcess([], 0, b"/tmp/MISSING-FONT/front.png\n", b""))

    def test_missing_or_changed_pinned_asset_blocks_substitution(self):
        template = read_template(ROOT / "templates/event-badge-final.coat")
        with tempfile.TemporaryDirectory() as temp, patch("event_checkin.cards.GOOGLE_SANS_DIR", Path(temp)):
            with self.assertRaisesRegex(RenderError, "no font substitution"):
                prepared_template(template, "displayName", "Synthetic Name")


class ActualFinalBadgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not Path(final_settings(Path("/tmp")).bun).is_file() or not (ROOT / "freshcoat/node_modules").exists():
            raise unittest.SkipTest("Bun or FreshCoat dependencies unavailable")

    def test_long_names_literal_verifier_and_offline_font_resolution(self):
        source = ROOT / "templates/event-badge-final.coat"
        original = hashlib.sha256(source.read_bytes()).hexdigest()
        with tempfile.TemporaryDirectory() as temp:
            settings = final_settings(temp)
            for label, name, tier in synthetic_names():
                with self.subTest(label=label):
                    token = "TEST-NOT-VALID-FOR-ADMISSION-" + label
                    path = CardRenderer(settings).render(label, name, token, ticket_tier=tier)
                    self.assertTrue(verify_render(settings, path, name, tier, token)["qr_decoded"])
                    derived, values = Path(temp) / "derived.coat", Path(temp) / "values.json"
                    fitted_template(source, derived, read_template(source), "displayName", name)
                    values.write_text(json.dumps(render_values(read_template(source), "displayName", name, label, "ticketTier", tier)))
                    result = subprocess.run([settings.bun, str(ROOT / "tools/inspect_badge_fonts.ts"), str(derived), str(values)],
                                            cwd=ROOT, capture_output=True, check=True, timeout=120)
                    report = json.loads(result.stdout)
                    self.assertEqual(report["missing"], [])
                    self.assertEqual(report["remote_requests"], 0)
                    self.assertTrue(all(w["exact_face_advances"] for w in report["matched_weights"]))
                    manifest = json.loads((ROOT / "templates/fonts/google-sans/manifest.json").read_text())
                    self.assertEqual(report["loaded_faces"][0]["sha256"], [entry["sha256"] for entry in manifest["files"]])
                    self.assertTrue(report["text"][0]["full_name_verified"])
                    self.assertEqual({t["weight"] for t in report["text"]}, {400, 700})
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), original)

    def test_actual_missing_font_warning_rejected_even_with_exit_zero(self):
        with tempfile.TemporaryDirectory() as temp:
            settings = final_settings(temp)
            template = prepared_template(read_template(settings.template), "displayName", "Synthetic Name")
            for font in template["fonts"]:
                if font["family"] == "Google Sans":
                    font["files"] = [{"weight": 400, "src": "missing-synthetic-font.ttf"}]
            # Write directly: fitted_template would repair Google Sans by design.
            derived = Path(temp) / "missing-font.coat"
            with zipfile.ZipFile(settings.template) as old, zipfile.ZipFile(derived, "w") as new:
                for entry in old.infolist():
                    new.writestr(entry, json.dumps(template).encode() if entry.filename == "template.json" else old.read(entry.filename))
            values = Path(temp) / "values.json"
            values.write_text(json.dumps({"displayName": "Synthetic Name", "ticketTier": "Lite", "ticket_id": "TEST"}))
            result = subprocess.run([settings.bun, str(settings.freshcoat / "packages/cli/src/bin.ts"), "render", str(derived),
                                     "--values", str(values), "--frame", "front", "--out", temp],
                                    cwd=settings.freshcoat, capture_output=True, check=True, timeout=120)
            self.assertEqual(result.returncode, 0)
            self.assertIn(b"no font data for Google Sans", result.stderr)
            with self.assertRaises(RenderError):
                assert_no_font_fallback(result)

    def test_actual_missing_glyph_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(RenderError, "glyph fallback"):
                CardRenderer(final_settings(temp)).render("TEST-MISSING-GLYPH", "Synthetic \U0010ffff", "TEST", ticket_tier="Pro")

    def test_controlled_reference_rejects_font_warning(self):
        with tempfile.TemporaryDirectory() as temp:
            settings = final_settings(temp)
            token = "TEST-NOT-VALID-FOR-ADMISSION-CONTROLLED"
            path = CardRenderer(settings).render("TEST-CONTROLLED", "Synthetic Name", token, ticket_tier="Pro")
            result = subprocess.CompletedProcess([], 0, b"", b"warning: no font data for Google Sans; text uses a fallback face")
            with patch("event_checkin.controlled_render.subprocess.run", return_value=result):
                with self.assertRaisesRegex(ControlledTestBlocked, "font or glyph fallback"):
                    verify_render(settings, path, "Synthetic Name", "Pro", token)
