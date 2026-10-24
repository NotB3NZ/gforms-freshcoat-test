"""Approval-gated, finite production batches. No polling and no implicit resend.

The adapter is the write authorization boundary: live adapters stay locked; fixtures
exercise the identical selection/issuance/recovery logic without Google or SMTP.
"""
import hashlib
import json
import re
import secrets
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path

from .config import private_directory
from .locking import file_lock
from .mailer import EmailNotSent
from .production_sheets import APPROVAL_HEADERS, validate_production_identities
from .sheets import SheetIntegrityError, normalize_email
from .tiers import TIERS, resolve_tier
from .test_identity import is_test_token


def approved(value):
    # bool is checked explicitly: 1, nonempty strings and "FALSE" are NOT approval.
    return value is True or (isinstance(value, str) and value == "TRUE")


def text(value):
    return value if isinstance(value, str) else ""


def recipient(row):
    return normalize_email(text(row.get("email")))


def details(row):
    return (text(row.get("name")).strip(), resolve_tier(row.source_id, row.get("tier")), text(row.get("_price_category")))


def valid_details(row):
    name, tier, price = details(row)
    return bool(name and len(name) <= 160 and not any(ord(c) < 32 or ord(c) == 127 for c in name)
                and re.fullmatch(r"[^\s@<>\r\n]+@[^\s@<>\r\n]+\.[^\s@<>\r\n]+", recipient(row))
                and tier in TIERS and price)


def has_approvals(row):
    return all(approved(row.get(key)) for key in APPROVAL_HEADERS)


def same_row(left, right):
    return left.source_id == right.source_id and (
        left.get("record_id") == right.get("record_id") if left.get("record_id") and right.get("record_id")
        else left.number == right.number)


def target(row):
    return row.source_id + ":" + (row.get("record_id") or "row:" + str(row.number))


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def fingerprint(row):
    # Approval changes are evaluated independently. Form/source edits invalidate a plan.
    return digest([row.source_id, row.get("name"), row.get("email"), row.get("tier"), row.get("_price_category")])


def issued_identity(row):
    return digest([row.get(k) for k in ("record_id", "participant_id", "ticket_token", "normalized_email",
                                        "ticket_name", "ticket_tier", "price_category", "source_id")])


def reasons(row, rows, event_id, action="send"):
    blocked = []
    if is_test_token(row.get("ticket_token")):
        blocked.append("reserved controlled-test identity; cleanup/review required")
    if row.get("event_id") not in ("", event_id):
        blocked.append("different event")
    missing = [key for key in APPROVAL_HEADERS if not approved(row.get(key))]
    if missing:
        blocked.append("waiting for approval: " + ", ".join(missing))
    if not valid_details(row):
        blocked.append("review invalid name/email or unknown raw tier/price category")
    if row.get("ticket_status") not in ("", "ACTIVE") or row.get("state") == "CANCELLED":
        blocked.append("cancelled or unknown ticket status")
    if row.get("email_status") in ("SENDING", "AMBIGUOUS") or row.get("state") == "EMAIL_REVIEW":
        blocked.append("uncertain delivery requires staff review")
    if row.get("ticket_token"):
        name, tier, price = details(row)
        if (recipient(row), name, tier, price, row.source_id) != (
                row.get("normalized_email"), row.get("ticket_name"), row.get("ticket_tier"),
                row.get("price_category"), row.get("source_id")):
            blocked.append("issued recipient/name/tier/source changed; staff review required")
    email = recipient(row)
    group = [r for r in rows if r.get("event_id") in ("", event_id) and email and
             email in (recipient(r), r.get("normalized_email"))]
    issued = [r for r in group if r.get("ticket_token")]
    if len(issued) > 1:
        blocked.append("conflicting issued tickets for one event/email")
    elif issued:
        if not same_row(row, issued[0]):
            blocked.append("duplicate of issued canonical registration " + target(issued[0]))
        if any(has_approvals(r) and valid_details(r) and details(r) != details(issued[0]) for r in group):
            blocked.append("conflicting approved registrations; staff review required")
    else:
        eligible = [r for r in group if has_approvals(r) and valid_details(r)]
        if len({details(r) for r in eligible}) > 1:
            blocked.append("conflicting approved registrations; staff review required")
        elif eligible and not same_row(row, eligible[0]):
            blocked.append("duplicate of approved canonical registration " + target(eligible[0]))
    sent = row.get("email_status") == "SENT" or row.get("state") == "SENT"
    if action == "resend":
        if not sent:
            blocked.append("resend requires a sent registration")
    elif sent:
        blocked.append("already sent; explicit resend required")
    if action == "retry" and row.get("state") not in ("READY", "RENDER_ERROR", "EMAIL_ERROR", "PROCESSING", "PENDING"):
        blocked.append("retry requires an existing recoverable job")
    elif action == "send" and row.get("state") in ("RENDER_ERROR", "EMAIL_ERROR"):
        blocked.append("failed job requires explicit retry")
    return blocked


