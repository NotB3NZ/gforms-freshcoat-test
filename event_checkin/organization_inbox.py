"""Synthetic organization-sender inbox test; no registration or Sheets adapters."""
import argparse
import hashlib
import json
import os
import re
import uuid
from dataclasses import replace
from email import policy
from email.headerregistry import Address
from email.parser import BytesParser
from pathlib import Path

from .cards import CardRenderer, google_sans_files, inspect_template
from .config import ConfigurationError, load_settings, private_directory
from .confirmation_email import (TEMPLATE, browser_preview_html, build_confirmation_message)
from .confirmation_previews import RemoteAssets
from .controlled_render import archive_invalid, verify_render
from .diagnostics import diagnostic_context, safe_diagnostic
from .locking import file_lock
from .mailer import SMTPMailer, EmailNotSent
from .production import private_json
from .test_identity import TEST_TOKEN_PREFIX, is_test_token

SENDER = "awscc.upmindanao@gmail.com"
NAME = "Avery O'Connell & Lane"  # Fictional; never derived from a registration.
TEMPLATE_SHA256 = "7e3c7247d68dd490d2987ae616d40cf344805a38d6a36023a9176d97327830d4"
NOTICE = ("TEST EMAIL — SYNTHETIC DATA — NOT A REGISTRATION CONFIRMATION. "
          "This badge and QR are permanently invalid and cannot admit anyone.")
BADGE = "INVALID-SYNTHETIC-BADGE.png"
TEST_QR = TEST_TOKEN_PREFIX + "ORG_INBOX_ONLY"


def require(condition, message):
    if not condition:
        raise ConfigurationError(message)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def validate_recipient(recipient):
    require(isinstance(recipient, str) and bool(re.fullmatch(r"[^\s@<>,;]+@[^\s@<>,;]+\.[^\s@<>,;]+", recipient)),
            "Supply exactly one recipient address, without a display name or address list.")
    try:
        Address(addr_spec=recipient)
    except (ValueError, IndexError):
        raise ConfigurationError("Supply exactly one valid recipient address.") from None


def check_sender(settings):
    require(settings.profile == "production", "Use the production profile for the organization inbox test.")
    require(settings.smtp_user == SENDER and settings.smtp_from == SENDER,
            "SMTP_USERNAME and SMTP_FROM must both exactly match the organization sender.")


def check_design(settings):
    require(settings.template.resolve() == (settings.root/"templates/event-badge-final.coat").resolve()
            and sha(settings.template) == TEMPLATE_SHA256
            and (settings.frame,settings.name_field,settings.tier_field,settings.qr_x,settings.qr_y,settings.qr_size)
            == ("front","displayName","ticketTier",378,228,444), "The pinned final badge configuration changed.")
    inspect_template(settings)
    google_sans_files()


def artifact_path(settings, output):
    parent = (settings.runtime/"organization-inbox-tests").resolve()
    path = Path(output).resolve()
    require(path.parent == parent, "Use a new direct child of runtime-production/organization-inbox-tests.")
    return path


def write_private(path, data):
    # New artifacts are exclusive; neither old previews nor delivery records are replaced.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd,"wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def build_test_message(recipient, badge, run_id, tier):
    """Offline composition; no profile, credentials, SMTP or network required."""
    validate_recipient(recipient)
    require(str(uuid.UUID(run_id)) == run_id, "Invalid synthetic test identifier.")
    pid = "INVALID-INBOX-" + run_id
    mid = "<organization-inbox-test-" + run_id + "@example.invalid>"
    message = build_confirmation_message(recipient, NAME, pid, badge, mid, ticket_tier=tier, sender=SENDER)
    message.replace_header("Subject", "[TEST — INVALID BADGE] AWS Student Community Day Mindanao 2026 — " + tier)
    message["X-Event-Checkin-Test"] = "synthetic-invalid-badge"
    plain = message.get_body(preferencelist=("plain",))
    plain.set_content(NOTICE + "\n\n" + plain.get_content())
    html = message.get_body(preferencelist=("html",))
    banner = ('<div role="note" style="background:#8b0000;color:#fff;padding:20px;'
              'font:700 16px Arial,sans-serif;text-align:center">' + NOTICE + '</div>')
    content, count = re.subn(r"(<body\b[^>]*>)",lambda match:match[0]+banner,html.get_content(),count=1,flags=re.I)
    require(count == 1, "The confirmation HTML body is unavailable for the test notice.")
    html.set_content(content,subtype="html")
    return message


