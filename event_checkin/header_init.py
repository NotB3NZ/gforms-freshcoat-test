"""Approved production row-one initialization, independent of participant adapters.

No participant-range read or general write interface is exposed. Each spreadsheet
uses one atomic batch with only a necessary column-count increase and exact header
userEnteredValue updates. Other cell fields (formatting, validation, notes) survive.
"""
import json
import time
from pathlib import Path

from .config import ConfigurationError, production_sources
from .locking import file_lock
from .production_sheets import PRODUCTION_METADATA, ProductionTable
from .sheets import SheetIntegrityError

EVENT_ID = "aws-scd-mindanao-2026"
APPROVED_RANGES = {"regular": (23, "X1:AU1"), "discount_15": (21, "V1:AS1"), "discount_10": (21, "V1:AS1")}


def require_checkpoint_settings(settings):
    if settings.profile != "production" or settings.event_id != EVENT_ID or settings.email_enabled:
        raise ConfigurationError("Header initialization requires the approved production event and EMAIL_ENABLED=false.")
    try:
        settings.require_operational()
    except ConfigurationError:
        return
    raise ConfigurationError("Participant operations must remain locked during header initialization.")


def load_plan(settings, path):
    require_checkpoint_settings(settings)
    try:
        plan = json.loads(Path(path).read_text())
        if plan["version"] != 1 or plan["event_id"] != EVENT_ID or plan["headers"] != list(PRODUCTION_METADATA):
            raise ValueError()
        entries = plan["sources"]
        if len(entries) != 3 or {e["source"]["id"] for e in entries} != set(APPROVED_RANGES):
            raise ValueError()
        configured = {s["id"]: s for s in production_sources(settings)}
        if set(configured) != set(APPROVED_RANGES):
            raise ValueError()
        for entry in entries:
            source = entry["source"]
            count, span = APPROVED_RANGES[source["id"]]
            if configured[source["id"]] != source or entry["range"] != span or len(entry["original_headers"]) != count:
                raise ValueError()
            ProductionTable(settings, source, None).validate_header(entry["original_headers"])
            if set(entry["original_headers"]) & set(PRODUCTION_METADATA):
                raise ValueError()
        return plan
    except (OSError, ValueError, KeyError, TypeError, SheetIntegrityError):
        raise ConfigurationError("Approved header plan is missing/invalid or configured sources changed. Stop for review; no Google writes attempted.") from None


def classify(entry, snapshot):
    source = entry["source"]
    if snapshot["title"] != source["tab_name"] or snapshot["tab_id"] != source["tab_id"]:
        raise SheetIntegrityError("Approved tab title/ID changed in " + source["id"] + "; initialization stopped.")
    original = entry["original_headers"]
    header = list(snapshot["headers"])
    while header and header[-1] == "":
        header.pop()
    if header[:len(original)] != original:
        raise SheetIntegrityError("Original headers differ from the approved snapshot in " + source["id"] + "; initialization stopped.")
    ProductionTable(None, source, None).validate_header(header)
    if header == original:
        state = "pending"
    elif header == original + list(PRODUCTION_METADATA):
        state = "initialized"
    else:
        raise SheetIntegrityError("Header destination contains partial, different or extra content in " + source["id"] + "; initialization stopped.")
    if type(snapshot["column_count"]) is not int or snapshot["column_count"] < len(header):
        raise SheetIntegrityError("Inconsistent grid metadata; initialization stopped.")
    return state


def approved_requests(entry, column_count):
    """The only legal write payload; fixed rows, columns, strings and field masks."""
    start = len(entry["original_headers"])
    end = start + len(PRODUCTION_METADATA)
    sid = entry["source"]["tab_id"]
    requests = []
    if column_count < end:
        requests.append({"updateSheetProperties": {
            "properties": {"sheetId": sid, "gridProperties": {"columnCount": end}},
            "fields": "gridProperties.columnCount"}})
    requests.append({"updateCells": {
        "range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1,
                  "startColumnIndex": start, "endColumnIndex": end},
        "rows": [{"values": [{"userEnteredValue": {"stringValue": name}} for name in PRODUCTION_METADATA]}],
        "fields": "userEnteredValue"}})
    return requests