def select(rows, ids=None, source=None, all_sources=False):
    if sum((bool(ids), bool(source), bool(all_sources))) != 1:
        raise SheetIntegrityError("Choose exactly one selector: IDs, source, or all sources.")
    if source:
        if source not in ("regular", "discount_15", "discount_10"):
            raise SheetIntegrityError("Unknown source selection.")
        return [r for r in rows if r.source_id == source]
    if all_sources:
        return list(rows)
    selected = []
    for identifier in ids:
        matches = [r for r in rows if identifier in (target(r), r.get("record_id"), r.get("participant_id"),
                                                     r.source_id + ":" + text(r.get("participant_id"))) and identifier]
        if len(matches) != 1:
            raise SheetIntegrityError("Target is missing or ambiguous; use a source-qualified permanent record ID or a fresh preview row locator.")
        if any(same_row(matches[0], r) for r in selected):
            raise SheetIntegrityError("Repeated target in explicit selection.")
        selected.append(matches[0])
    return selected


def preview(settings, sheet, *, ids=None, source=None, all_sources=False, limit=25, action="send", regenerate=False):
    if not isinstance(limit, int) or limit < 1 or action not in ("send", "retry", "resend"):
        raise SheetIntegrityError("Choose a positive batch limit and send/retry/resend action.")
    rows = sheet.rows()  # ALL sources must be readable, even for one targeted registration.
    validate_production_identities(rows)
    selected = select(rows, ids, source, all_sources)
    entries, report, count = [], [], 0
    for row in selected:
        blocked = reasons(row, rows, settings.event_id, action)
        eligible = not blocked
        included = eligible and count < limit
        if included:
            count += 1
        display = {"target": target(row), "name": row.get("name"), "email": row.get("email"),
                   "tier": row.get("tier"), "mapped_tier": details(row)[1], "price_category": row.get("_price_category"),
                   "approvals": {key: approved(row.get(key)) for key in APPROVAL_HEADERS},
                   "state": row.get("state") or ("WAITING_APPROVAL" if not has_approvals(row) else "UNPROCESSED"),
                   "sent": row.get("email_status") == "SENT" or row.get("state") == "SENT",
                   "blocking_reasons": blocked, "in_batch": included}
        report.append(display)
        # A finite snapshot of selected waiting rows may receive registration IDs/state,
        # never tickets. They do not consume the delivery limit. Sent/failed/unselected
        # records are never requeued by this discovery step.
        waiting = not has_approvals(row) and not row.get("ticket_token") and row.get("event_id") in ("", settings.event_id)
        if included or waiting:
            entries.append({"target": target(row), "source": row.source_id,
                            "record_id": row.get("record_id"), "fingerprint": fingerprint(row),
                            "issued_identity": issued_identity(row) if row.get("ticket_token") else "",
                            "delivery": [row.get(k) for k in ("email_status", "message_id", "email_sent_at", "email_attempts")],
                            "process": included})
    plan = {"version": 1, "plan_id": str(uuid.uuid4()), "event_id": settings.event_id,
            "profile": settings.profile, "live": sheet.live, "dataset": sheet.dataset,
            "action": action, "regenerate": regenerate, "limit": limit, "entries": entries}
    return report, plan


