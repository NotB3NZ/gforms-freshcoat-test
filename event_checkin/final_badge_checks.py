"""Reproduce only the final badge's two offline checks with synthetic inputs.

No profile loading, participant rows, identity journals, Sheets or mail clients.
"""
import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

from .cards import (CardRenderer, assert_no_font_fallback, fitted_template,
                    read_template, render_values)
from .config import ROOT, Settings, private_directory
from .controlled_render import archive_invalid, verify_render


def final_settings(output):
    return Settings(template=ROOT / "templates/event-badge-final.coat", generated=Path(output),
                    bun=shutil.which("bun") or str(Path.home() / ".bun/bin/bun"),
                    qr_x=378, qr_y=228, qr_size=444, email_enabled=False)


def synthetic_names():
    seed = "Élodie O'Connor & José D’Ambrósio "
    return [("ACCENTS-45", (seed * 2)[:45], "Lite"),
            ("LONG-80", (seed * 4)[:80], "Regular"),
            ("LONG-160", (seed * 6)[:160], "Pro"),
            ("WIDE-160", "W" * 160, "Pro")]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path, help="New artifact directory; existing folders are refused")
    args = parser.parse_args()
    output = args.out.resolve()
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    settings = final_settings(output)
    original = settings.template.read_bytes()
    report = {"synthetic_only": True, "email_enabled": False, "production_processing": "LOCKED",
              "template_sha256": hashlib.sha256(original).hexdigest(), "cases": []}
    template = read_template(settings.template)
    for label, name, tier in synthetic_names():
        token = "TEST-NOT-VALID-FOR-ADMISSION-" + label
        path = CardRenderer(settings).render(label, name, token, ticket_tier=tier)
        proof = verify_render(settings, path, name, tier, token)
        directory = path.parent
        values = render_values(template, settings.name_field, name, label, settings.tier_field, tier)
        values_path = directory / "synthetic-values.json"
        values_path.write_text(json.dumps(values, indent=2, ensure_ascii=False) + "\n")
        derived = directory / "rendering-copy.coat"
        fitted_template(settings.template, derived, template, settings.name_field, name)
        cli_directory = private_directory(directory / "cli")
        result = subprocess.run([settings.bun, str(settings.freshcoat / "packages/cli/src/bin.ts"), "render", str(derived),
                                 "--values", str(values_path), "--frame", "front", "--out", str(cli_directory)],
                                cwd=settings.freshcoat, capture_output=True, check=True, timeout=120)
        (directory / "cli-stdout.txt").write_bytes(result.stdout)
        (directory / "cli-stderr.txt").write_bytes(result.stderr)
        assert_no_font_fallback(result)
        warnings = result.stderr.decode(errors="replace").splitlines()
        result = subprocess.run([settings.bun, str(ROOT / "tools/inspect_badge_fonts.ts"), str(derived), str(values_path)],
                                cwd=ROOT, capture_output=True, check=True, timeout=120)
        fonts = json.loads(result.stdout)
        (directory / "font-resolution.json").write_text(json.dumps(fonts, indent=2) + "\n")
        preview = directory / "INVALID-SYNTHETIC-PREVIEW.png"
        archive_invalid(path, preview)
        report["cases"].append({"case": label, "name_length": len(name), "tier": tier, "proof": proof,
                                "fonts": fonts, "preview": str(preview), "font_warnings": False,
                                "other_cli_warnings": warnings})
    assert settings.template.read_bytes() == original
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"report": str(output / "report.json"), "previews": [case["preview"] for case in report["cases"]]}, indent=2))


if __name__ == "__main__":
    main()
