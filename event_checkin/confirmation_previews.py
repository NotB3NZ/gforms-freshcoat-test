"""Synthetic-only HTML/EML previews; never loads profiles or constructs SMTP clients."""
import argparse
import hashlib
import json
from email import policy
from email.parser import BytesParser
from html import escape
from html.parser import HTMLParser
from pathlib import Path

from .cards import CardRenderer
from .confirmation_email import TEMPLATE, browser_preview_html, build_confirmation_message
from .controlled_render import archive_invalid, verify_render
from .final_badge_checks import final_settings
from .test_identity import TEST_TOKEN_PREFIX, is_test_token


def synthetic_examples():
    return (("Lite", "Avery O'Connell & Lane", "avery.lite@example.invalid"),
            ("Regular", "Élodie Marín O'Connell", "elodie.regular@example.invalid"),
            ("Pro", "Rowan Harper Everleigh", "rowan.pro@example.invalid"))


class RemoteAssets(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = []

    def handle_starttag(self, tag, attrs):
        for key, value in attrs:
            if tag in ("img", "link") and key in ("src", "href") and value.startswith(("https://", "http://")):
                self.urls.append(value)


def generate_previews(output):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    settings = final_settings(output)
    originals = {path: path.read_bytes() for path in (TEMPLATE, settings.template)}
    assets = RemoteAssets()
    assets.feed(TEMPLATE.read_text(encoding="utf-8"))
    report = {"synthetic_only": True, "email_enabled": False, "production_processing": "LOCKED",
              "smtp_connected": False, "remote_assets": {url: "UNVERIFIED" for url in assets.urls},
              "inbox_compatibility": "UNVERIFIED; browser previews are for visual review only",
              "source_sha256": {str(path): hashlib.sha256(data).hexdigest() for path, data in originals.items()},
              "examples": []}
    links = []
    for tier, name, recipient in synthetic_examples():
        pid = "INVALID-EMAIL-" + tier.upper()
        token = TEST_TOKEN_PREFIX + "EMAIL_PREVIEW_" + hashlib.sha256(tier.encode()).hexdigest()[:24]
        assert is_test_token(token) and 20 <= len(name) <= 25
        card = CardRenderer(settings).render(pid, name, token, ticket_tier=tier)
        proof = verify_render(settings, card, name, tier, token)
        badge = card.with_name("INVALID-SYNTHETIC-BADGE.png")
        archive_invalid(card, badge)
        message_id = "<synthetic-confirmation-" + tier.lower() + "@example.invalid>"
        message = build_confirmation_message(recipient, name, pid, badge, message_id,
                                             ticket_tier=tier, sender="preview-sender@example.invalid")
        html_path = badge.parent / "preview.html"
        html_path.write_text(browser_preview_html(message, badge.name), encoding="utf-8")
        eml_path = badge.parent / "confirmation.eml"
        eml_path.write_bytes(message.as_bytes(policy=policy.SMTP))
        # Verify the actual serialized MIME, not just the in-memory message.
        parsed = BytesParser(policy=policy.default).parsebytes(eml_path.read_bytes())
        images = [part for part in parsed.walk() if part.get_content_type() == "image/png"]
        assert len(images) == 2 and all(part.get_payload(decode=True) == badge.read_bytes() for part in images)
        assert parsed["Message-ID"] == message_id and parsed["To"].addresses[0].addr_spec == recipient
        assert "cid:" in parsed.get_body(preferencelist=("html",)).get_content()
        assert "cid:" not in html_path.read_text(encoding="utf-8")
        report["examples"].append({"tier": tier, "full_name": name, "recipient": recipient, "message_id": message_id,
                                    "token_namespace": TEST_TOKEN_PREFIX, "badge_verification": proof,
                                    "badge": str(badge), "html": str(html_path), "eml": str(eml_path),
                                    "png_sha256": hashlib.sha256(badge.read_bytes()).hexdigest()})
        relative = badge.parent.name
        links.append(f'<li>{escape(tier)} — {escape(name)} — {escape(recipient)}: '
                     f'<a href="{relative}/preview.html">HTML preview</a> · '
                     f'<a href="{relative}/confirmation.eml">EML</a> · '
                     f'<a href="{relative}/{badge.name}">invalid badge</a></li>')
    for path, data in originals.items():
        assert path.read_bytes() == data
    (output / "index.html").write_text(
        '<!doctype html><meta charset="utf-8"><title>Synthetic confirmation review</title>'
        '<h1>Synthetic confirmation review</h1><p>No email sent. All badges are permanently invalid. '
        'Remote poster/web fonts are unverified; browser previews do not establish inbox compatibility.</p><ul>'
        + "".join(links) + '</ul><p><a href="report.json">Verification report</a></p>', encoding="utf-8")
    (output / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path, help="New artifact folder; existing folders are refused")
    report = generate_previews(parser.parse_args().out)
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