class BatchJournal:
    def __init__(self, settings):
        self.path = private_directory(settings.runtime) / "production-batches.sqlite3"
        if not self.path.exists():
            self.path.touch(mode=0o600)
        self.path.chmod(0o600)
        with self.db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS plans (id TEXT PRIMARY KEY, digest TEXT NOT NULL)")
            db.execute("""CREATE TABLE IF NOT EXISTS actions (plan_id TEXT, target TEXT, record_id TEXT NOT NULL,
                          message_id TEXT NOT NULL, done INTEGER DEFAULT 0, PRIMARY KEY(plan_id,target))""")
            db.execute("""CREATE TABLE IF NOT EXISTS claims (event_id TEXT, email TEXT, record_id TEXT UNIQUE,
                          participant_id TEXT UNIQUE, token TEXT UNIQUE, name TEXT, tier TEXT, price TEXT,
                          source TEXT, PRIMARY KEY(event_id,email))""")

    def db(self):
        return closing(sqlite3.connect(self.path, timeout=30, isolation_level=None))

    def bind(self, plan):
        with self.db() as db:
            value = digest(plan)
            previous = db.execute("SELECT digest FROM plans WHERE id=?", (plan["plan_id"],)).fetchone()
            if previous and previous[0] != value:
                raise SheetIntegrityError("A saved batch was edited after execution began; review required.")
            db.execute("INSERT OR IGNORE INTO plans VALUES(?,?)", (plan["plan_id"], value))

    def action(self, plan, entry):
        with self.db() as db:
            db.execute("INSERT OR IGNORE INTO actions(plan_id,target,record_id,message_id) VALUES(?,?,?,?)",
                       (plan["plan_id"], entry["target"], entry["record_id"] or "R-" + str(uuid.uuid4()),
                        "<" + str(uuid.uuid4()) + "@event-checkin.local>"))
            return db.execute("SELECT record_id,message_id,done FROM actions WHERE plan_id=? AND target=?",
                              (plan["plan_id"], entry["target"])).fetchone()

    def complete(self, plan, entry):
        with self.db() as db:
            db.execute("UPDATE actions SET done=1 WHERE plan_id=? AND target=?", (plan["plan_id"], entry["target"]))

    def claim(self, settings, row):
        values = (settings.event_id, recipient(row), row.get("record_id"),
                  row.get("participant_id") or "P-" + str(uuid.uuid4()),
                  row.get("ticket_token") or secrets.token_urlsafe(32), *details(row), row.source_id)
        with self.db() as db:
            old = db.execute("SELECT * FROM claims WHERE event_id=? AND email=?", values[:2]).fetchone()
            if old:
                if old[2] != values[2] or old[5:] != values[5:] or (row.get("ticket_token") and old[3:5] != values[3:5]):
                    raise SheetIntegrityError("Durable ticket reservation conflicts with current registration; review required.")
                return old[3:5]
            try:
                db.execute("INSERT INTO claims VALUES(?,?,?,?,?,?,?,?,?)", values)
            except sqlite3.IntegrityError:
                raise SheetIntegrityError("Durable ticket identity conflicts; review required.") from None
            return values[3:5]


