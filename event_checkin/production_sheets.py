"""Production schema adapter. Reads and writes have separate authorization boundaries.

Only the mapped name/email/tier, staff approval cells and app metadata are read.
Uploaded documents are never fetched. Initializing app headers never touches responses.
"""
import re
import time
from copy import deepcopy

from .config import METADATA, ConfigurationError, production_sources
from .sheets import Row, SheetIntegrityError, validate_identities
from .diagnostics import diagnostic_context

APPROVAL_HEADERS = {
    "confirmation": "Confirmation",
    "reference_matched": "Reference Number Matched?",
    "price_matched": "Payment price match the price? (Discount/Regular)",
    "payment_received": "Confirm if payment is received in our accont",
}
APPROVAL_COLUMNS = {
    "regular": ("A", "U", "V", "W"),
    "discount_15": ("A", "S", "T", "U"),
    "discount_10": ("A", "S", "T", "U"),
}
PRODUCTION_METADATA = METADATA + ("record_id", "source_id", "price_category", "ticket_tier")
# Explicitly reviewed row-one duplicates. A source must also opt into this exact
# policy in its private manifest and approved snapshot; legacy sources stay strict.
REVIEWED_DUPLICATE_HEADERS = {
    "regular": {"Email Address": ["C", "E"]},
    "discount_15": {"Email Address": ["C", "F"]},
    "discount_10": {"Email Address": ["C", "F"]},
}


class PacedWorksheet:
    """Share a conservative request clock across the three Sheets (one host)."""
    def __init__(self, worksheet, pace):
        self.worksheet, self.pace = worksheet, pace

    def __getattr__(self, name):
        value = getattr(self.worksheet, name)
        if not callable(value):
            return value
        def request(*args, **kwargs):
            self.pace()
            return value(*args, **kwargs)
        return request


def column_number(column):
    if not isinstance(column, str) or not re.fullmatch(r"[A-Z]+", column):
        raise SheetIntegrityError("Invalid mapped column; review the source manifest.")
    number = 0
    for char in column:
        number = number * 26 + ord(char) - ord("A") + 1
    return number


def source_mapping(source):
    if source["id"] not in APPROVAL_COLUMNS or source["approvals"] != APPROVAL_HEADERS:
        raise SheetIntegrityError("Unreviewed production source or approval mapping; manual schema review required.")
    mapping = {key: (value["header"], value["column"]) for key, value in source["fields"].items()}
    mapping.update({key: (label, col) for (key, label), col in
                    zip(APPROVAL_HEADERS.items(), APPROVAL_COLUMNS[source["id"]])})
    if len({label for label, _ in mapping.values()}) != len(mapping):
        raise SheetIntegrityError("Source field and approval headers overlap.")
    if len({col for _, col in mapping.values()}) != len(mapping):
        raise SheetIntegrityError("Source field and approval columns overlap.")
    for _, col in mapping.values():
        column_number(col)
    if "approved_duplicate_headers" in source:
        if (source["approved_duplicate_headers"] != REVIEWED_DUPLICATE_HEADERS[source["id"]]
                or mapping.get("email") != ("Email Address", "C")):
            raise SheetIntegrityError("Unreviewed duplicate-header exception or recipient column; schema review required.")
    return mapping


