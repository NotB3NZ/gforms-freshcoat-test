import re
import uuid
from pathlib import Path

from .locking import file_lock
from .issuance import IdentityJournal
from .mailer import EmailNotSent, EmailAmbiguous
from .sheets import by_id, normalize_email, validate_identities, SheetIntegrityError


class Worker:
    def __init__(self, settings, sheet, renderer, mailer):
        self.s, self.sheet, self.renderer, self.mailer = settings, sheet, renderer, mailer
        self.identities = IdentityJournal(settings)

    def lock(self):
        return file_lock(self.s.runtime / "sheets.lock")

    def save(self, row, **changes):
        changes["updated_at"] = self.s.now()
        self.sheet.update(row, changes)

    def prepare(self, locator):
        with self.lock():
            rows = self.sheet.rows()
            validate_identities(rows)
            row = next((r for r in rows if (r.get("participant_id") == locator.get("participant_id")
                       if locator.get("participant_id") else r.number == locator.number)), None)
            if row is None or row.get("event_id") != self.s.event_id:
                return None
            if row.get("state") not in ("PENDING", "PROCESSING"):
                return None
            if row.get("ticket_status") not in ("", "ACTIVE"):
                return None
            email = row.get("normalized_email") or normalize_email(row.get("email"))
            name = row.get("ticket_name") or row.get("name").strip()
            if not name or len(name) > 160 or not re.fullmatch(r"[^\s@<>\r\n]+@[^\s@<>\r\n]+\.[^\s@<>\r\n]+", email):
                self.save(row, state="INVALID", last_error="Missing/invalid name or email; correct the form response then retry.")
                return None
            # An already issued canonical ticket wins regardless of sorting. Otherwise the
            # earliest current response wins when initially assigning identities.
            same = [r for r in rows if r.get("event_id") == self.s.event_id and
                    (r.get("normalized_email") or normalize_email(r.get("email"))) == email and
                    r.get("state") not in ("DUPLICATE", "INVALID")]
            issued = [r for r in same if r.get("ticket_token")]
            if len(issued) > 1:
                raise SheetIntegrityError("Multiple tickets for one email/event; manual review required.")
            canonical = issued[0] if issued else same[0]
            pid = row.get("participant_id") or "P-" + str(uuid.uuid4())
            if canonical.number != row.number:
                self.save(row, participant_id=pid, normalized_email=email, ticket_name=name,
                          state="DUPLICATE", duplicate_of=canonical.get("participant_id"),
                          last_error="This email already has a registration for this event; use its original ticket.")
                return None
            # Partial identities indicate manual edits/corruption, not permission to mint a ticket.
            if bool(row.get("participant_id")) != bool(row.get("ticket_token")):
                raise SheetIntegrityError("Incomplete identity on a canonical registration; manual review required.")
            # Reserve durably by email/event before attempting Sheets. Even if a
            # timed-out request has not committed yet, retries use the same pair.
            pid, token = self.identities.reserve(self.s.event_id, email, row.get("participant_id"), row.get("ticket_token"))
            self.save(row, participant_id=pid, ticket_token=token, normalized_email=email, ticket_name=name,
                      state="PROCESSING", ticket_status="ACTIVE", last_error="")
            # Confirm persistence BEFORE producing a card or any external side effect.
            confirmed = by_id(self.sheet, pid, self.s.event_id)
            if confirmed.get("ticket_token") != token:
                raise SheetIntegrityError("Ticket identity could not be confirmed in Sheets.")
            return confirmed

    def process(self, row):
        row = self.prepare(row)
        if row is None:
            return
        pid = row.get("participant_id")
        # Any interrupted send is uncertain, including a crash before SMTP was called.
        if row.get("email_status") == "SENDING":
            with self.lock():
                row = by_id(self.sheet, pid, self.s.event_id)
                self.save(row, state="EMAIL_REVIEW", email_status="AMBIGUOUS", last_error="Interrupted email attempt; review provider logs before resending.")
            return
        if row.get("email_status") == "SENT":
            with self.lock():
                self.save(by_id(self.sheet, pid, self.s.event_id), state="SENT")
            return
        path = Path(row.get("card_path")) if row.get("card_path") else None
        # Only trust paths inside our private generated directory.
        valid_path = path and path.resolve().is_relative_to(self.s.generated.resolve()) and path.is_file()
        if row.get("render_status") != "RENDERED" or not valid_path:
            with self.lock():
                row = by_id(self.sheet, pid, self.s.event_id)
                self.save(row, render_status="RENDERING", render_attempts=str(int(row.get("render_attempts") or 0) + 1))
            try:
                path = self.renderer.render(pid, row.get("ticket_name"), row.get("ticket_token"))
            except Exception:
                with self.lock():
                    self.save(by_id(self.sheet, pid, self.s.event_id), state="RENDER_ERROR", render_status="FAILED",
                              last_error="Card rendering failed. Run preview/check-template, fix the cause, then retry.")
                return
            with self.lock():
                row = by_id(self.sheet, pid, self.s.event_id)
                self.save(row, render_status="RENDERED", card_path=str(path))
        if not self.s.email_enabled:
            with self.lock():
                self.save(by_id(self.sheet, pid, self.s.event_id), state="READY", email_status="DISABLED")
            return
        # Validate all email configuration before marking a potentially ambiguous send.
        self.s.require_email()
        with self.lock():
            row = by_id(self.sheet, pid, self.s.event_id)
            message_id = row.get("message_id") or "<" + str(uuid.uuid4()) + "@event-checkin.local>"
            self.save(row, email_status="SENDING", message_id=message_id,
                      email_attempts=str(int(row.get("email_attempts") or 0) + 1))
            confirmed = by_id(self.sheet, pid, self.s.event_id)
            if confirmed.get("email_status") != "SENDING" or confirmed.get("message_id") != message_id:
                raise SheetIntegrityError("Email intent could not be confirmed; no send attempted.")
        try:
            self.mailer.send(row.get("normalized_email"), row.get("ticket_name"), pid, path, message_id)
        except EmailNotSent:
            with self.lock():
                self.save(by_id(self.sheet, pid, self.s.event_id), state="EMAIL_ERROR", email_status="FAILED",
                          last_error="Email was not accepted by SMTP; fix the cause then retry.")
        except Exception:
            with self.lock():
                self.save(by_id(self.sheet, pid, self.s.event_id), state="EMAIL_REVIEW", email_status="AMBIGUOUS",
                          last_error="Email outcome uncertain; review provider logs before resolving.")
        else:
            with self.lock():
                self.save(by_id(self.sheet, pid, self.s.event_id), state="SENT", email_status="SENT", email_sent_at=self.s.now())

    def run_once(self):
        with file_lock(self.s.runtime / "worker.lock", blocking=False):
            with self.lock():
                pending = [r for r in self.sheet.rows() if r.get("event_id") == self.s.event_id
                           and r.get("state") in ("PENDING", "PROCESSING")
                           and r.get("ticket_status") in ("", "ACTIVE")][:self.s.worker_batch_size]
            for row in pending:
                # Uncertain Sheet writes stop the pass. The next run re-reads durable state.
                self.process(row)
            return len(pending)

    def retry(self, pid, resend=False, resolution=None):
        with file_lock(self.s.runtime / "worker.lock", blocking=False), self.lock():
            row = by_id(self.sheet, pid, self.s.event_id)
            if row.get("ticket_status") not in ("", "ACTIVE") or row.get("state") == "CANCELLED":
                raise SheetIntegrityError("Cancelled or unknown ticket status; retry/resend is disabled.")
            if resolution:
                if row.get("state") != "EMAIL_REVIEW":
                    raise ValueError("Only EMAIL_REVIEW can be resolved.")
                if resolution == "sent":
                    self.save(row, state="SENT", email_status="SENT", email_sent_at=self.s.now(), last_error="Staff confirmed SMTP delivery; timestamp is review time.")
                    return
                self.save(row, email_status="FAILED", state="PENDING", last_error="Staff authorized retry after delivery review.")
                return
            if row.get("state") in ("DUPLICATE", "EMAIL_REVIEW"):
                raise ValueError("Duplicate registrations cannot retry; uncertain email requires resolve-email.")
            if row.get("state") == "SENT" and not resend:
                raise ValueError("Already sent; use explicit resend if another delivery is intended.")
            if resend and row.get("state") != "SENT":
                raise ValueError("Resend requires a SENT registration.")
            changes = {"state": "PENDING", "last_error": ""}
            if resend:
                changes.update(email_status="RETRY", message_id="")
            self.save(row, **changes)

    def cancel(self, pid):
        # Excludes local worker jobs and serializes against an in-progress admission.
        # Preserve job/email/attendance fields and the original identity for audit/recovery.
        with file_lock(self.s.runtime / "worker.lock", blocking=False), self.lock():
            row = by_id(self.sheet, pid, self.s.event_id)
            if not row.get("ticket_token") or row.get("state") in ("DUPLICATE", "INVALID"):
                raise SheetIntegrityError("Only an issued canonical ticket can be cancelled.")
            self.save(row, ticket_status="CANCELLED")