def prepare(settings, recipient, output, tier="Regular"):
    check_sender(settings)
    require(not settings.email_enabled, "Prepare the inbox test with EMAIL_ENABLED=false.")
    validate_recipient(recipient)
    require(tier in ("Lite","Regular","Pro"), "Choose Lite, Regular or Pro.")
    check_design(settings)
    output = artifact_path(settings,output)
    private_directory(output.parent)
    output.mkdir(mode=0o700,exist_ok=False)
    run_id = str(uuid.uuid4())
    # A fixed, clearly invalid payload makes the test QR reproducible. The UUID
    # binds the individual message/artifacts; no admitting identity is issued.
    token = TEST_QR
    require(is_test_token(token), "Synthetic tokens must use the permanently rejected test namespace.")
    # Only a rendering clone uses separate output storage. No email flag is overridden.
    rendering = replace(settings,generated=private_directory(output/"rendered"))
    card = CardRenderer(rendering).render("INVALID-INBOX-"+run_id,NAME,token,ticket_tier=tier)
    proof = verify_render(rendering,card,NAME,tier,token)
    badge = output/BADGE
    archive_invalid(card,badge)
    verify_invalid_qr(badge,token)
    message = build_test_message(recipient,badge,run_id,tier)
    write_private(output/"confirmation.eml",message.as_bytes(policy=policy.SMTP))
    write_private(output/"preview.html",browser_preview_html(message,BADGE).encode("utf-8"))
    assets = RemoteAssets(); assets.feed(TEMPLATE.read_text(encoding="utf-8"))
    manifest = {"version":1,"run_id":run_id,"synthetic_only":True,"sender":SENDER,"recipient":recipient,
        "name":NAME,"tier":tier,"test_token":token,"message_id":str(message["Message-ID"]),
        "template_sha256":sha(settings.template),"html_template_sha256":sha(TEMPLATE),
        "font_sha256":{str(weight):sha(path) for weight,path in google_sans_files().items()},
        "badge_verification":proof,"invalid_banner":True,"invalid_qr_verified":True,
        "files":{name:sha(output/name) for name in (BADGE,"confirmation.eml","preview.html")},
        "remote_assets":{url:"UNVERIFIED" for url in assets.urls},
        "inbox_compatibility":"UNVERIFIED; browser preview does not establish inbox compatibility"}
    private_json(output/"manifest.json",manifest)
    private_json(output/"prepared.json",{"manifest_sha256":sha(output/"manifest.json"),"attempts":0,"status":"PREPARED"})
    return {"status":"PREPARED","synthetic_only":True,"email_enabled":False,"smtp_connected":False,
            "google_accessed":False,"production_processing":"LOCKED","artifacts":str(output)}


def verify_invalid_qr(badge, token):
    import cv2
    require(is_test_token(token) and cv2.QRCodeDetector().detectAndDecode(cv2.imread(str(badge)))[0] == token,
            "The badge must decode to its permanently invalid synthetic token.")


class InboxSettings:
    """Only SMTP is scoped; operational/Sheet/worker methods remain hard-locked."""
    def __init__(self,settings,reload_settings):
        self.base,self.reload = settings,reload_settings

    def __getattr__(self,key):
        return getattr(self.base,key)

    def require_email(self):
        require(self.base.email_enabled, "EMAIL_ENABLED=false prohibits SMTP sending.")
        current = self.reload()
        require(current.email_enabled, "EMAIL_ENABLED=false prohibits SMTP sending.")
        check_sender(self.base); check_sender(current)
        require(all(getattr(current,key) == getattr(self.base,key) for key in
                    ("smtp_host","smtp_port","smtp_security","smtp_user","smtp_password","smtp_from")),
                "SMTP settings changed; stop before submission.")
        require(bool(current.smtp_host and current.smtp_password) and 0 < current.smtp_port < 65536
                and current.smtp_security in ("starttls","ssl"), "Complete the private SMTP TLS settings.")


def load_prepared(settings,recipient,output):
    output = artifact_path(settings,output)
    prepared = json.loads((output/"prepared.json").read_text())
    require(prepared["manifest_sha256"] == sha(output/"manifest.json"), "Prepared inbox-test manifest changed.")
    manifest = json.loads((output/"manifest.json").read_text())
    require(manifest["version"] == 1 and manifest["synthetic_only"] is True
            and manifest["name"] == NAME and manifest["sender"] == SENDER
            and manifest["recipient"] == recipient, "Recipient or synthetic inbox-test binding changed.")
    require(manifest["template_sha256"] == sha(settings.template) and manifest["html_template_sha256"] == sha(TEMPLATE),
            "Templates changed after inbox-test preparation.")
    require(manifest["font_sha256"] == {str(weight):sha(path) for weight,path in google_sans_files().items()},
            "Pinned fonts changed after inbox-test preparation.")
    run_id = str(uuid.UUID(manifest["run_id"]))
    require(manifest["test_token"] == TEST_QR,
            "The saved inbox-test token is not permanently invalid.")
    require(manifest["files"].keys() == {BADGE,"confirmation.eml","preview.html"}, "Unexpected inbox-test artifacts.")
    require(all(sha(output/name) == value for name,value in manifest["files"].items()), "Prepared inbox-test artifacts changed.")
    message = BytesParser(policy=policy.default).parsebytes((output/"confirmation.eml").read_bytes())
    require(not message.defects and len(message.get_all("To",[])) == len(message.get_all("From",[])) == 1
            and not any(message.get(key) for key in ("Cc","Bcc","Resent-To","Resent-Cc","Resent-Bcc","Resent-From"))
            and len(message["To"].addresses) == len(message["From"].addresses) == 1
            and message["To"].addresses[0].addr_spec == recipient and message["From"].addresses[0].addr_spec == SENDER
            and str(message["Message-ID"]) == manifest["message_id"] == "<organization-inbox-test-"+run_id+"@example.invalid>"
            and message["X-Event-Checkin-Test"] == "synthetic-invalid-badge"
            and str(message["Subject"]).startswith("[TEST — INVALID BADGE]"), "The prepared test email envelope changed.")
    require(all(NOTICE in message.get_body(preferencelist=(subtype,)).get_content() for subtype in ("plain","html")),
            "The test email notice is missing.")
    images = [part for part in message.walk() if part.get_content_type() == "image/png"]
    require(len(images) == 2 and {p.get_content_disposition() for p in images} == {"inline","attachment"}
            and all(p.get_payload(decode=True) == (output/BADGE).read_bytes() for p in images), "Test badge MIME contents changed.")
    browser_preview_html(message,BADGE)  # Enforce the one matching CID reference.
    verify_invalid_qr(output/BADGE,manifest["test_token"])
    return output,manifest,message