class ProductionTable:
    def __init__(self, settings, source, worksheet):
        self.settings, self.source, self.ws = settings, source, worksheet
        self.mapping = source_mapping(source)

    def validate_header(self, header, require_metadata=False):
        nonempty = [h for h in header if h]
        approved_duplicates = self.source.get("approved_duplicate_headers", {})
        for label, columns in approved_duplicates.items():
            positions = [index + 1 for index, value in enumerate(header) if value == label]
            if positions != [column_number(col) for col in columns]:
                raise SheetIntegrityError("Reviewed duplicate header moved or missing; schema review required.")
        for label in set(nonempty):
            if nonempty.count(label) > 1 and label not in approved_duplicates:
                raise SheetIntegrityError("Duplicate production headers; schema review required.")
        for label, col in self.mapping.values():
            index = column_number(col) - 1
            if index >= len(header) or header[index] != label:
                raise SheetIntegrityError("Production field/approval header moved or missing in source " + self.source["id"] + "; schema review required.")
        if any(label in nonempty for label in PRODUCTION_METADATA if label in [v[0] for v in self.mapping.values()]):
            raise SheetIntegrityError("App-owned and staff/form headers overlap.")
        if require_metadata and any(k not in header for k in PRODUCTION_METADATA):
            raise SheetIntegrityError("Production tracking headers are missing; reviewed initialization is required.")
        if approved_duplicates and (require_metadata or any(k in header for k in PRODUCTION_METADATA)):
            start = max(column_number(col) for col in APPROVAL_COLUMNS[self.source["id"]])
            if header[start:start + len(PRODUCTION_METADATA)] != list(PRODUCTION_METADATA):
                raise SheetIntegrityError("Production tracking headers moved or missing from approved positions.")
        return header

    def header(self, require_metadata=False):
        return self.validate_header(self.ws.row_values(1), require_metadata)

    def schema_plan(self):
        header = self.header()
        return {"source": self.source["id"], "existing_columns": len(header),
                "append": [key for key in PRODUCTION_METADATA if key not in header]}

    def initialize(self):
        self.settings.require_operational()  # Never remove this to bypass the checkpoint.
        from gspread.utils import rowcol_to_a1
        plan = self.schema_plan()
        if plan["append"]:
            total = plan["existing_columns"] + len(plan["append"])
            if total > self.ws.col_count:
                self.ws.add_cols(total - self.ws.col_count)
            self.ws.update(range_name=rowcol_to_a1(1, plan["existing_columns"] + 1),
                           values=[plan["append"]], value_input_option="RAW")

    def rows(self, require_metadata=False):
        from gspread.utils import rowcol_to_a1
        header = self.header(require_metadata)
        # One batch read of only the reviewed columns; no ID/proof-file downloads.
        positions = {key: column_number(col) for key, (_, col) in self.mapping.items()}
        positions.update({key: header.index(key) + 1 for key in PRODUCTION_METADATA if key in header})
        keys = list(positions)
        ranges = [rowcol_to_a1(2, positions[key]) + ":" + rowcol_to_a1(self.ws.row_count, positions[key]) for key in keys]
        columns = self.ws.batch_get(ranges, value_render_option="UNFORMATTED_VALUE")
        if len(columns) != len(keys):
            raise SheetIntegrityError("Incomplete production read; no approval can be assumed.")
        if self.header(require_metadata) != header:
            raise SheetIntegrityError("Production headers changed during the read; retry after schema review.")
        height = max((len(col) for col in columns), default=0)
        rows = []
        for index in range(height):
            values = {key: (col[index][0] if index < len(col) and col[index] else "") for key, col in zip(keys, columns)}
            if not any(value != "" for value in values.values()):
                continue
            if values.get("source_id") not in (None, "", self.source["id"]):
                raise SheetIntegrityError("Stored source identity conflicts with its physical Sheet.")
            values["_price_category"] = self.source["price_category"]
            rows.append(Row(index + 2, values, self.source["id"]))
        return rows

    def update(self, row, changes):
        self.settings.require_operational()
        from gspread.utils import rowcol_to_a1
        if not changes or set(changes) - set(PRODUCTION_METADATA):
            raise SheetIntegrityError("Production updates may only change reviewed app-owned metadata.")
        current = self.rows(require_metadata=True)
        if row.get("record_id"):
            matches = [r for r in current if r.get("record_id") == row.get("record_id")]
        else:
            matches = [r for r in current if r.number == row.number and r.data == row.data]
        if len(matches) != 1 or matches[0].data != row.data:
            raise SheetIntegrityError("Registration changed during the update; no write attempted.")
        target = matches[0]
        header = self.header(require_metadata=True)
        updates = [{"range": rowcol_to_a1(target.number, header.index(key) + 1), "values": [[value]]}
                   for key, value in changes.items()]
        self.ws.batch_update(updates, value_input_option="RAW")
        row.data.update(changes)


