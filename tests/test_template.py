import shutil
import tempfile
import unittest
from pathlib import Path

from event_checkin.cards import CardRenderer, inspect_template, RenderError
from event_checkin.config import Settings, ROOT


class TemplateTests(unittest.TestCase):
    def test_v2_template_personalizes_name_and_tier_and_preserves_archive(self):
        template = ROOT / "templates/event-badgev2.coat"
        if not template.is_file():
            self.skipTest("Optional v2 template is not installed")
        bun = shutil.which("bun") or str(Path.home() / ".bun/bin/bun")
        if not Path(bun).is_file() or not (ROOT / "freshcoat/node_modules").exists():
            self.skipTest("Bun or FreshCoat dependencies missing")
        import cv2
        import numpy as np
        from PIL import Image
        original = template.read_bytes()
        token = "0123456789_abcdefghijklmnopqrstuvwxyz-ABCDE"
        with tempfile.TemporaryDirectory() as temp:
            s = Settings(template=template, bun=bun, generated=Path(temp))
            renderer = CardRenderer(s)
            lite = renderer.render("LOCAL-LITE", "Local Example", token, ticket_tier="Lite")
            pro = renderer.render("LOCAL-PRO", "Local Example", token, ticket_tier="Pro")
            image_lite, image_pro = Image.open(lite), Image.open(pro)
            self.assertFalse(np.array_equal(np.array(image_lite.crop((35, 315, 610, 380))),
                                           np.array(image_pro.crop((35, 315, 610, 380)))))
            self.assertEqual(cv2.QRCodeDetector().detectAndDecode(cv2.imread(str(lite)))[0], token)
            self.assertEqual(image_lite.size, (1200, 900))
        self.assertEqual(template.read_bytes(), original)

    def test_actual_binding_and_no_text_overlap(self):
        template, dims = inspect_template(Settings())
        self.assertEqual(dims, (1200, 900))
        self.assertIn("displayName", template["fields"]["properties"])
        with self.assertRaises(RenderError):
            inspect_template(Settings(qr_y=300))
        with self.assertRaises(RenderError):
            inspect_template(Settings(name_field="company"))

    def test_actual_freshcoat_render_and_qr_roundtrip(self):
        bun = shutil.which("bun") or str(Path.home() / ".bun/bin/bun")
        if not Path(bun).is_file() or not (ROOT / "freshcoat/node_modules").exists():
            self.skipTest("Bun or FreshCoat dependencies missing; install locally then rerun")
        import cv2
        import numpy as np
        from PIL import Image
        with tempfile.TemporaryDirectory() as temp:
            original_template = (ROOT / "templates/card.coat").read_bytes()
            s = Settings(bun=bun, generated=Path(temp))
            token = "0123456789_abcdefghijklmnopqrstuvwxyz-ABCDE"
            self.assertEqual(len(token), 43)
            path = CardRenderer(s).render("LOCAL-TEST", "Avery Example", token)
            image_a = Image.open(path).copy()
            decoded, _, _ = cv2.QRCodeDetector().detectAndDecode(cv2.imread(str(path)))
            self.assertEqual(decoded, token)
            self.assertEqual(image_a.size, (1200, 900))
            self.assertEqual(image_a.getpixel((400, 450)), (255, 255, 255))
            path_b = CardRenderer(s).render("LOCAL-TEST-B", "Jordan Different", token)
            image_b = Image.open(path_b)
            # Actual pixels change in the name area, while the event band is preserved.
            self.assertFalse(np.array_equal(np.array(image_a.crop((40, 286, 1060, 444))), np.array(image_b.crop((40, 286, 1060, 444)))))
            self.assertTrue(np.array_equal(np.array(image_a.crop((0, 0, 1200, 208))), np.array(image_b.crop((0, 0, 1200, 208)))))
            long_path = CardRenderer(s).render("LOCAL-LONG-NAME", "Alexandria " + "Long Participant Name " * 5, token)
            long_image = Image.open(long_path)
            self.assertTrue(np.all(np.array(long_image.crop((0, 444, 1200, 450))) == 255))
            self.assertEqual((ROOT / "templates/card.coat").read_bytes(), original_template)
