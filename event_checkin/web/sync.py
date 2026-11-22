import secrets
import threading
import time

from sqlalchemy import text

from .db import many, one


class Synchronizer:
    def __init__(self, service, sheets):
        self.service, self.db, self.s, self.sheets = service, service.db, service.s, sheets

    def run_once(self, *, force=False):
        now = time.time()
        owner = secrets.token_hex(16)
        with self.db.transaction() as conn:
            state = one(conn, "SELECT * FROM sync_state WHERE event_id=:e", e=self.s.event_id)
            if state["lease_until"] > now:
                return {"status": "busy"}
            due = force or state["requested"] or not state["last_attempt"] or now - state["last_attempt"] >= self.s.sync_seconds
            if not due:
                return {"status": "waiting"}
            acquired = conn.execute(text("""UPDATE sync_state SET lease_owner=:o,lease_until=:until,last_attempt=:t,requested=0
              WHERE event_id=:e AND lease_until<=:t"""),
                                    {"e": self.s.event_id, "o": owner, "until": now + 600, "t": now}).rowcount
            if not acquired:
                return {"status": "busy"}
        # No database locks held during external I/O. Admissions remain independent.
        try:
            rows = self.sheets.rows()
            self.service.import_rows(rows, lease_owner=owner)
        except Exception:
            with self.db.transaction() as conn:
                conn.execute(text("UPDATE sync_state SET error=:error WHERE event_id=:e AND lease_owner=:o"),
                             {"e": self.s.event_id, "o": owner,
                              "error": "Registration sync failed validation or could not read every source. Last valid snapshot retained; review source identities and connection."})
            self.release(owner)
            return {"status": "error"}
        try:
            self.export(owner)
        finally:
            self.release(owner)
        return {"status": "complete"}

    def release(self, owner):
        with self.db.transaction() as conn:
            conn.execute(text("UPDATE sync_state SET lease_owner='',lease_until=0 WHERE event_id=:e AND lease_owner=:o"), {"e": self.s.event_id, "o": owner})

    def export(self, owner):
        with self.db.transaction() as conn:
            lease = one(conn, "SELECT * FROM sync_state WHERE event_id=:e", e=self.s.event_id)
            if lease["lease_owner"] != owner or lease["lease_until"] < time.time():
                return
            items = many(conn, """SELECT o.event_id,o.participant_id,o.request_id,o.attempts,
              p.source_id,p.record_id,p.identity_digest,a.checked_in_at,a.checked_in_by
              FROM outbox o JOIN participants p ON p.event_id=o.event_id AND p.participant_id=o.participant_id
              JOIN attendance a ON a.event_id=o.event_id AND a.participant_id=o.participant_id
              WHERE o.event_id=:e AND o.delivered_at IS NULL AND o.next_attempt<=:t
              ORDER BY a.checked_in_at LIMIT 100""", e=self.s.event_id, t=time.time())
        if not items:
            return
        error = False
        try:
            self.sheets.export_batch(items)
        except Exception:
            error = True
        with self.db.transaction() as conn:
            for item in items:
                params = {"e": item["event_id"], "p": item["participant_id"], "t": time.time(),
                          "next": time.time() + min(900, 2 ** min(item["attempts"] + 1, 10))}
                conn.execute(text("UPDATE outbox SET attempts=attempts+1," +
                                  ("next_attempt=:next" if error else "delivered_at=:t") +
                                  " WHERE event_id=:e AND participant_id=:p"), params)
            conn.execute(text("UPDATE sync_state SET export_error=:error WHERE event_id=:e"),
                         {"e": self.s.event_id, "error": "Attendance export failed; saved check-ins remain safe and will retry with backoff." if error else ""})

    def request(self):
        with self.db.transaction() as conn:
            conn.execute(text("UPDATE sync_state SET requested=1 WHERE event_id=:e"), {"e": self.s.event_id})

    def start(self):
        stop = threading.Event()
        def loop():
            while not stop.is_set():
                try:
                    self.run_once()
                except Exception:
                    # Deliberately omit exception messages: Google errors can contain data.
                    pass
                stop.wait(2)
        thread = threading.Thread(target=loop, name="registration-sync", daemon=True)
        thread.start()
        return stop, thread
