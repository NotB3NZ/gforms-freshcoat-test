"""Offline confirmation composition from already-issued, frozen ticket data."""
import hashlib
import re
from email.message import EmailMessage
from email.headerregistry import Address
from email.utils import formatdate
from html import escape
from io import BytesIO
from pathlib import Path

from PIL import Image

from .config import ROOT
from .mailer import EmailNotSent

TEMPLATE = ROOT / "templates/email/registration-confirmation.html"
SUBJECT = "Registration confirmed: AWS Student Community Day Mindanao 2026"
REQUIRED = frozenset(("first_name", "full_name", "email", "pass_type", "badge_image_url"))
PLACEHOLDER = re.compile(r"{{\s*([A-Za-z_][A-Za-z0-9_]*)\s*}}")
BADGE_BLOCK = re.compile(
    r'<!-- Replace with: <img src="{{badge_image_url}}"[^\n]*?-->\s*'
    r'<table\b[^>]*>.*?</table>', re.DOTALL)
BADGE_IMAGE = ('<img src="{{badge_image_url}}" width="320" height="240" '
               'alt="Event badge for {{full_name}}" '
               'style="display:block;width:100%;max-width:320px;height:auto;border:0;border-radius:8px;">')


class MessageBuildError(EmailNotSent):
    """A local composition failure; nothing has been submitted to SMTP."""


def _text(value):
    # Brace entities preserve literal participant text without creating a second
    # template expression. Substitution is strictly one pass, never recursive.
    return escape(value, quote=True).replace("{", "&#123;").replace("}", "&#125;")


def render_confirmation_html(*, name, recipient, ticket_tier, badge_source, template_path=TEMPLATE):
    """Personalize a copy; badge_source is supplied internally by the MIME builder."""
    try:
        template = Path(template_path).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise MessageBuildError("The confirmation HTML template is unavailable.") from None
    if set(PLACEHOLDER.findall(template)) != REQUIRED:
        raise MessageBuildError("Required confirmation placeholders are missing or unknown.")
    matches = list(BADGE_BLOCK.finditer(template))
    if len(matches) != 1 or "border:2px dashed" not in matches[0].group():
        raise MessageBuildError("The confirmation template needs exactly one known badge placeholder.")
    template = BADGE_BLOCK.sub(lambda _: BADGE_IMAGE, template, count=1)
    fields = set(PLACEHOLDER.findall(template))
    if fields != REQUIRED or "{{" in PLACEHOLDER.sub("", template) or "}}" in PLACEHOLDER.sub("", template):
        raise MessageBuildError("Required confirmation placeholders are missing, unknown or unresolved.")
    values = {"first_name": name.split()[0], "full_name": name, "email": recipient,
              "pass_type": ticket_tier, "badge_image_url": badge_source}
    rendered = PLACEHOLDER.sub(lambda match: _text(values[match[1]]), template)
    if "{{" in rendered or "}}" in rendered:
        raise MessageBuildError("Required confirmation placeholders remain unresolved.")
    return rendered


def build_confirmation_message(recipient, name, participant_id, card_path, message_id, *, ticket_tier,
                               sender=None, template_path=TEMPLATE):
    """Build multipart/mixed → alternative → HTML/related with an inline PNG.

    No settings, credentials, Sheets, SMTP, network calls or message-ID generation.
    The caller supplies the durable message ID and the exact already-rendered card.
    """
    if (not isinstance(name, str) or not name.strip() or len(name) > 160
            or any(ord(c) < 32 or ord(c) == 127 for c in name)):
        raise MessageBuildError("A complete issued name is required.")
    if not isinstance(recipient, str) or not re.fullmatch(r"[^\s@<>\r\n]+@[^\s@<>\r\n]+\.[^\s@<>\r\n]+", recipient):
        raise MessageBuildError("One frozen recipient address is required.")
    try:
        Address(addr_spec=recipient)
    except (ValueError, IndexError):
        raise MessageBuildError("One frozen recipient address is required.") from None
    if ticket_tier not in ("Lite", "Regular", "Pro"):
        raise MessageBuildError("The issued tier must be Lite, Regular or Pro.")
    if not isinstance(participant_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", participant_id):
        raise MessageBuildError("A valid issued participant ID is required.")
    if not isinstance(message_id, str) or not re.fullmatch(r"<[^\s<>@]+@[^\s<>@]+>", message_id):
        raise MessageBuildError("The durable message ID is required.")
    try:
        badge = Path(card_path).read_bytes()
        with Image.open(BytesIO(badge)) as image:
            if image.format != "PNG":
                raise ValueError()
            image.verify()
    except (OSError, ValueError):
        raise MessageBuildError("The personalized PNG badge is missing or invalid.") from None
    cid = "badge." + hashlib.sha256(message_id.encode()).hexdigest()[:24] + "@event-checkin.invalid"
    html = render_confirmation_html(name=name, recipient=recipient, ticket_tier=ticket_tier,
                                    badge_source="cid:" + cid, template_path=template_path)
    message = EmailMessage()
    try:
        if sender:
            message["From"] = sender
        message["To"] = recipient
        message["Subject"] = SUBJECT
        message["Message-ID"] = message_id
        message["Date"] = formatdate(localtime=False)
    except ValueError:
        raise MessageBuildError("Invalid confirmation message headers.") from None
    message.set_content(
        f"Hello {name.split()[0]},\n\n"
        "Your registration for AWS Student Community Day Mindanao 2026 is confirmed.\n\n"
        f"Name: {name}\nEmail: {recipient}\nPass type: {ticket_tier}\nParticipant ID: {participant_id}\n\n"
        "Saturday, November 28, 2026\n9:00 AM – 5:00 PM (registration starts at 8:00 AM)\n"
        "Finster Auditorium, Ateneo de Davao University, Davao City\n"
        "Build Beyond. Dream Limitless.\n\n"
        "Present the attached event badge at the registration desk. Bring a valid school or government ID, "
        "your laptop and charger, and arrive early. Keep the QR private: it admits one person.\n\n"
        "Questions: awscc.upmindanao@gmail.com\n"
    )
    message.add_alternative(html, subtype="html")
    message.get_payload()[-1].add_related(badge, maintype="image", subtype="png", cid="<" + cid + ">",
                                         disposition="inline", filename="event-badge-inline.png")
    message.add_attachment(badge, maintype="image", subtype="png", filename="event-badge.png")
    return message


def browser_preview_html(message, badge_filename):
    """Make a separate browser copy; local paths never enter the email itself."""
    if not re.fullmatch(r"[A-Za-z0-9_-]+\.png", badge_filename):
        raise MessageBuildError("Browser badge images must use a simple local PNG filename.")
    inline = [part for part in message.walk() if part.get_content_type() == "image/png"
              and part.get_content_disposition() == "inline"]
    if len(inline) != 1:
        raise MessageBuildError("Expected exactly one inline badge.")
    html = message.get_body(preferencelist=("html",)).get_content()
    source = 'src="cid:' + str(inline[0]["Content-ID"]).strip("<>") + '"'
    if html.count(source) != 1:
        raise MessageBuildError("The HTML badge must reference its inline Content-ID exactly once.")
    return html.replace(source, 'src="' + badge_filename + '"', 1)
