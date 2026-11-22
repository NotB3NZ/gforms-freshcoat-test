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
from .settings import ROOT, WebSettings


def main():
    parser = argparse.ArgumentParser(description="Isolated FreshCoat staff web tools; synthetic by default")
    parser.add_argument("command", choices=["password", "migrate", "seed", "import", "open-admissions", "qr-fixtures"])
    parser.add_argument("--write", action="store_true", help="Write password hash into private .env.web")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--reconciled", action="store_true", help="Confirm existing attendance was reviewed before cutover")
    args = parser.parse_args()
    if args.command == "password":
        first = getpass.getpass("New shared staff password (at least 12 characters): ")
        if first != getpass.getpass("Repeat password: "):
            parser.error("Passwords do not match")
        hashed = hash_password(first)
        if args.write:
            path = ROOT / ".env.web"
            lines = [line for line in path.read_text().splitlines() if not line.startswith("WEB_PASSWORD_HASH=")] if path.exists() else []
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as file:
                file.write("\n".join(lines + ["WEB_PASSWORD_HASH='" + hashed + "'"]) + "\n")
            path.chmod(0o600)
            print("Saved private .env.web password hash.")
        else:
            print("WEB_PASSWORD_HASH='" + hashed + "'")
        return
    s = WebSettings.load()
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
    main()