def private_json(path, payload):
    path = Path(path)
    private_directory(path.parent)
    # Atomic replacement: an interrupted local save preserves the previous snapshot.
    import tempfile
    import os
    fd, staging = tempfile.mkstemp(prefix=".batch-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        Path(staging).replace(path)
    finally:
        Path(staging).unlink(missing_ok=True)


class BatchProcessor:
    def __init__(self, settings, sheet, renderer, mailer, persist=None):
        self.s, self.sheet, self.renderer, self.mailer = settings, sheet, renderer, mailer
        self.persist = persist or (lambda: None)
        self.journal = None  # A preview does not create runtime state.

    def save(self, row, **changes):
        changes["updated_at"] = self.s.now()
        self.sheet.update(row, changes)
        self.persist()  # Fixture checkpoints survive every successful metadata write.

    def locate(self, rows, entry, record_id):
        matches = [r for r in rows if r.source_id == entry["source"] and r.get("record_id") == record_id]
        if not matches and not entry["record_id"]:
            matches = [r for r in rows if target(r) == entry["target"] and not r.get("record_id")]
        if len(matches) != 1 or fingerprint(matches[0]) != entry["fingerprint"]:
            raise SheetIntegrityError("Selected registration moved/changed or cannot be resolved uniquely; create a fresh preview.")
        if entry.get("issued_identity") and issued_identity(matches[0]) != entry["issued_identity"]:
            raise SheetIntegrityError("Issued identity changed after selection; staff review required.")
        return matches[0]

    def checked(self, entry, record_id, action):
        rows = self.sheet.rows()
        row = self.locate(rows, entry, record_id)
        if row.get("ticket_token"):
            self.journal.claim(self.s, row)  # Verify durable identity at every later gate too.
        return row, reasons(row, rows, self.s.event_id, action)

    def blocked(self, row, blocked):
        # Never rewrite delivery history or reactivate/cancel an issued ticket here.
        if row.get("email_status") in ("SENDING", "AMBIGUOUS"):
            self.save(row, state="EMAIL_REVIEW", email_status="AMBIGUOUS", last_error="Interrupted/uncertain email; review provider logs.")
        elif not row.get("ticket_token"):
            state = "WAITING_APPROVAL" if not has_approvals(row) else "REGISTRATION_REVIEW"
            self.save(row, state=state, last_error="; ".join(blocked))
        elif row.get("state") not in ("SENT", "EMAIL_REVIEW", "EMAIL_ERROR", "RENDER_ERROR"):
            self.save(row, state="REGISTRATION_REVIEW", last_error="; ".join(blocked))

    def execute(self, plan):
        self.sheet.require_write()  # Fail BEFORE journal creation, discovery, rendering or SMTP.
        if (plan.get("version"), plan.get("event_id"), plan.get("profile"), plan.get("live"), plan.get("dataset")) != (
                1, self.s.event_id, self.s.profile, self.sheet.live, self.sheet.dataset):
            raise SheetIntegrityError("Batch belongs to another event, profile or adapter mode.")
        if plan.get("action") not in ("send", "retry", "resend") or not isinstance(plan.get("limit"), int) or plan["limit"] < 1:
            raise SheetIntegrityError("Invalid saved batch action/limit.")
        entries = plan.get("entries", [])
        if sum(bool(e["process"]) for e in entries) > plan["limit"] or len({e["target"] for e in entries}) != len(entries):
            raise SheetIntegrityError("Invalid or repeated batch targets.")
        outcomes = []
        with file_lock(self.s.runtime / "worker.lock", blocking=False), file_lock(self.s.runtime / "sheets.lock"):
            self.journal = BatchJournal(self.s)
            self.journal.bind(plan)
            # Preflight every selected locator and ALL source access before any writes.
            actions = [self.journal.action(plan, e) for e in entries]
            rows = self.sheet.rows()
            for entry, (rid, _, done) in zip(entries, actions):
                if not done:
                    self.locate(rows, entry, rid)
            for entry, (rid, mid, done) in zip(entries, actions):
                if done:
                    outcomes.append("completed previously")
                    continue
                outcome = self.process(plan, entry, rid, mid)
                outcomes.append(outcome)
                if outcome in ("sent", "ready", "already sent"):
                    self.journal.complete(plan, entry)
        return outcomes

    def process(self, plan, entry, rid, resend_mid):
        action = plan["action"]
        row, blocked = self.checked(entry, rid, action)
        if row.get("event_id") not in ("", self.s.event_id):
            return "different event; no changes"
        if row.get("email_status") == "SENDING":
            self.blocked(row, ["uncertain delivery requires review"])
            return "review"
        if action != "resend" and (row.get("email_status") == "SENT" or row.get("state") == "SENT"):
            return "already sent"
        # Resume this exact resend intent, including after a lost SENT-write response.
        own_resend = action == "resend" and row.get("message_id") == resend_mid
        if own_resend and row.get("email_status") == "SENT":
            return "already sent"
        if own_resend:
            row, blocked = self.checked(entry, rid, "retry")
        if not row.get("record_id"):
            self.save(row, record_id=rid, event_id=self.s.event_id, source_id=row.source_id,
                      price_category=row.get("_price_category"))
            row, blocked = self.checked(entry, rid, "retry" if own_resend else action)
        if blocked:
            self.blocked(row, blocked)
            return "blocked"
        if not entry["process"]:
            return "waiting snapshot; use a new preview for later approvals"
        if action == "resend" and not own_resend:
            current_delivery = [row.get(k) for k in ("email_status", "message_id", "email_sent_at", "email_attempts")]
            if current_delivery != entry["delivery"]:
                return "delivery changed; fresh resend preview required"
            self.save(row, state="PROCESSING", email_status="RETRY", message_id=resend_mid)
        pid, token = self.journal.claim(self.s, row)
        name, tier, price = details(row)
        self.save(row, participant_id=pid, ticket_token=token, normalized_email=recipient(row),
                  ticket_name=name, ticket_tier=tier, price_category=price, source_id=row.source_id,
                  state="PROCESSING", ticket_status="ACTIVE", last_error="")
        row, blocked = self.checked(entry, rid, "retry")
        if row.get("ticket_token") != token or row.get("participant_id") != pid:
            raise SheetIntegrityError("Ticket persistence could not be confirmed; no render/send attempted.")
        if blocked:
            self.blocked(row, blocked)
            return "blocked"
        path = Path(row.get("card_path")) if row.get("card_path") else None
        valid_path = path and path.resolve().is_relative_to(self.s.generated.resolve()) and path.is_file()
        if plan.get("regenerate") or row.get("render_status") != "RENDERED" or not valid_path:
            self.save(row, render_status="RENDERING", render_attempts=str(int(row.get("render_attempts") or 0) + 1))
            try:
                path = self.renderer.render(pid, name, token, ticket_tier=tier)
            except Exception:
                row, _ = self.checked(entry, rid, "retry")
                self.save(row, state="RENDER_ERROR", render_status="FAILED", last_error="Card rendering failed; fix renderer then retry this selection.")
                return "render error"
            row, blocked = self.checked(entry, rid, "retry")
            self.save(row, render_status="RENDERED", card_path=str(path))
        if not self.s.email_enabled:
            row, blocked = self.checked(entry, rid, "retry")
            if blocked:
                self.blocked(row, blocked)
                return "blocked"
            self.save(row, state="READY", email_status="DISABLED")
            return "ready"
        self.s.require_email()
        row, blocked = self.checked(entry, rid, "retry")
        if blocked:
            self.blocked(row, blocked)
            return "blocked"
        mid = row.get("message_id") or "<" + str(uuid.uuid4()) + "@event-checkin.local>"
        self.save(row, email_status="SENDING", message_id=mid,
                  email_attempts=str(int(row.get("email_attempts") or 0) + 1))
        # Last authoritative read, including every source and current recipient/tier,
        # after persisting intent and immediately before SMTP. Selection never authorizes.
        row, blocked = self.checked(entry, rid, "retry")
        blocked = [b for b in blocked if b != "uncertain delivery requires staff review"]
        if row.get("state") != "PROCESSING" or row.get("email_status") != "SENDING" or row.get("message_id") != mid:
            raise SheetIntegrityError("Email intent could not be confirmed; no send attempted.")
        if blocked:
            self.save(row, state="REGISTRATION_REVIEW", email_status="FAILED", last_error="Approval or registration changed before SMTP; no send attempted.")
            return "blocked"
        try:
            self.mailer.send(row.get("normalized_email"), row.get("ticket_name"), pid, path, mid,
                             ticket_tier=row.get("ticket_tier"))
        except EmailNotSent:
            self.save(row, state="EMAIL_ERROR", email_status="FAILED", last_error="SMTP did not accept the message; explicit retry required.")
            return "email error"
        except Exception:
            self.save(row, state="EMAIL_REVIEW", email_status="AMBIGUOUS", last_error="Delivery uncertain; staff must review provider logs before retry.")
            return "review"
        self.save(row, state="SENT", email_status="SENT", email_sent_at=self.s.now())
        return "sent"

    def resolve_email(self, identifier, outcome):
        self.sheet.require_write()
        if outcome not in ("sent", "retry"):
            raise SheetIntegrityError("Resolve delivery as sent or retry after reviewing provider logs.")
        with file_lock(self.s.runtime / "worker.lock", blocking=False), file_lock(self.s.runtime / "sheets.lock"):
            rows = self.sheet.rows()
            row = select(rows, ids=[identifier])[0]
            uncertain = row.get("state") == "EMAIL_REVIEW" or (
                row.get("state") == "PROCESSING" and row.get("email_status") in ("SENDING", "AMBIGUOUS"))
            if not uncertain or not row.get("ticket_token"):
                raise SheetIntegrityError("Only an issued uncertain/interrupted email can be resolved after staff review.")
            if outcome == "sent":
                self.save(row, state="SENT", email_status="SENT", email_sent_at=self.s.now(),
                          last_error="Staff confirmed delivery; timestamp is review time.")
            else:
                blocked = [b for b in reasons(row, rows, self.s.event_id, "retry")
                           if b not in ("uncertain delivery requires staff review", "retry requires an existing recoverable job")]
                if blocked:
                    raise SheetIntegrityError("Approval/registration checks block recovery; create a private preview.")
                self.save(row, state="EMAIL_ERROR", email_status="FAILED",
                          last_error="Staff authorized retry after reviewing uncertain delivery; create a retry batch.")