def send_prepared(settings,recipient,output,authorize_send,*,reload_settings=None):
    require(authorize_send is True, "Use --authorize-send explicitly for this one-recipient test.")
    validate_recipient(recipient); check_sender(settings)
    scoped = InboxSettings(settings,reload_settings or
                           (lambda:load_settings("production",settings.root,inbox_test=True)))
    scoped.require_email()  # False fails before artifact writes or SMTP construction.
    check_design(settings)
    output,manifest,message = load_prepared(settings,recipient,output)
    with file_lock(output/"send.lock",blocking=False):
        require(not (output/"attempt.json").exists(),
                "This test already has an attempt. Review its result/provider evidence; automatic resubmission is prohibited.")
        scoped.require_email()
        write_private(output/"attempt.json",json.dumps({"attempts":1,"status":"STARTED",
            "message_id":manifest["message_id"],"manifest_sha256":sha(output/"manifest.json"),
            "started_at":settings.now()}).encode())
        def checkpoint(stage,error):
            diagnostic = safe_diagnostic(error,stage) if error is not None else None
            private_json(output/("smtp-"+stage+".json"),{"stage":stage,"diagnostic":diagnostic,
                         "accepted":stage=="single_entry_smtp_accepted","attempts":1,"recorded_at":settings.now()})
        def before_submit():
            scoped.require_email()
            load_prepared(settings,recipient,output)  # Frozen recipient, MIME, PNG and invalid QR must still match.
        mailer = SMTPMailer(scoped,diagnostic_sink=checkpoint)
        try:
            mailer.submit_message(message,before_submit=before_submit)
        except (EmailNotSent,ConfigurationError):
            result = {"status":"NOT_ACCEPTED","attempts":1,"automatic_retry":False}
        except Exception:
            result = {"status":"UNCERTAIN","attempts":1,"automatic_retry":False}
        else:
            result = {"status":"SMTP_ACCEPTED","attempts":1,"automatic_retry":False}
        result["completed_at"] = settings.now()
        private_json(output/"result.json",result)
        return result


def register(commands):
    parser = commands.add_parser("production-inbox-test",help="Synthetic organization-sender inbox test; never reads Sheets")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--prepare",action="store_true",help="Offline render and MIME preview; requires email disabled")
    mode.add_argument("--send",action="store_true",help="Submit the prepared synthetic message at most once")
    parser.add_argument("--to",required=True,action=OneRecipient,help="One explicit recipient; must match the prepared message")
    parser.add_argument("--out",type=Path,required=True,help="Private child of runtime-production/organization-inbox-tests")
    parser.add_argument("--tier",choices=("Lite","Regular","Pro"),help="Preparation only; defaults to Regular")
    parser.add_argument("--authorize-send",action="store_true",help="Explicitly authorize the one test; email must also be enabled")


class OneRecipient(argparse.Action):
    def __call__(self,parser,namespace,value,option_string=None):
        if getattr(namespace,self.dest,None) is not None:
            raise argparse.ArgumentError(self,"Supply --to exactly once.")
        setattr(namespace,self.dest,value)


def run_command(settings,args):
    try:
        with diagnostic_context("organization_inbox_test"):
            if args.prepare:
                require(not args.authorize_send, "Preparation cannot authorize SMTP.")
                result = prepare(settings,args.to,args.out,args.tier or "Regular")
            else:
                require(args.tier is None, "Sending uses the tier already frozen in the prepared test.")
                result = send_prepared(settings,args.to,args.out,args.authorize_send)
        print(json.dumps(result,indent=2))
        return 0 if result["status"] in ("PREPARED","SMTP_ACCEPTED") else 1
    except Exception as exc:
        diagnostic = safe_diagnostic(exc,"organization_inbox_test")
        result = {"status":"STOPPED","diagnostic":diagnostic,"production_processing":"LOCKED"}
        if isinstance(exc,ConfigurationError):
            result["reason"] = str(exc)  # All ConfigurationErrors in this path use fixed local text.
        print(json.dumps(result,indent=2))
        return 1
