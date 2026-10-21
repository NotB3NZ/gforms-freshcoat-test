from dataclasses import dataclass
from .config import METADATA


class SheetIntegrityError(RuntimeError):
    pass


@dataclass
class Row:
    # Locator for the current snapshot only. Never used as a participant identity.
    number: int
    data: dict
    source_id: str = ""

    def get(self, key):
        return self.data.get(key, "")


def normalize_email(value):
    return value.strip().casefold()


def validate_identities(rows):
    for key in ("participant_id", "ticket_token"):
        values = [r.get(key) for r in rows if r.get(key)]
        if len(values) != len(set(values)):
            raise SheetIntegrityError("Duplicate permanent identity or ticket in Sheet; manual review required.")


class GoogleSheet:
    def __init__(self, settings):
        import gspread
        settings.require_sheet()
        self.settings = settings
        client = gspread.service_account(
            filename=str(settings.service_account),
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
        # Explicit IDs avoid Drive listing and use the credential only for authentication.
        client.set_timeout((10, 30))
        self.ws = client.open_by_key(settings.spreadsheet_id).worksheet(settings.tab_name)

    def _header(self):
        header = self.ws.row_values(1)
        nonempty = [v for v in header if v]
        if len(nonempty) != len(set(nonempty)):
            raise SheetIntegrityError("Duplicate Sheet header names; fix them before continuing.")
        return header

    def initialize(self):
        """Add metadata headers only. Never queue existing registrations implicitly."""
        from gspread.utils import rowcol_to_a1
        header = self._header()
        for key in ("name", "email"):
            if self.settings.headers[key] not in header:
                raise SheetIntegrityError("Configured form header missing; check NAME_HEADER and EMAIL_HEADER.")
        missing = [self.settings.headers[k] for k in METADATA if self.settings.headers[k] not in header]
        if missing:
            total = len(header) + len(missing)
            if total > self.ws.col_count:
                self.ws.add_cols(total - self.ws.col_count)
            self.ws.update(range_name=rowcol_to_a1(1, len(header) + 1), values=[missing], value_input_option="RAW")

    def rows(self):
        values = self.ws.get_all_values()
        if not values:
            raise SheetIntegrityError("Sheet has no header row.")
        header = values[0]
        self._last_header = header
        if len([h for h in header if h]) != len(set(h for h in header if h)):
            raise SheetIntegrityError("Duplicate Sheet headers.")
        if any(h not in header for h in self.settings.headers.values()):
            raise SheetIntegrityError("Missing configured headers; run init-sheet after checking form headers.")
        positions = {k: header.index(h) for k, h in self.settings.headers.items()}
        rows = [Row(i, {k: str(v[p]) if p < len(v) else "" for k, p in positions.items()})
                for i, v in enumerate(values[1:], 2) if any(v)]
        validate_identities(rows)
        return rows

    def update(self, row, changes):
        """Caller holds sheets.lock. Fresh participant lookup survives row reordering between jobs.

        Do not sort/delete rows while the system runs: Sheets offers no conditional cell update.
        Appended form responses are supported. Only specified metadata cells are written.
        """
        from gspread.utils import rowcol_to_a1
        if set(changes) - set(METADATA):
            raise SheetIntegrityError("Attempt to update a form response or unknown column.")
        current = self.rows()
        if row.get("participant_id"):
            matches = [r for r in current if r.get("participant_id") == row.get("participant_id")]
        else:
            matches = [r for r in current if r.number == row.number and r.data == row.data]
        if len(matches) != 1:
            raise SheetIntegrityError("Registration moved or changed during processing; retry after review.")
        target = matches[0]
        header = self._last_header
        updates = [{"range": rowcol_to_a1(target.number, header.index(self.settings.headers[k]) + 1), "values": [[v]]}
                   for k, v in changes.items()]
        # One atomic Sheets request; RAW prevents formula interpretation of user-derived values.
        self.ws.batch_update(updates, value_input_option="RAW")
        row.data.update({k: str(v) for k, v in changes.items()})


def by_id(sheet, participant_id, event_id):
    matches = [r for r in sheet.rows() if r.get("participant_id") == participant_id and r.get("event_id") == event_id]
    if len(matches) != 1:
        raise SheetIntegrityError("Participant ID was not found uniquely for this event.")
    return matches[0]
