"""Clearly synthetic identities. These tokens are never accepted by a live service."""
import base64
import hashlib
from datetime import datetime, timezone

from ..production_sheets import APPROVAL_HEADERS


def token(label):
    return base64.urlsafe_b64encode(hashlib.sha256(("freshcoat-local-fixture:" + label).encode()).digest()).decode().rstrip("=")


def payload(event_id="freshcoat-synthetic"):
    sources = {"regular": [], "discount_15": [], "discount_10": [], "special": []}
    examples = [("lite", "regular", "Lite (₱199)", "Lite", "Alex Rivera", "alex.rivera@example.invalid"),
                ("regular", "discount_15", "Regular (₱246)", "Regular", "Alex Rivero", "alex.rivero@example.invalid"),
                ("pro", "regular", "Pro (₱349)", "Pro", "Morgan Santos", "morgan@example.invalid"),
                ("long", "discount_10", "Regular (₱261)", "Regular", "María Alexandra de los Santos Villanueva y Dela Cruz", "maria.alexandra.de.los.santos.villanueva.department.registration@example.invalid"),
                ("special", "special", "Special Ticket (₱180)", "Special Ticket", "Sam Bautista", "sam@example.invalid"),
                ("pass", "special", "Pass", "Pass", "Charlie Lim", "charlie@example.invalid"),
                ("arrived", "regular", "Regular", "Regular", "Jamie Reyes", "jamie@example.invalid"),
                ("cancelled", "regular", "Lite", "Lite", "Taylor Garcia", "cancelled@example.invalid"),
                ("invalid", "discount_15", "Pro", "Pro", "Robin Tan", "invalid@example.invalid"),
                ("unapproved", "discount_10", "Lite", "Lite", "Casey Cruz", "waiting@example.invalid"),
                ("controlled", "regular", "Lite", "Lite", "Controlled Test — Do Not Admit", "test@example.invalid"),
                ("duplicate", "regular", "Lite", "Lite", "Duplicate Registration", "duplicate@example.invalid")]
    prices = dict(regular="regular", discount_15="15_percent_discount", discount_10="10_percent_discount", special="special")
    for key, source, raw, tier, name, email in examples:
        row = dict(record_id="DEMO-R-" + key, participant_id="DEMO-P-" + key, source_id=source,
                   event_id=event_id, name=name, email=email, tier=raw, ticket_name=name,
                   normalized_email=email, ticket_tier=tier, price_category=prices[source], _price_category=prices[source],
                   ticket_token=token(key), state="SENT", email_status="SENT", render_status="RENDERED", ticket_status="ACTIVE")
        row.update({field: True for field in APPROVAL_HEADERS})
        if key == "arrived":
            row.update(checked_in_at="2026-11-23T08:30:00+08:00", checked_in_by="Demo opening shift", checkin_request_id="legacy-demo-arrived")
        if key == "cancelled": row["ticket_status"] = "CANCELLED"
        if key == "invalid": row["state"] = "INVALID"
        if key == "duplicate": row["state"] = "DUPLICATE"
        if key == "unapproved": row["confirmation"] = False
        if key == "controlled": row["ticket_token"] = "INVALID_TEST_" + "x" * 30
        sources[source].append(row)
    return {"synthetic": True, "event_id": event_id, "fixture_id": "web-synthetic-v1", "sources": sources}