class ProductionSheets:
    live = True

    def __init__(self, settings, writable=False):
        import gspread
        self.settings = settings
        self.writable = writable
        if writable:
            settings.require_operational()
        sources = production_sources(settings)
        from .production import digest
        self.dataset = digest(sources)
        if {s["id"] for s in sources} != set(APPROVAL_COLUMNS):
            raise ConfigurationError("Production requires all three reviewed sources.")
        scope = "https://www.googleapis.com/auth/spreadsheets" + ("" if writable else ".readonly")
        client = gspread.service_account(filename=str(settings.service_account), scopes=[scope])
        client.set_timeout((10, 30))
        self.tables = {}
        self.last_request = 0.0
        def pace():
            delay = 1.1 - (time.monotonic() - self.last_request)
            if delay > 0:
                time.sleep(delay)
            self.last_request = time.monotonic()
        try:
            for source in sources:
                with diagnostic_context("open_production_source", source["id"]):
                    pace()
                    book = client.open_by_key(source["spreadsheet_id"])
                    pace()
                    ws = book.get_worksheet_by_id(source["tab_id"])
                    if ws.title != source["tab_name"]:
                        raise SheetIntegrityError("Production tab title/ID changed; schema review required.")
                    self.tables[source["id"]] = ProductionTable(settings, source, PacedWorksheet(ws, pace))
        except SheetIntegrityError:
            raise
        except Exception:
            raise SheetIntegrityError("Cannot access every production source; no batch can proceed.") from None

    def require_write(self):
        self.settings.require_operational()
        if not self.writable:
            raise ConfigurationError("This production adapter is read-only.")

    def schema_plan(self):
        return [table.schema_plan() for table in self.tables.values()]

    def initialize(self):
        self.require_write()
        from .locking import file_lock
        with file_lock(self.settings.runtime / "worker.lock", blocking=False), file_lock(self.settings.runtime / "sheets.lock"):
            self.schema_plan()  # Validate ALL headers before the first append.
            for table in self.tables.values():
                table.initialize()

    def rows(self):
        rows = []
        try:
            for table in self.tables.values():
                with diagnostic_context("read_production_source", table.source["id"]):
                    rows.extend(table.rows(require_metadata=self.writable))
        except SheetIntegrityError:
            raise
        except Exception:
            raise SheetIntegrityError("A production source could not be read; batch stopped closed.") from None
        validate_production_identities(rows)
        return rows

    def update(self, row, changes):
        self.require_write()
        self.tables[row.source_id].update(row, changes)


def validate_production_identities(rows):
    validate_identities(rows)
    records = [r.get("record_id") for r in rows if r.get("record_id")]
    if len(records) != len(set(records)):
        raise SheetIntegrityError("Duplicate permanent registration IDs; manual review required.")
    for row in rows:
        if row.get("source_id") not in ("", row.source_id):
            raise SheetIntegrityError("Stored source identity conflicts with the registration source.")
        if row.get("ticket_token") and not all(row.get(k) for k in ("participant_id", "record_id", "normalized_email", "ticket_name", "ticket_tier", "source_id", "price_category")):
            raise SheetIntegrityError("Incomplete issued production identity; manual review required.")
        if row.get("participant_id") and not row.get("ticket_token"):
            raise SheetIntegrityError("Partial production ticket identity; manual review required.")


class FixtureSheets:
    """Explicitly synthetic, local-only integration/demo store. Never creates a Google client."""
    live = False

    def __init__(self, payload):
        if payload.get("synthetic") is not True or set(payload.get("sources", {})) != set(APPROVAL_COLUMNS):
            raise ConfigurationError("Use a synthetic fixture containing all three sources.")
        self.data = deepcopy(payload)
        self.dataset = payload.get("fixture_id", "synthetic-default")

    def require_write(self):
        pass

    def rows(self):
        rows = []
        for source, records in self.data["sources"].items():
            for index, data in enumerate(records, 2):
                rows.append(Row(index, deepcopy(data), source))
        validate_production_identities(rows)
        return rows

    def update(self, row, changes):
        if not changes or set(changes) - set(PRODUCTION_METADATA):
            raise SheetIntegrityError("Only app-owned metadata may be updated.")
        matches = [r for r in self.rows() if r.source_id == row.source_id and
                   (r.get("record_id") == row.get("record_id") if row.get("record_id") else r.number == row.number)]
        if len(matches) != 1 or matches[0].data != row.data:
            raise SheetIntegrityError("Fixture changed before update.")
        self.data["sources"][row.source_id][matches[0].number - 2].update(deepcopy(changes))
        row.data.update(changes)
