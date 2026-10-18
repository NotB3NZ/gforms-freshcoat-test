import json
import shutil
import subprocess
import unittest
from pathlib import Path

from event_checkin.config import METADATA, ROOT


class AppsScriptTests(unittest.TestCase):
    def test_trigger_queueing_identity_protection_and_idempotent_install(self):
        bun = shutil.which("bun") or str(Path.home() / ".bun/bin/bun")
        if not Path(bun).is_file():
            self.skipTest("Bun missing; install locally then rerun")
        result = subprocess.run([bun, str(ROOT / "tests/apps-script.test.js")],
                                cwd=ROOT, capture_output=True, text=True, timeout=15, check=True)
        # Python and Apps Script must agree on all required columns.
        self.assertEqual(json.loads(result.stdout), list(METADATA))
