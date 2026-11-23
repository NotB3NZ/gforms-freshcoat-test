import json
import re
import time
import unicodedata
from datetime import datetime

from sqlalchemy import text

from .db import many, one
from .security import digest
from ..production import issued_identity, reasons
from ..production_sheets import validate_production_identities
from ..test_identity import is_test_token
from ..tiers import TICKET_TIERS

TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")
PUBLIC_COLUMNS = "p.participant_id,p.record_id,p.source_id,p.price_category,p.name,p.email,p.tier,p.eligible,p.reason,a.checked_in_at,a.checked_in_by,a.method"
JOIN = " FROM participants p LEFT JOIN attendance a ON a.event_id=p.event_id AND a.participant_id=p.participant_id "


class Conflict(ValueError):
    pass


class NotReady(ValueError):
    pass


def sheet_time(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed.timestamp()
    except (ValueError, TypeError):
        raise Conflict("Existing attendance timestamp needs timezone review.") from None


def eligibility(row, rows, event_id, synthetic=False):
    # Production selection reasons cover approvals, exact aliases, frozen fields,
    # shared-email approvals and special-recipient exceptions. Delivery is separate.
    blocked = [r for r in reasons(row, rows, event_id, action="admit")
               if r not in ("already sent; explicit resend required", "uncertain delivery requires staff review")]
    if row.get("event_id") != event_id:
        blocked.append("different or missing event")
    if row.get("state") in ("INVALID", "DUPLICATE", "CANCELLED") or row.get("duplicate_of"):
        blocked.append("invalid, duplicate or cancelled registration")
    if row.get("render_status") != "RENDERED":
        blocked.append("issued ticket has not been rendered")
    if not TOKEN.fullmatch(str(row.get("ticket_token"))) or is_test_token(row.get("ticket_token")):
        blocked.append("invalid or controlled-test ticket")
    if not synthetic and str(row.get("ticket_token")).startswith("DEMO_"):
        blocked.append("development ticket is not an admission credential")
    if row.get("ticket_tier") not in TICKET_TIERS:
        blocked.append("unrecognized issued tier")
    return not blocked, "; ".join(dict.fromkeys(blocked))


class AdmissionService:
    def __init__(self, db, settings):
        self.db, self.s = db, settings

    def participant(self, conn, participant_id):
        return one(conn, "SELECT " + PUBLIC_COLUMNS + JOIN +
                   "WHERE p.event_id=:e AND p.participant_id=:p", e=self.s.event_id, p=participant_id)

    def lookup(self, token):
        if not TOKEN.fullmatch(token) or is_test_token(token) or (not self.s.synthetic and token.startswith("DEMO_")):
            return {"status": "invalid", "message": "Malformed or controlled-test ticket. Use manual search for review."}
        with self.db.transaction() as conn:
            row = one(conn, "SELECT participant_id FROM participants WHERE event_id=:e AND token_digest=:t",
                      e=self.s.event_id, t=digest(token))
            if not row:
                return {"status": "unknown", "message": "Unknown ticket. Check registration sync or search by name/email."}
            return {"status": "found", "participant": self.participant(conn, row["participant_id"])}

    def confirm(self, participant_id, request_id, method, session):
        e = self.s.event_id
        # The database event row serializes short decisions across all processes.
        # No Google calls or local process/file locks occur in this transaction.
        with self.db.transaction(e) as conn:
            previous = one(conn, "SELECT * FROM requests WHERE event_id=:e AND request_id=:r", e=e, r=request_id)
            if previous:
                if (previous["participant_id"], previous["method"], previous["staff_session_id"]) != (participant_id, method, session["session_id"]):
                    raise Conflict("Request ID is bound to another participant, method or staff session.")
                return json.loads(previous["result"])
            current_session = one(conn, "SELECT * FROM staff_sessions WHERE session_id=:s", s=session["session_id"])
            if not current_session or current_session["revoked"] or current_session["expires_at"] <= time.time() or not current_session["nickname"]:
                raise NotReady("Staff session expired. Sign in to continue.")
            event = one(conn, "SELECT * FROM events WHERE event_id=:e", e=e)
            if not event or not event["admissions_open"]:
                raise NotReady("Admissions are closed. Complete the initial import and attendance reconciliation first.")
            p = self.participant(conn, participant_id)
            result = {"request_id": request_id, "participant": p}
            if not p:
                result["status"] = "invalid"
            elif p["checked_in_at"] is not None:
                result["status"] = "duplicate"
            elif not p["eligible"]:
                result["status"] = "invalid"
            else:
                timestamp = time.time()
                conn.execute(text("""INSERT INTO attendance VALUES(:e,:p,:t,:n,:s,:m,:r)"""),
                             {"e": e, "p": participant_id, "t": timestamp, "n": current_session["nickname"],
                              "s": session["session_id"], "m": method, "r": request_id})
                conn.execute(text("INSERT INTO outbox(event_id,participant_id,request_id) VALUES(:e,:p,:r)"),
                             {"e": e, "p": participant_id, "r": request_id})
                result.update(status="accepted", participant=self.participant(conn, participant_id))
            conn.execute(text("INSERT INTO requests VALUES(:e,:r,:p,:m,:s,:result)"),
                         {"e": e, "r": request_id, "p": participant_id, "m": method,
                          "s": session["session_id"], "result": json.dumps(result)})
        # Only return after commit, including its durable outbox and idempotency result.
        return result

    def import_rows(self, rows, *, dry_run=False, lease_owner=None):
        validate_production_identities(rows)
        selected = [r for r in rows if r.get("event_id") == self.s.event_id and r.get("record_id")]
        omitted = len(rows) - len(selected)
        prepared = []
        for row in selected:
            # Stable record IDs cover records still waiting for issuance. Their local
            # review key cannot collide with an issued participant namespace.
            pid = row.get("participant_id") or "unissued:" + row.source_id + ":" + row.get("record_id")
            valid, reason = eligibility(row, rows, self.s.event_id, self.s.synthetic)
            prepared.append((row, dict(e=self.s.event_id, p=pid, r=row.get("record_id"), s=row.source_id,
                                      c=row.get("price_category") or row.get("_price_category"),
                                      t=digest(row.get("ticket_token")) if row.get("ticket_token") else None,
                                      i=issued_identity(row), n=row.get("ticket_name") or row.get("name"),
                                      email=row.get("normalized_email") or row.get("email"),
                                      tier=row.get("ticket_tier") or "Unissued", ok=int(valid), reason=reason)))
        with self.db.transaction(self.s.event_id) as conn:
            if lease_owner:
                lease = one(conn, "SELECT * FROM sync_state WHERE event_id=:e", e=self.s.event_id)
                if lease["lease_owner"] != lease_owner or lease["lease_until"] < time.time():
                    raise Conflict("Sync lease expired; discard this snapshot and retry.")
            existing = many(conn, "SELECT * FROM participants WHERE event_id=:e", e=self.s.event_id)
            records = {data["r"] for _, data in prepared}
            if any(p["record_id"] not in records for p in existing):
                raise Conflict("Previously imported stable record disappeared; last snapshot retained for review.")
            by_record = {p["record_id"]: p for p in existing}
            # Validate every identity/attendance value before the first mutation.
            for row, data in prepared:
                previous = by_record.get(data["r"])
                if previous and previous["token_digest"] and previous["identity_digest"] != data["i"]:
                    raise Conflict("Frozen issued identity changed; last snapshot retained for review.")
                if previous and previous["source_id"] != data["s"]:
                    raise Conflict("Registration source changed; last snapshot retained for review.")
                if row.get("checked_in_at"):
                    stamp = sheet_time(row.get("checked_in_at"))
                    prior = one(conn, "SELECT * FROM attendance WHERE event_id=:e AND participant_id=:p", e=data["e"], p=data["p"])
                    if prior and (abs(prior["checked_in_at"] - stamp) > 0.001 or prior["checked_in_by"] != (row.get("checked_in_by") or "Legacy staff (unspecified)")):
                        raise Conflict("Sheet attendance conflicts with database attendance; reconciliation required.")
                    if event_open(conn, data["e"]) and not prior:
                        raise Conflict("New legacy attendance appeared after cutover; stop the legacy writer and reconcile.")
            if dry_run:
                return {"registrations": len(prepared), "eligible": sum(d["ok"] for _, d in prepared),
                        "existing_attendance": sum(bool(r.get("checked_in_at")) for r, _ in prepared), "omitted_without_event_or_stable_id": omitted}
            for row, data in prepared:
                previous = by_record.get(data["r"])
                if previous and previous["participant_id"] != data["p"]:
                    if previous["token_digest"]:
                        raise Conflict("Issued participant ID changed.")
                    conn.execute(text("DELETE FROM participants WHERE event_id=:e AND participant_id=:p"), {"e": data["e"], "p": previous["participant_id"]})
                data["search"] = unicodedata.normalize("NFKC", data["n"] + " " + data["email"]).casefold()
                conn.execute(text("""INSERT INTO participants(event_id,participant_id,record_id,source_id,price_category,token_digest,identity_digest,name,email,tier,eligible,reason,search_text) VALUES(:e,:p,:r,:s,:c,:t,:i,:n,:email,:tier,:ok,:reason,:search)
                  ON CONFLICT(event_id,participant_id) DO UPDATE SET token_digest=excluded.token_digest,
                  identity_digest=excluded.identity_digest,name=excluded.name,email=excluded.email,tier=excluded.tier,
                  eligible=excluded.eligible,reason=excluded.reason,search_text=excluded.search_text"""), data)
                if row.get("checked_in_at"):
                    conn.execute(text("""INSERT INTO attendance VALUES(:e,:p,:t,:n,NULL,'legacy',:r)
                      ON CONFLICT(event_id,participant_id) DO NOTHING"""),
                                 {"e": data["e"], "p": data["p"], "t": sheet_time(row.get("checked_in_at")),
                                  "n": row.get("checked_in_by") or "Legacy staff (unspecified)", "r": row.get("checkin_request_id") or "legacy:" + data["r"]})
            conn.execute(text("UPDATE sync_state SET last_success=:t,error='',omitted=:o WHERE event_id=:e"),
                         {"e": self.s.event_id, "t": time.time(), "o": omitted})
        return {"registrations": len(prepared), "omitted_without_event_or_stable_id": omitted}

    def open_admissions(self):
        with self.db.transaction(self.s.event_id) as conn:
            state = one(conn, "SELECT * FROM sync_state WHERE event_id=:e", e=self.s.event_id)
            if not state["last_success"] or state["error"]:
                raise NotReady("A successful validated import is required before opening admissions.")
            conn.execute(text("UPDATE events SET admissions_open=1 WHERE event_id=:e"), {"e": self.s.event_id})

    def list_participants(self, query="", tier="", attendance="", offset=0, limit=25):
        clauses = ["p.event_id=:e"]
        params = dict(e=self.s.event_id, q="%" + unicodedata.normalize("NFKC", query).casefold().replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%", tier=tier, offset=offset, limit=limit)
        if query:
            clauses.append("p.search_text LIKE :q ESCAPE '!'")
        if tier:
            clauses.append("p.tier=:tier")
        if attendance == "arrived":
            clauses.append("a.checked_in_at IS NOT NULL")
        elif attendance == "waiting":
            clauses.append("a.checked_in_at IS NULL AND p.eligible=1")
        elif attendance == "ineligible":
            clauses.append("p.eligible=0")
        where = "WHERE " + " AND ".join(clauses)
        with self.db.transaction() as conn:
            total = one(conn, "SELECT COUNT(*) AS n" + JOIN + where, **params)["n"]
            items = many(conn, "SELECT " + PUBLIC_COLUMNS + JOIN + where + " ORDER BY LOWER(p.name),p.participant_id LIMIT :limit OFFSET :offset", **params)
            return dict(items=items, total=total, offset=offset, limit=limit)

    def dashboard(self):
        with self.db.transaction() as conn:
            params = {"e": self.s.event_id}
            counts = one(conn, """SELECT COUNT(*) AS eligible,
              COALESCE(SUM(CASE WHEN a.checked_in_at IS NOT NULL THEN 1 ELSE 0 END),0) AS arrived""" + JOIN +
                         "WHERE p.event_id=:e AND p.eligible=1", **params)
            counts["waiting"] = counts["eligible"] - counts["arrived"]
            activity = many(conn, "SELECT " + PUBLIC_COLUMNS + JOIN +
                            "WHERE p.event_id=:e AND a.checked_in_at IS NOT NULL ORDER BY a.checked_in_at DESC LIMIT 30", **params)
            state = one(conn, "SELECT last_success,last_attempt,error,export_error,omitted,lease_until FROM sync_state WHERE event_id=:e", **params)
            state["pending_exports"] = one(conn, "SELECT COUNT(*) AS n FROM outbox WHERE event_id=:e AND delivered_at IS NULL", **params)["n"]
            return dict(counts=counts, activity=activity, sync=state, admissions_open=event_open(conn, self.s.event_id),
                        timezone=self.s.timezone, event_id=self.s.event_id, synthetic=self.s.synthetic, server_time=time.time(), tiers=TICKET_TIERS)


def event_open(conn, event_id):
    return bool(one(conn, "SELECT admissions_open FROM events WHERE event_id=:e", e=event_id)["admissions_open"])
