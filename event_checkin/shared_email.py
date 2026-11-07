"""Explicit, private approvals for distinct attendees sharing one delivery email."""
import hashlib
import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .config import private_directory
from .sheets import SheetIntegrityError, normalize_email


APPROVALS_NAME = "shared-email-bookings.approved.json"
CLAIMS_SCHEMA_VERSION = 2


def approvals_path(settings):
    return settings.runtime / APPROVALS_NAME


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


def _email_hash(row):
    email_value = row.get("email")
    email = normalize_email(email_value if isinstance(email_value, str) else "")
    return _digest([email]) if email else ""


def registration_binding(settings, row):
    """Bind the exact physical response and its frozen form details, without storing PII."""
    return _digest([
        settings.event_id, row.source_id, row.number, row.get("submission_timestamp"),
        row.get("email"), row.get("name"), row.get("tier"), row.get("_price_category"),
    ])


def review_members(settings, rows, source_id, row_numbers):
    from .production import details, valid_details

    if not isinstance(source_id, str) or not source_id:
        raise SheetIntegrityError("Choose a configured production source.")
    if (not isinstance(row_numbers, (list, tuple)) or len(row_numbers) < 2
            or len(set(row_numbers)) != len(row_numbers)
            or any(type(n) is not int or n < 2 for n in row_numbers)):
        raise SheetIntegrityError("Specify at least two distinct physical registration rows.")
    members = []
    for number in sorted(row_numbers):
        found = [r for r in rows if r.source_id == source_id and r.number == number]
        if len(found) != 1 or not valid_details(found[0]):
            raise SheetIntegrityError("A requested row is absent, ambiguous, or has invalid registration details.")
        row = found[0]
        if row.get("event_id") not in ("", settings.event_id):
            raise SheetIntegrityError("A registration belongs to a different event.")
        if row.get("ticket_token"):
            name, tier, price = details(row)
            if (normalize_email(row.get("email") or ""), name, tier, price, source_id) != (
                    row.get("normalized_email"), row.get("ticket_name"), row.get("ticket_tier"),
                    row.get("price_category"), row.get("source_id")):
                raise SheetIntegrityError("An issued registration has changed frozen details; review it before grouping.")
        if row.get("email_status") in ("SENDING", "AMBIGUOUS") or row.get("state") == "EMAIL_REVIEW":
            raise SheetIntegrityError("An uncertain delivery must be reviewed before grouping.")
        members.append({
            "row": number,
            "registration_sha256": registration_binding(settings, row),
            "record_id": row.get("record_id") or "",
        })
    selected = [r for r in rows if r.source_id == source_id and r.number in set(row_numbers)]
    emails = {normalize_email(r.get("email") or "") for r in selected}
    if len(emails) != 1 or not next(iter(emails), ""):
        raise SheetIntegrityError("The selected registrations do not share one delivery email.")
    review = {
        "event_id": settings.event_id,
        "source_id": source_id,
        "email_sha256": _email_hash(selected[0]),
        "members": members,
    }
    return review, _digest(review), selected


def _read_approvals(settings):
    path = approvals_path(settings)
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text())
        if (payload.get("version") != 1 or payload.get("event_id") != settings.event_id
                or not isinstance(payload.get("groups"), list)):
            raise ValueError()
        return payload["groups"]
    except (OSError, ValueError, TypeError, AttributeError):
        raise SheetIntegrityError("Private shared-email approval file is invalid; staff review required.") from None


def save_approval(settings, review, review_sha256, *, confirm_distinct_attendees,
                  confirm_payment_coverage):
    if not confirm_distinct_attendees or not confirm_payment_coverage:
        raise SheetIntegrityError("Approval requires both distinct-attendee and payment-coverage confirmations.")
    if review_sha256 != _digest(review):
        raise SheetIntegrityError("Review digest changed; run the review command again.")
    path = approvals_path(settings)
    private_directory(path.parent)
    from .locking import file_lock
    from .production import private_json
    with file_lock(settings.runtime / "shared-email-approvals.lock"):
        groups = _read_approvals(settings)
        signature = _digest(review)
        if any(group.get("review_sha256") == signature for group in groups):
            raise SheetIntegrityError("This exact shared-email group is already approved.")
        groups.append({
            "group_id": str(uuid.uuid4()),
            **review,
            "review_sha256": signature,
            "confirm_distinct_attendees": True,
            "confirm_payment_coverage": True,
            "approved_at": datetime.now(timezone.utc).isoformat(),
        })
        private_json(path, {"version": 1, "event_id": settings.event_id, "groups": groups})
        path.chmod(0o600)
    return groups[-1]["group_id"]