class HeaderTransport:
    """Authentication is used only here, with no participant adapter or SMTP instance."""
    def __init__(self, settings, plan, apply=False):
        import gspread
        self.plan, self.apply = plan, apply
        scope = "https://www.googleapis.com/auth/spreadsheets" + ("" if apply else ".readonly")
        client = gspread.service_account(filename=str(settings.service_account), scopes=[scope])
        client.set_timeout((10, 30))
        self.http = client.http_client
        self.last_request = 0.0
        self.reads = self.writes = 0

    def pace(self):
        delay = 1.1 - (time.monotonic() - self.last_request)
        if delay > 0:
            time.sleep(delay)
        self.last_request = time.monotonic()

    def check_entry(self, entry):
        if entry not in self.plan["sources"]:
            raise SheetIntegrityError("Attempt to access a source outside the approved header plan.")

    def read(self, entry):
        self.check_entry(entry)
        source = entry["source"]
        self.pace()
        metadata = self.http.fetch_sheet_metadata(source["spreadsheet_id"], params={
            "includeGridData": "false",
            "fields": "spreadsheetId,sheets(properties(sheetId,title,gridProperties(columnCount)))"})
        self.reads += 1
        tabs = [s["properties"] for s in metadata.get("sheets", []) if s["properties"]["sheetId"] == source["tab_id"]]
        if metadata.get("spreadsheetId") != source["spreadsheet_id"] or len(tabs) != 1 or tabs[0]["title"] != source["tab_name"]:
            raise SheetIntegrityError("Approved spreadsheet/tab changed or is unavailable; initialization stopped.")
        self.pace()
        # FORMULA detects a destination formula even if its displayed result is blank.
        span = "'" + source["tab_name"].replace("'", "''") + "'!1:1"
        values = self.http.values_get(source["spreadsheet_id"], span, params={"valueRenderOption": "FORMULA"})
        self.reads += 1
        rows = values.get("values", [])
        if len(rows) > 1:
            raise SheetIntegrityError("Unexpected row-one response; initialization stopped.")
        return {"title": tabs[0]["title"], "tab_id": tabs[0]["sheetId"],
                "column_count": tabs[0]["gridProperties"]["columnCount"], "headers": rows[0] if rows else []}

    def write(self, entry, snapshot):
        self.check_entry(entry)
        if not self.apply or classify(entry, snapshot) != "pending":
            raise SheetIntegrityError("Header writes require an approved blank destination and apply mode.")
        body = {"requests": approved_requests(entry, snapshot["column_count"])}
        self.pace()
        self.http.batch_update(entry["source"]["spreadsheet_id"], body)
        self.writes += 1


class HeaderInitializer:
    def __init__(self, settings, plan, transport):
        self.settings, self.plan, self.transport = settings, plan, transport

    def validate_all(self):
        snapshots = {}
        for entry in self.plan["sources"]:
            snapshot = self.transport.read(entry)
            classify(entry, snapshot)
            snapshots[entry["source"]["id"]] = snapshot
        return snapshots

    def run(self, apply=False):
        require_checkpoint_settings(self.settings)
        with file_lock(self.settings.runtime / "worker.lock", blocking=False), file_lock(self.settings.runtime / "sheets.lock"):
            initial = self.validate_all()  # All sources checked before ANY mutation.
            before = {key: value["column_count"] for key, value in initial.items()}
            written = set()
            if apply:
                for entry in self.plan["sources"]:
                    # Revalidate every source immediately before each spreadsheet batch.
                    current = self.validate_all()
                    key = entry["source"]["id"]
                    # Detect any change since the previous check, apart from our own completed appends.
                    for other in self.plan["sources"]:
                        other_key = other["source"]["id"]
                        expected = "initialized" if other_key in written else classify(other, initial[other_key])
                        if classify(other, current[other_key]) != expected:
                            raise SheetIntegrityError("Headers changed during initialization; stopped before the next write.")
                        expected_columns = max(before[other_key], len(other["original_headers"]) + 24) if other_key in written else before[other_key]
                        if current[other_key]["column_count"] != expected_columns:
                            raise SheetIntegrityError("Grid changed during initialization; stopped before the next write.")
                    snapshot = current[key]
                    if classify(entry, snapshot) == "initialized":
                        continue
                    # A final check of the write target narrows the non-transactional read/write gap.
                    last = self.transport.read(entry)
                    if last != snapshot:
                        raise SheetIntegrityError("Destination changed immediately before write; initialization stopped.")
                    self.transport.write(entry, last)
                    verified = self.transport.read(entry)
                    expected_columns = max(last["column_count"], len(entry["original_headers"]) + 24)
                    if classify(entry, verified) != "initialized" or verified["column_count"] != expected_columns:
                        raise SheetIntegrityError("Header write could not be verified; stopped. Re-run only this command to reconcile.")
                    written.add(key)
            final = self.validate_all()
            for entry in self.plan["sources"]:
                key = entry["source"]["id"]
                expected_columns = max(before[key], len(entry["original_headers"]) + 24) if key in written else before[key]
                if final[key]["column_count"] != expected_columns:
                    raise SheetIntegrityError("Grid changed during final verification; stop for review.")
            return [{"source": entry["source"]["id"], "tab": entry["source"]["tab_name"],
                     "tab_id": entry["source"]["tab_id"], "range": entry["range"],
                     "status": "written and verified" if entry["source"]["id"] in written else (
                         "already initialized; unchanged" if classify(entry, final[entry["source"]["id"]]) == "initialized" else "validated; no writes"),
                     "headers": list(PRODUCTION_METADATA),
                     "columns_before": before[entry["source"]["id"]],
                     "columns_after": final[entry["source"]["id"]]["column_count"],
                     "grid_columns_added": final[entry["source"]["id"]]["column_count"] - before[entry["source"]["id"]]}
                    for entry in self.plan["sources"]]


def run_command(settings, args):
    path = args.plan or settings.root / "config/production-header-init.approved.json"
    plan = load_plan(settings, path)
    transport = HeaderTransport(settings, plan, apply=args.apply)
    try:
        report = HeaderInitializer(settings, plan, transport).run(apply=args.apply)
    except (ConfigurationError, SheetIntegrityError):
        raise
    except Exception:
        raise SheetIntegrityError("Header initialization could not be fully confirmed; stopped. A request may have committed. Re-run only this header command to reconcile; participant operations remain locked.") from None
    print(json.dumps({"event_id": settings.event_id, "email_enabled": settings.email_enabled,
                      "participant_operations": "LOCKED", "participant_rows_read": 0,
                      "sources": report}, indent=2))
