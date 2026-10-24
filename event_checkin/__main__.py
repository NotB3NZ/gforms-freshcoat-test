import argparse
import sys
import time

from .cards import CardRenderer, inspect_template
from .config import load_settings, production_sources
from .locking import file_lock
from .mailer import SMTPMailer
from .sheets import GoogleSheet
from .worker import Worker


def main():
    parser = argparse.ArgumentParser(description="Event registration tools. Run from the event-checkin directory.")
    parser.add_argument("--profile", choices=("test", "production"), default=None,
                        help="Isolated configuration; defaults to test (or EVENT_CHECKIN_PROFILE)")
    commands = parser.add_subparsers(dest="command", required=True)
    from . import production_cli
    production_cli.register(commands)
    commands.add_parser("profile-info", help="Show non-secret profile paths and readiness; no account access")
    commands.add_parser("check-template", help="Inspect bindings and QR placement without account access")
    preview = commands.add_parser("preview", help="Render the actual template with a clearly fake QR; sends no email")
    preview.add_argument("--tier", choices=("Lite", "Regular", "Pro"), default=None,
                         help="Preview-only tier value for templates declaring CARD_TIER_FIELD")
    commands.add_parser("check-config", help="Validate local Sheet settings without connecting to Google")
    commands.add_parser("check-sheet", help="Read-only connection/header check; never prints row contents")
    commands.add_parser("init-sheet", help="Add configured metadata headers; does not queue rows")
    commands.add_parser("queue-unprocessed", help="Explicitly queue old rows with no identity/state; no email is sent by this command")
    worker = commands.add_parser("worker")
    worker.add_argument("--once", action="store_true", help="One pass instead of polling")
    for command in ("retry", "resend", "cancel"):
        p = commands.add_parser(command)
        p.add_argument("participant_id")
    resolve = commands.add_parser("resolve-email")
    resolve.add_argument("participant_id")
    resolve.add_argument("--outcome", choices=("sent", "retry"), required=True)
    server = commands.add_parser("server")
    server.add_argument("--port", type=int, default=8000)
    commands.add_parser("scanner")
    email_test = commands.add_parser("send-test", help="Send only the local preview to one explicitly authorized test recipient")
    email_test.add_argument("--to", required=True)
    email_test.add_argument("--authorize-send", action="store_true", required=True)
    args = parser.parse_args()
    try:
        s = (load_settings(profile=args.profile, single_entry=True) if args.command == "production-single-send"
             else load_settings(profile=args.profile))
        if s.profile == "production" and args.command not in ("profile-info", "check-config", "check-template", "preview") + production_cli.COMMANDS:
            s.require_operational()
        if args.command in production_cli.COMMANDS:
            return production_cli.run(s, args)
        if args.command == "profile-info":
            print("Profile: " + s.profile)
            print("Configuration: " + str(s.env_file))
            print("Runtime records: " + str(s.runtime))
            print("Generated cards: " + str(s.generated))
            print("Template: " + str(s.template))
            if s.profile == "production":
                print("Configured sources: " + str(len(production_sources(s))))
                print("Live production operations: LOCKED (human schema/event-ID checkpoint and email authorization pending)")
            else:
                print("Email enabled: " + str(s.email_enabled))
            return
        if args.command == "check-config":
            if s.profile == "production":
                sources = production_sources(s)
                print("Production profile is isolated; " + str(len(sources)) + " sources configured locally. Google access has not been tested by this command. Live operations remain locked.")
                return
            s.require_sheet()
            print("Local Sheet settings are present. Google account access has not been tested.")
            return
        if args.command == "check-template":
            template, dimensions = inspect_template(s)
            print("Template verified: {} x {}; {} uses {{{{{}}}}}; QR x={}, y={}, size={}.".format(
                *dimensions, s.frame, s.name_field, s.qr_x, s.qr_y, s.qr_size))
            return
        if args.command == "preview":
            template, _ = inspect_template(s)
            tier = args.tier
            if s.tier_field in template["fields"]["properties"] and tier is None:
                tier = "Regular"
            path = CardRenderer(s).render("LOCAL-PREVIEW", "Local Test Participant", "LOCAL_PREVIEW_ONLY_" + "0" * 24 + "X",
                                          ticket_tier=tier)
            print("Local preview written to " + str(path) + ". Its fake QR is not an issued ticket.")
            return
        if args.command == "scanner":
            from .scanner import run_camera
            run_camera(s)
            return
        if args.command == "send-test":
            import re
            import uuid
            from .config import ConfigurationError
            if not re.fullmatch(r"[^\s@<>\r\n]+@[^\s@<>\r\n]+\.[^\s@<>\r\n]+", args.to):
                raise ConfigurationError("Supply one valid, explicitly authorized test recipient.")
            # This command cannot consume registrations or send any participant's real QR.
            path = CardRenderer(s).render("LOCAL-PREVIEW", "Local Test Participant", "LOCAL_PREVIEW_ONLY_" + "0" * 24 + "X")
            SMTPMailer(s).send(args.to, "Test Participant", "LOCAL-PREVIEW", path,
                              "<" + str(uuid.uuid4()) + "@event-checkin.local>")
            print("SMTP accepted the test message. Confirm receipt and attachment in the test inbox.")
            return
        if args.command == "server":
            import uvicorn
            from .server import create_app
            s.require_staff()
            s.require_sheet()
            # Local only, no auto-reload, one central process. Lifespan also enforces a singleton.
            uvicorn.run(create_app(settings=s), host="127.0.0.1", port=args.port,
                        workers=1, access_log=False, log_level="critical")
            return
        sheet = GoogleSheet(s)
        if args.command == "init-sheet":
            with file_lock(s.runtime / "worker.lock", blocking=False), file_lock(s.runtime / "sheets.lock"):
                sheet.initialize()
            print("Metadata headers ready. Existing responses were not queued.")
            return
        if args.command == "check-sheet":
            with file_lock(s.runtime / "sheets.lock"):
                rows = sheet.rows()
            print("Sheet connection and configured headers verified. Registration count: " + str(len(rows)))
            return
        if args.command == "queue-unprocessed":
            count = 0
            with file_lock(s.runtime / "worker.lock", blocking=False), file_lock(s.runtime / "sheets.lock"):
                for row in sheet.rows():
                    if not row.get("participant_id") and row.get("state") in ("", "INVALID") and row.get("event_id") in ("", s.event_id):
                        sheet.update(row, {"event_id": s.event_id, "state": "PENDING", "updated_at": s.now()})
                        count += 1
            print("Queued registrations: " + str(count) + ". Worker will process them when run.")
            return
        service = Worker(s, sheet, CardRenderer(s), SMTPMailer(s))
        if args.command == "cancel":
            service.cancel(args.participant_id)
            print("Ticket cancelled. Identity, email history and attendance were preserved.")
            return
        if args.command in ("retry", "resend", "resolve-email"):
            service.retry(args.participant_id, resend=args.command == "resend", resolution=getattr(args, "outcome", None))
            print("Registration updated. Run worker --once to process queued work.")
            return
        while True:
            try:
                count = service.run_once()
                print("Worker pass complete; registrations considered: " + str(count), flush=True)
            except Exception:
                print("Worker stopped this pass: Sheet/configuration could not be confirmed. No new jobs processed; check access and states, then retry.", file=sys.stderr, flush=True)
                if args.once:
                    return 1
            if args.once:
                return
            time.sleep(s.poll_seconds)
    except KeyboardInterrupt:
        print("Stopped. Pending requests/jobs will recover from saved state.")
    except Exception as error:
        # Only our explicit local error messages are suitable for terminal output.
        from .config import ConfigurationError
        from .cards import RenderError
        from .sheets import SheetIntegrityError
        from .mailer import EmailNotSent, EmailAmbiguous
        if isinstance(error, (ConfigurationError, RenderError, SheetIntegrityError, EmailNotSent, EmailAmbiguous)):
            print(str(error), file=sys.stderr)
        else:
            print("Operation could not complete. Check configuration, dependencies and account access locally. No credentials were logged.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
