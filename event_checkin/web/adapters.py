"""Attendance-only adapters. No issuance, rendering, header creation or SMTP."""
import json
import os
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from ..production_sheets import FixtureSheets, ProductionSheets
from ..shared_email import apply_approved_groups
from ..config import Settings, load_settings
from ..production import issued_identity
from .service import Conflict, sheet_time

ATTENDANCE_FIELDS = {"checked_in_at", "checked_in_by", "checkin_request_id"}


def export_changes(item):
    return {"checked_in_at": datetime.fromtimestamp(item["checked_in_at"], timezone.utc).isoformat(),
            "checked_in_by": item["checked_in_by"], "checkin_request_id": item["request_id"]}


def match_export(rows, item):
    matches = [r for r in rows if r.source_id == item["source_id"] and
               r.get("record_id") == item["record_id"] and r.get("event_id") == item["event_id"] and
               r.get("participant_id") == item["participant_id"]]
    if len(matches) != 1 or issued_identity(matches[0]) != item["identity_digest"]:
        raise Conflict("Export identity changed; source record needs review.")
    row = matches[0]
    changes = export_changes(item)
    if row.get("checked_in_at") and (row.get("checkin_request_id") != item["request_id"] or
                                     row.get("checked_in_by") != item["checked_in_by"] or
                                     abs(sheet_time(row.get("checked_in_at")) - item["checked_in_at"]) > 0.001):
        raise Conflict("Existing Sheet attendance differs; export requires reconciliation.")
    return row, changes


class FakeSheets:
    """Persistent, atomically replaced fake Sheet. Failure flags are local fixtures."""
    live = False

    def __init__(self, settings):
        self.s = settings
        self.path = settings.fixture
        self.approval_settings = Settings(root=self.path.parent, runtime=self.path.parent / "approvals", event_id=settings.event_id)

    def read_payload(self):
        payload = json.loads(self.path.read_text())
        if payload.get("synthetic") is not True or payload.get("event_id") != self.s.event_id:
            raise Conflict("Only this event's explicitly synthetic fixture may be opened.")
        return payload

    def rows(self):
        payload = self.read_payload()
        if payload.get("fail_reads"):
            raise ConnectionError("Synthetic Sheets read outage")
        return apply_approved_groups(self.approval_settings, FixtureSheets(payload).rows())

    def export_batch(self, items):
        payload = self.read_payload()
        if payload.get("fail_exports"):
            raise ConnectionError("Synthetic Sheets export outage")
        fixture = FixtureSheets(payload)
        rows = fixture.rows()
        targets = [match_export(rows, item) for item in items]
        for row, changes in targets:
            # All targets are re-resolved by stable ID in this fresh atomic snapshot.
            fixture.data["sources"][row.source_id][row.number - 2].update(changes)
        self.write(fixture.data)

    def write(self, payload):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.path.with_suffix(".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as file:
            json.dump(payload, file, indent=2, ensure_ascii=False)
            file.flush()
            os.fsync(file.fileno())
        temporary.replace(self.path)


class LiveSheets:
    """Explicit future activation boundary, independent of the email authorizations.

    Only --live dry-run reads until a later operator configures all live gates.
    Never calls ProductionSheets.initialize/update or changes their safeguards.
    """
    live = True

    def __init__(self, settings):
        if settings.synthetic or not settings.live_config:
            raise ValueError("Live adapter requires explicit WEB_LIVE_CONFIG repository root.")
        self.s = settings
        self.legacy = load_settings(profile="production", root=Path(settings.live_config))
        if self.legacy.event_id != settings.event_id or self.legacy.email_enabled:
            raise ValueError("Live event must match; email must remain disabled in this sync configuration.")
        self.sheet = ProductionSheets(self.legacy, writable=False)
        self.writer_tables = None

    def rows(self):
        # Existing mapped source reads, duplicate-header exceptions, source categories,
        # special delivery overrides, and reviewed shared-email booking approvals.
        return apply_approved_groups(self.legacy, self.sheet.rows())

    def export_batch(self, items):
        if not self.s.live_export:
            raise ValueError("Live export is disabled. Explicit later cutover activation is required.")
        if self.writer_tables is None:
            import gspread
            from ..config import production_sources
            from ..production_sheets import ProductionTable
            client = gspread.service_account(filename=str(self.legacy.service_account),
                                             scopes=["https://www.googleapis.com/auth/spreadsheets"])
            client.set_timeout((10, 30))
            self.writer_tables = {}
            for source in production_sources(self.legacy):
                worksheet = client.open_by_key(source["spreadsheet_id"]).get_worksheet_by_id(source["tab_id"])
                if worksheet.title != source["tab_name"]:
                    raise Conflict("Live export tab identity changed.")
                self.writer_tables[source["id"]] = ProductionTable(self.legacy, source, worksheet)
        # Read and validate ALL sources before any write. Batch by physical worksheet.
        from ..production_sheets import validate_production_identities
        from gspread.utils import rowcol_to_a1
        rows = []
        headers = {}
        for source, table in self.writer_tables.items():
            headers[source] = table.header(require_metadata=True)
            rows.extend(table.rows(require_metadata=True))
        validate_production_identities(rows)
        batches = {}
        for item in items:
            row, changes = match_export(rows, item)
            header = headers[row.source_id]
            for field, value in changes.items():
                if field not in ATTENDANCE_FIELDS:
                    raise Conflict("Unknown attendance field.")
                batches.setdefault(row.source_id, []).append({"range": rowcol_to_a1(row.number, header.index(field) + 1), "values": [[value]]})
        for source, updates in batches.items():
            self.writer_tables[source].ws.batch_update(updates, value_input_option="RAW")
        # A response loss or partial multi-Sheet batch is replayed with identical values.
        # Google Sheets has no conditional stable-ID writes: freeze sorting/deletion
        # during operation; append-only form responses remain supported.


def adapter(settings):
    return FakeSheets(settings) if settings.synthetic else LiveSheets(settings)
