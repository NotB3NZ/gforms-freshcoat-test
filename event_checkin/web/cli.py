import argparse
import getpass
import json
import os
from pathlib import Path

from .adapters import FakeSheets, adapter
from .db import Database
from .security import hash_password
from .seed import payload, token
from .service import AdmissionService
from .settings import ROOT, WebSettings, config_path


def main():
    parser = argparse.ArgumentParser(description="Isolated FreshCoat staff web tools; synthetic by default")
    parser.add_argument("command", choices=["password", "preview-setup", "migrate", "seed", "import", "open-admissions", "qr-fixtures"])
    parser.add_argument("--source-root", type=Path, default=ROOT, help="Existing repository containing the reviewed .env.production and .env.web; preview-setup only")
    parser.add_argument("--write", action="store_true", help="Write password hash into private .env.web")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--reconciled", action="store_true", help="Confirm existing attendance was reviewed before cutover")
    args = parser.parse_args()
    if args.command == "preview-setup":
        from .preview import prepare_preview
        try:
            prepare_preview(ROOT, args.source_root)
        except FileExistsError:
            parser.error(".env.web.preview already exists; it was not overwritten.")
        except ValueError as error:
            parser.error(str(error))
        print("Created private .env.web.preview using the existing staff password. No Google requests or attendance changes were made.")
        print("Use WEB_ENV_FILE=.env.web.preview for import and server commands; see docs/WEB_PREVIEW.md.")
        return
    if args.command == "password":
        first = getpass.getpass("New shared staff password (at least 12 characters): ")
        if first != getpass.getpass("Repeat password: "):
            parser.error("Passwords do not match")
        hashed = hash_password(first)
        if args.write:
            path = config_path()
            lines = [line for line in path.read_text().splitlines() if not line.startswith("WEB_PASSWORD_HASH=")] if path.exists() else []
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as file:
                file.write("\n".join(lines + ["WEB_PASSWORD_HASH='" + hashed + "'"]) + "\n")
            path.chmod(0o600)
            print("Saved password hash in the selected private web configuration.")
        else:
            print("WEB_PASSWORD_HASH='" + hashed + "'")
        return
    s = WebSettings.load()
    if s.read_only_preview and args.command in ("open-admissions", "seed", "qr-fixtures"):
        parser.error("Read-only preview cannot open admissions or create demo tickets/registrations.")
    if args.command == "qr-fixtures":
        if not s.synthetic:
            parser.error("Development fixtures require synthetic mode")
        import qrcode
        output = ROOT / ".web-runtime/qr"
        output.mkdir(parents=True, exist_ok=True, mode=0o700)
        values = {key: token(key) for key in ("lite", "regular", "pro", "long", "special", "pass", "arrived", "cancelled", "invalid", "unapproved", "duplicate")}
        values.update(unknown=token("unknown"), malformed="https://example.invalid/do-not-navigate", controlled="INVALID_TEST_" + "x" * 30)
        for name, value in values.items():
            qrcode.make(value).save(output / ("DEVELOPMENT-" + name + ".png"))
        print("Synthetic QR images saved in .web-runtime/qr; upload one in Scan to exercise the actual decoder.")
        return
    db = Database(s.database_url)
    db.migrate(s)
    service = AdmissionService(db, s)
    if args.command == "seed":
        if not s.synthetic:
            parser.error("Seed is restricted to synthetic mode")
        if s.fixture.exists():
            parser.error("Fixture already exists; use a new private fixture/database to start another demo")
        fake = FakeSheets(s)
        fake.write(payload(s.event_id))
        print(service.import_rows(fake.rows()))
        service.open_admissions()
        print("Synthetic attendance reconciled; local admissions open.")
    elif args.command == "import":
        print(json.dumps(service.import_rows(adapter(s).rows(), dry_run=args.dry_run), indent=2))
    elif args.command == "open-admissions":
        if not args.reconciled:
            parser.error("Review initial existing attendance and run with --reconciled")
        service.open_admissions()
        print("Admissions open for the configured event. Legacy attendance writers must stay stopped.")
    else:
        print("Database migrations applied.")
    db.engine.dispose()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Account/HTTP errors may contain private URLs or response contents.
        from dotenv import dotenv_values
        preview_requested = os.environ.get("WEB_READ_ONLY_PREVIEW", dotenv_values(config_path(), interpolate=False).get("WEB_READ_ONLY_PREVIEW")) == "true"
        if preview_requested:
            import sys
            from .service import Conflict, NotReady, PreviewReadOnly
            from .preview import failure_message
            message = str(error) if isinstance(error, (Conflict, NotReady, PreviewReadOnly)) else failure_message(error)
            print("Preview could not complete: " + message + " No check-in/export was performed.", file=sys.stderr)
            raise SystemExit(1) from None
        raise