def apply_approved_groups(settings, rows):
    """Annotate only groups whose every member still matches its reviewed binding."""
    groups = _read_approvals(settings)
    row_index = {(r.source_id, r.number): r for r in rows}
    for row in rows:
        row.data.pop("_shared_email_review_required", None)
        row.data.pop("_shared_email_group_id", None)
        row.data.pop("_shared_email_member_rows", None)
        row.data.pop("_shared_email_member_record_ids", None)
    assignments = {}
    for group in groups:
        try:
            review_payload = {key: group[key] for key in
                              ("event_id", "source_id", "email_sha256", "members")}
            if (group["event_id"] != settings.event_id or not group["confirm_distinct_attendees"]
                    or not group["confirm_payment_coverage"] or not group["group_id"]
                    or len(group["members"]) < 2
                    or group.get("review_sha256") != _digest(review_payload)):
                raise ValueError()
            current = []
            for member in group["members"]:
                row = row_index.get((group["source_id"], member["row"]))
                if (row is None or registration_binding(settings, row) != member["registration_sha256"]
                        or (member.get("record_id") and row.get("record_id") != member["record_id"])):
                    current = []
                    break
                current.append(row)
            if (len(current) != len(group["members"])
                    or len({r.number for r in current}) != len(current)
                    or {_email_hash(r) for r in current} != {group["email_sha256"]}
                    or len({_email_hash(r) for r in current}) != 1):
                # A moved or changed member invalidates the approval for the whole group.
                for member in group.get("members", []):
                    stale = row_index.get((group.get("source_id"), member.get("row")))
                    if stale is not None:
                        stale.data["_shared_email_review_required"] = True
                continue
            for row in current:
                key = (row.source_id, row.number)
                if key in assignments:
                    raise SheetIntegrityError("Overlapping shared-email approvals; staff review required.")
                assignments[key] = (group, current)
        except (KeyError, TypeError, ValueError):
            raise SheetIntegrityError("Private shared-email approval entry is malformed; staff review required.") from None
    for key, (group, current) in assignments.items():
        row = row_index[key]
        row.data.pop("_shared_email_review_required", None)
        row.data["_shared_email_group_id"] = group["group_id"]
        row.data["_shared_email_member_rows"] = [member.number for member in current]
        row.data["_shared_email_member_record_ids"] = [member.get("record_id") or "" for member in current]
    return rows


def claims_schema(path):
    """Return legacy, shared_email, new, or missing without modifying the database."""
    path = Path(path)
    if not path.exists() or path.stat().st_size == 0:
        return "new"
    try:
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "claims" not in tables:
                return "new"
            columns = db.execute("PRAGMA table_info(claims)").fetchall()
            primary = [r[1] for r in sorted((r for r in columns if r[5]), key=lambda r: r[5])]
            if primary == ["event_id", "email"]:
                return "legacy"
            if primary == ["event_id", "record_id"]:
                return "shared_email"
            return "unsupported"
    except sqlite3.Error:
        return "unsupported"


def migrate_claims_database(database_path, backup_path):
    """Back up then transactionally remove the event/email uniqueness constraint."""
    database_path, backup_path = Path(database_path).resolve(), Path(backup_path).resolve()
    if database_path == backup_path or not database_path.is_file():
        raise SheetIntegrityError("Migration requires an existing journal and a separate backup path.")
    if claims_schema(database_path) != "legacy":
        raise SheetIntegrityError("Journal is not on the legacy event/email claims schema.")
    private_directory(backup_path.parent)
    fd = os.open(backup_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    try:
        with sqlite3.connect(database_path) as source, sqlite3.connect(backup_path) as backup:
            source.backup(backup)
        backup_path.chmod(0o600)
        with sqlite3.connect(database_path, timeout=30, isolation_level=None) as db:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN EXCLUSIVE")
            before = db.execute("SELECT * FROM claims ORDER BY event_id,email").fetchall()
            db.execute("""CREATE TABLE claims_shared_email (
                event_id TEXT, email TEXT, record_id TEXT UNIQUE,
                participant_id TEXT UNIQUE, token TEXT UNIQUE, name TEXT, tier TEXT, price TEXT,
                source TEXT, PRIMARY KEY(event_id,record_id))""")
            db.execute("""INSERT INTO claims_shared_email
                (event_id,email,record_id,participant_id,token,name,tier,price,source)
                SELECT event_id,email,record_id,participant_id,token,name,tier,price,source FROM claims""")
            db.execute("DROP TABLE claims")
            db.execute("ALTER TABLE claims_shared_email RENAME TO claims")
            db.execute("CREATE INDEX claims_event_email_idx ON claims(event_id,email)")
            db.execute("PRAGMA user_version=2")
            after = db.execute("SELECT * FROM claims ORDER BY event_id,email,record_id").fetchall()
            if sorted(before) != sorted(after):
                raise SheetIntegrityError("Claim migration changed existing identities; transaction rolled back.")
            db.execute("COMMIT")
        if claims_schema(database_path) != "shared_email":
            raise SheetIntegrityError("Claim migration schema verification failed.")
        return {"claims_preserved": len(before), "schema": "shared_email", "backup": str(backup_path)}
    except Exception:
        try:
            with sqlite3.connect(database_path, isolation_level=None) as db:
                db.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
