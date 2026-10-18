import hashlib
import json
import os
import sqlite3

from .config import private_directory
from .locking import file_lock
from .sheets import validate_identities, SheetIntegrityError


class RequestConflict(ValueError):
    pass


class CheckinService:
    def __init__(self, settings, sheet):
        self.s, self.sheet = settings, sheet
        private_directory(settings.runtime)
        self.db_path = settings.runtime / "checkin.sqlite3"
        fd = os.open(self.db_path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        self.db_path.chmod(0o600)
        with self.db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS requests (
                event_id TEXT NOT NULL, request_id TEXT NOT NULL, token_hash TEXT NOT NULL,
                intent TEXT, result TEXT, PRIMARY KEY(event_id, request_id))""")

    def db(self):
        # Connections stay within their own request thread. FULL sync is SQLite's default.
        from contextlib import closing
        return closing(sqlite3.connect(self.db_path, timeout=30, isolation_level=None))

    def check_in(self, token, request_id, station_name="Local staff station"):
        digest = hashlib.sha256(token.encode()).hexdigest()
        s = self.s
        # Includes the journal, fresh Sheet lookup, decision AND update. The local worker
        # uses this same lock for its metadata updates. No cached registration snapshot.
        with file_lock(s.runtime / "sheets.lock"), self.db() as db:
            previous = db.execute("SELECT token_hash,intent,result FROM requests WHERE event_id=? AND request_id=?",
                                  (s.event_id, request_id)).fetchone()
            if previous and previous[0] != digest:
                raise RequestConflict("Request ID was already used for a different ticket.")
            if previous and previous[2]:
                return json.loads(previous[2])
            if not previous:
                db.execute("INSERT INTO requests(event_id,request_id,token_hash) VALUES(?,?,?)", (s.event_id, request_id, digest))
            rows = self.sheet.rows()
            validate_identities(rows)
            matches = [r for r in rows if r.get("event_id") == s.event_id and r.get("ticket_token") == token]
            result = {"status": "invalid", "request_id": request_id}
            if len(matches) > 1:
                raise SheetIntegrityError("Ticket is not unique; attendance cannot be confirmed.")
            if matches:
                row = matches[0]
                result.update(participant_id=row.get("participant_id"), name=row.get("ticket_name"))
                # Blank status supports tickets saved before ticket_status was introduced.
                # Unknown statuses fail closed, as do explicit cancellation and invalid jobs.
                if (not row.get("participant_id") or row.get("state") in ("INVALID", "DUPLICATE", "CANCELLED")
                        or row.get("ticket_status") not in ("", "ACTIVE")):
                    pass
                elif row.get("checked_in_at"):
                    result.update(status="accepted" if row.get("checkin_request_id") == request_id else "duplicate",
                                  checked_in_at=row.get("checked_in_at"), checked_in_by=row.get("checked_in_by"))
                elif row.get("render_status") == "RENDERED":
                    # Fail closed if manual edits introduced two tickets for an email/event.
                    same_email = [r for r in rows if r.get("event_id") == s.event_id and
                                  r.get("normalized_email") == row.get("normalized_email") and
                                  r.get("ticket_token") and r.get("state") not in ("INVALID", "DUPLICATE")]
                    if not row.get("normalized_email") or len(same_email) != 1:
                        raise SheetIntegrityError("Email/event identity conflict; attendance cannot be confirmed.")
                    intent = json.loads(previous[1]) if previous and previous[1] else None
                    timestamp = intent["checked_in_at"] if intent else s.now()
                    station = intent.get("checked_in_by", station_name) if intent else station_name
                    result.update(status="accepted", checked_in_at=timestamp, checked_in_by=station)
                    # Persist the intended timestamp before writing Sheets. A lost response can
                    # be reconciled from checkin_request_id and this durable journal on retry.
                    db.execute("UPDATE requests SET intent=? WHERE event_id=? AND request_id=?",
                               (json.dumps(result), s.event_id, request_id))
                    self.sheet.update(row, {"checked_in_at": timestamp, "checkin_request_id": request_id,
                                            "checked_in_by": station})
                    # If the Sheet write raises, no acceptance response or final journal result
                    # is returned. The same request can safely retry and reconcile.
            db.execute("UPDATE requests SET result=? WHERE event_id=? AND request_id=?",
                       (json.dumps(result), s.event_id, request_id))
            return result
