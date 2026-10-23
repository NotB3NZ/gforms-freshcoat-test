"""One registration, no sending, durable recovery and compare-before-restore.

This path never invokes BatchProcessor or either production identity journal.
Its isolated reservation is permanently retired, even if Sheets cleanup blocks.
The regular production adapter and all mail operations keep their hard locks.
"""
import hashlib
import json
import os
import re
import secrets
import sqlite3
import subprocess
import tempfile
import uuid
from contextlib import closing
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

from .cards import CardRenderer, fitted_template, inspect_template
from .config import ConfigurationError, private_directory
from .header_init import HeaderTransport, classify, load_plan, require_checkpoint_settings
from .locking import file_lock
from .production import details, fingerprint, private_json, reasons, recipient
from .production_sheets import PRODUCTION_METADATA, ProductionSheets, validate_production_identities
from .sheets import Row, SheetIntegrityError
from .test_identity import TEST_TOKEN_PREFIX
from .diagnostics import diagnostic_operation

TEST_FIELDS = frozenset((
    "record_id", "event_id", "participant_id", "ticket_token", "normalized_email", "ticket_name", "ticket_tier",
    "source_id", "price_category", "state", "ticket_status", "email_status", "render_status", "render_attempts",
    "last_error", "updated_at", "card_path",
))


class ControlledTestBlocked(SheetIntegrityError):
    """A safe, fixed diagnostic without participant values."""


def tracking(row):
    return {key: row.get(key) for key in PRODUCTION_METADATA}


def assert_unissued(row, rows, event_id):
    try:
        validate_production_identities(rows)
    except SheetIntegrityError:
        raise ControlledTestBlocked("Production identity integrity requires review; no test started.") from None
    blockers = reasons(row, rows, event_id)
    if blockers:
        safe = [b.split(" registration ")[0] + " registration (identity withheld)" if b.startswith("duplicate of ") else b for b in blockers]
        raise ControlledTestBlocked("Requirements failed: " + "; ".join(safe) + ". No test started.")
    if row.get("participant_id") or row.get("ticket_token"):
        raise ControlledTestBlocked("Selected registration is already issued; no test started.")
    # Existing registration IDs and their history are retained, never replaced.
    if row.get("state") not in ("", "WAITING_APPROVAL", "PENDING") or any(
            row.get(k) not in ("", None) for k in (
                "render_status", "email_status", "card_path", "render_attempts", "email_attempts",
                "message_id", "email_sent_at", "checked_in_at", "checkin_request_id", "checked_in_by")):
        raise ControlledTestBlocked("Existing processing history requires review; no test started.")


def check_normal_reservations(settings, row):
    """Read existing journals without creating/changing them or claiming an email."""
    checks = (
        ("production-batches.sqlite3", "claims", "SELECT COUNT(*) FROM claims WHERE event_id=? AND email=?"),
        ("issuance.sqlite3", "identities", "SELECT COUNT(*) FROM identities WHERE event_id=? AND email=?"),
    )
    for filename, table, query in checks:
        path = settings.runtime / filename
        if not path.exists():
            continue
        try:
            with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
                db.execute("PRAGMA query_only=ON")
                if db.execute(query, (settings.event_id, recipient(row))).fetchone()[0]:
                    raise ControlledTestBlocked("Existing local identity reservation requires review; no test started.")
                if table == "claims" and row.get("record_id") and db.execute(
                        "SELECT COUNT(*) FROM actions WHERE record_id=?", (row.get("record_id"),)).fetchone()[0]:
                    raise ControlledTestBlocked("Existing local processing history requires review; no test started.")
        except sqlite3.Error:
            raise ControlledTestBlocked("Existing identity journal could not be inspected; no test started.") from None


class LiveTestSheet:
    """Exact target writes; no initialize, general update, queue or email API."""
    live = True

    @diagnostic_operation("open_production_sources_read_only")
    def __init__(self, settings):
        require_checkpoint_settings(settings)
        self.settings = settings
        self.plan = load_plan(settings, settings.root / "config/production-header-init.approved.json")
        self.reader = ProductionSheets(settings, writable=False)
        self.headers = HeaderTransport(settings, self.plan, apply=False)
        self.writer = None  # Write scope is requested only after a durable snapshot.

    @diagnostic_operation("validate_headers_and_read_mapped_columns")
    def rows(self):
        for entry in self.plan["sources"]:
            try:
                if classify(entry, self.headers.read(entry)) != "initialized":
                    raise ControlledTestBlocked("Approved tracking headers are not initialized.")
            except Exception as exc:
                exc._safe_operation_stage = "validate_header_snapshot_" + entry["source"]["id"]
                raise
        return self.reader.rows()

    @diagnostic_operation("read_selected_original_cell_values")
    def capture(self, source, number):
        """Preserve exact userEnteredValue objects, including blanks/formulas/types.

        Read just mapped form/approval cells and the approved tracking span. No
        proof/ID uploads, other Form answers, formats, notes or validation fetched.
        """
        if type(number) is not int or number < 2:
            raise ControlledTestBlocked("Target must be a participant-row locator, never row one.")
        entry = next(e for e in self.plan["sources"] if e["source"]["id"] == source)
        from .production_sheets import source_mapping, column_number
        from gspread.utils import rowcol_to_a1
        mapped = source_mapping(entry["source"])
        cells = {key: column_number(col) for key, (_, col) in mapped.items()}
        header = self.headers.read(entry)
        if classify(entry, header) != "initialized":
            raise ControlledTestBlocked("Header snapshot changed.")
        cells.update({key: header["headers"].index(key) + 1 for key in PRODUCTION_METADATA})
        prefix = "'" + entry["source"]["tab_name"].replace("'", "''") + "'!"
        ranges = [prefix + rowcol_to_a1(number, col) for col in cells.values()]
        self.headers.pace()
        data = self.headers.http.fetch_sheet_metadata(entry["source"]["spreadsheet_id"], params={
            "ranges": ranges, "includeGridData": "true",
            "fields": "spreadsheetId,sheets(properties(sheetId,title),data(startRow,startColumn,rowData(values(userEnteredValue))))"})
        sheets = data.get("sheets", [])
        if data.get("spreadsheetId") != entry["source"]["spreadsheet_id"] or len(sheets) != 1 or sheets[0].get("properties") != {
                "sheetId": entry["source"]["tab_id"], "title": entry["source"]["tab_name"]}:
            raise ControlledTestBlocked("Unexpected target grid response.")
        raw = {}
        valid_columns = set(cells.values())
        for grid in sheets[0].get("data", []):
            col = grid.get("startColumn", 0) + 1
            if grid.get("startRow", 0) != number - 1 or col not in valid_columns or col in raw:
                raise ControlledTestBlocked("Unexpected target grid shape.")
            rows = grid.get("rowData", [])
            if len(rows) > 1 or rows and len(rows[0].get("values", [])) > 1:
                raise ControlledTestBlocked("Unexpected target cell shape.")
            values = rows[0].get("values", []) if rows else []
            raw[col] = values[0].get("userEnteredValue", {}) if values else {}
        if set(raw) != valid_columns:
            raise ControlledTestBlocked("Incomplete original-cell snapshot.")
        if self.headers.read(entry) != header:
            raise ControlledTestBlocked("Headers changed during target snapshot.")
        return {"source": source, "row": number, "headers": header,
                "cells": cells, "values": {key: raw[col] for key, col in cells.items()}}

    def write(self, snapshot, changes):
        require_checkpoint_settings(self.settings)
        if not changes or set(changes) - TEST_FIELDS:
            raise ControlledTestBlocked("Only the controlled test's fixed tracking fields can be written.")
        current = self.capture(snapshot["source"], snapshot["row"])
        if current != snapshot:
            raise ControlledTestBlocked("Cells changed immediately before write; no write attempted.")
        if self.writer is None:
            # The sole write client lives inside this constrained adapter. The
            # general ProductionSheets writable constructor is never unlocked.
            import gspread
            client = gspread.service_account(filename=str(self.settings.service_account),
                                             scopes=["https://www.googleapis.com/auth/spreadsheets"])
            client.set_timeout((10, 30))
            self.writer = client.http_client
        entry = next(e for e in self.plan["sources"] if e["source"]["id"] == snapshot["source"])
        requests = [{"updateCells": {
            "range": {"sheetId": entry["source"]["tab_id"], "startRowIndex": snapshot["row"] - 1,
                      "endRowIndex": snapshot["row"], "startColumnIndex": snapshot["cells"][key] - 1,
                      "endColumnIndex": snapshot["cells"][key]},
            "rows": [{"values": [{"userEnteredValue": value} if value else {}]}],
            "fields": "userEnteredValue"}} for key, value in changes.items()]
        # Authentication and payload construction can take time; compare once
        # more after them, immediately before the constrained Sheets request.
        if self.capture(snapshot["source"], snapshot["row"]) != snapshot:
            raise ControlledTestBlocked("Cells changed immediately before write; no write attempted.")
        self.headers.pace()
        self.writer.batch_update(entry["source"]["spreadsheet_id"], {"requests": requests})


def changed_snapshot(snapshot, changes):
    result = deepcopy(snapshot)
    result["values"].update(changes)
    return result


def literal(value):
    # Sheets normalizes an entered empty string to an empty cell on readback.
    return {"stringValue": value} if value != "" else {}


def archive_invalid(path, destination):
    """Retain only a visibly invalid preview, preserving the original card area."""
    from PIL import Image, ImageDraw, ImageFont
    with Image.open(path) as source:
        image = Image.new("RGB", (source.width, source.height + 74), "#8b0000")
        image.paste(source.convert("RGB"), (0, 0))
        message = "INVALID TEST TICKET - RETIRED - NOT VALID FOR ADMISSION"
        font = ImageFont.load_default()
        box = ImageDraw.Draw(image).textbbox((0, 0), message, font=font)
        label = Image.new("RGB", (box[2] + 2, box[3] + 2), "#8b0000")
        ImageDraw.Draw(label).text((0, 0), message, fill="white", font=font)
        scale = min(3, (source.width - 48) / label.width)
        label = label.resize((int(label.width * scale), int(label.height * scale)), Image.Resampling.NEAREST)
        image.paste(label, ((source.width - label.width) // 2, source.height + (74 - label.height) // 2))
        image.save(destination)
    destination.chmod(0o600)
    if Path(path) != destination:
        Path(path).unlink()


def verify_render(settings, path, name, tier, token):
    """Verify QR and text pixels against independently literalized bindings.

    The reference replaces the name/tier bindings with expected literal text in
    a temporary copy of the current template. Exact text-box pixels must match
    the personalized output. No OCR, raw text output, template edits or sending.
    """
    import cv2
    import numpy as np
    from PIL import Image
    template, dimensions = inspect_template(settings)
    original_hash = hashlib.sha256(settings.template.read_bytes()).hexdigest()
    if cv2.QRCodeDetector().detectAndDecode(cv2.imread(str(path)))[0] != token:
        raise ControlledTestBlocked("Rendered QR did not decode to the test token.")
    boxes = {settings.name_field: [], settings.tier_field: []}
    expected = {settings.name_field: name, settings.tier_field: tier}
    def visit(elements, ox=0, oy=0):
        for element in elements:
            props = element.get("properties", {})
            x, y = ox + element.get("pos", {}).get("x", 0), oy + element.get("pos", {}).get("y", 0)
            if element.get("type") == "text" and isinstance(props.get("value"), str):
                for key, value in expected.items():
                    pattern = r"{{\s*" + re.escape(key) + r"\s*}}"
                    if re.search(pattern, props["value"]):
                        size = element.get("size", {})
                        boxes[key].append((int(x), int(y), int(x + size["width"]), int(y + size["height"])))
                        props["value"] = re.sub(pattern, lambda _: value, props["value"])
                        if key == settings.name_field:
                            props["fit"] = "shrink"
            visit(props.get("children", []), x, y)
    for frame in template["template_data"]:
        if frame["name"] == settings.frame:
            visit(frame["elements"])
    if not all(boxes.values()):
        raise ControlledTestBlocked("Name/tier text bindings are missing.")
    with tempfile.TemporaryDirectory(prefix="verify-", dir=Path(path).parent) as temp:
        temp = Path(temp)
        reference = temp / "literal-reference.coat"
        fitted_template(settings.template, reference, template, settings.name_field)
        values = dict(expected)
        if "name" in template["fields"]["properties"]:
            values["name"] = name[:template["fields"]["properties"]["name"].get("maxLength", len(name))]
        if "ticket_id" in template["fields"]["properties"]:
            values["ticket_id"] = Path(path).parent.name
        values_path = temp / "values.json"
        private_json(values_path, values)
        try:
            subprocess.run([settings.bun, str(settings.freshcoat / "packages/cli/src/bin.ts"), "render", str(reference),
                            "--values", str(values_path), "--frame", settings.frame, "--scale", "1", "--format", "png", "--out", str(temp)],
                           cwd=settings.freshcoat, capture_output=True, check=True, timeout=120)
        except (OSError, subprocess.SubprocessError):
            raise ControlledTestBlocked("Independent text verification render failed.") from None
        with Image.open(path) as actual, Image.open(temp / (settings.frame + ".png")) as reference_image:
            if actual.size != dimensions or reference_image.size != dimensions:
                raise ControlledTestBlocked("Unexpected card dimensions.")
            for regions in boxes.values():
                for box in regions:
                    pixels = np.asarray(actual.convert("RGB").crop(box))
                    if not np.array_equal(pixels, np.asarray(reference_image.convert("RGB").crop(box))) or np.max(pixels) == np.min(pixels):
                        raise ControlledTestBlocked("Rendered name/tier text pixels did not match the expected literals.")
    if hashlib.sha256(settings.template.read_bytes()).hexdigest() != original_hash:
        raise ControlledTestBlocked("Current template changed during verification.")
    return {"name_pixels_verified": True, "tier_pixels_verified": True, "qr_decoded": True,
            "template_sha256": original_hash, "dimensions": list(dimensions)}


class ControlledRenderTest:
    def __init__(self, settings, sheet, renderer=None, verifier=verify_render):
        require_checkpoint_settings(settings)
        self.settings, self.sheet = settings, sheet
        self.renderer, self.verifier = renderer, verifier

    @diagnostic_operation("validate_single_registration")
    def prepare(self, source, number):
        rows = self.sheet.rows()
        matches = [r for r in rows if r.source_id == source and r.number == number]
        if len(matches) != 1:
            raise ControlledTestBlocked("Selected locator is unavailable or ambiguous.")
        row = matches[0]
        assert_unissued(row, rows, self.settings.event_id)
        if len([r for r in rows if r.source_id == source and fingerprint(r) == fingerprint(row)]) != 1:
            raise ControlledTestBlocked("Selected registration cannot be uniquely bound; review duplicate submissions.")
        check_normal_reservations(self.settings, row)
        snapshot = self.sheet.capture(source, number)
        # Align computed read with exact original cells before saving a selection.
        fresh = self.sheet.rows()
        selected = [r for r in fresh if r.source_id == source and r.number == number]
        if len(selected) != 1 or selected[0].data != row.data or self.sheet.capture(source, number) != snapshot:
            raise ControlledTestBlocked("Registration changed during preparation.")
        return {"version": 1, "event_id": self.settings.event_id, "profile": self.settings.profile,
                "source": source, "row": number, "fingerprint": fingerprint(row),
                "original": snapshot, "selected_data": deepcopy(row.data)}

    def cleanup(self, audit, audit_path):
        # Retirement is durable before attempting any cleanup. No normal local
        # identity/claims table ever contained this reservation or is deleted.
        audit["reservation_status"] = "RETIRED"
        audit["cleanup"] = "RECOVERY_REQUIRED"
        private_json(audit_path, audit)
        try:
            if not set(audit["touched"]) <= TEST_FIELDS:
                raise ControlledTestBlocked("Recovery audit contains fields outside this test's fixed write scope.")
            current = self.sheet.capture(audit["source"], audit["row"])
            original = audit["original"]
            expected = audit["expected"]
            if current == original:
                pass  # Write failed before commit, or cleanup committed despite timeout.
            elif any(current == state for state in audit.get("expected_states", [expected])):
                restore = {key: original["values"][key] for key in audit["touched"]}
                audit["restore_intent"] = True
                private_json(audit_path, audit)
                try:
                    self.sheet.write(current, restore)
                except Exception:
                    # Read back: do not blindly repeat an uncertain restore.
                    pass
                if self.sheet.capture(audit["source"], audit["row"]) != original:
                    raise ControlledTestBlocked("Restore could not be verified.")
            else:
                raise ControlledTestBlocked("Concurrent changes or a partial write prevent safe cleanup.")
            # Verify the test token is absent from every production source.
            rows = self.sheet.rows()
            if any(r.get("ticket_token") == audit["token"] or r.get("participant_id") == audit["participant_id"] for r in rows):
                raise ControlledTestBlocked("Retired test identity is still present in a production source.")
            audit["cleanup"] = "RESTORED_AND_VERIFIED"
        except Exception:
            # Sanitized fixed result. The original and every intended value stay
            # in the private audit for deliberate recovery; nothing is forced.
            audit["cleanup"] = "RECOVERY_REQUIRED"
        private_json(audit_path, audit)
        return audit["cleanup"] == "RESTORED_AND_VERIFIED"

    def execute(self, selection):
        require_checkpoint_settings(self.settings)
        if selection.get("version") != 1 or selection.get("event_id") != self.settings.event_id or selection.get("profile") != self.settings.profile:
            raise ControlledTestBlocked("Selection belongs to another event/profile.")
        selection = deepcopy(selection)
        selection_id = selection.pop("selection_id", None)
        if not selection_id and getattr(self.sheet, "live", False):
            raise ControlledTestBlocked("Live execution requires a privately prepared, single-use selection.")
        selection_id = selection_id or str(uuid.uuid4())  # Synthetic adapters only.
        try:
            if str(uuid.UUID(selection_id)) != selection_id:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise ControlledTestBlocked("Invalid single-use selection ID.") from None
        with file_lock(self.settings.runtime / "worker.lock", blocking=False), file_lock(self.settings.runtime / "sheets.lock"):
            fresh = self.prepare(selection["source"], selection["row"])
            if fresh != selection:
                raise ControlledTestBlocked("Selected registration changed; obtain a fresh selection.")
            consumed = private_directory(self.settings.runtime / "controlled-tests" / "selections") / (selection_id + ".json")
            if consumed.exists():
                raise ControlledTestBlocked("This selection was already attempted; use its audit for cleanup, never repeat issuance.")
            run_id = str(uuid.uuid4())
            directory = private_directory(self.settings.runtime / "controlled-tests" / run_id)
            audit_path = directory / "audit.json"
            render_settings = replace(self.settings, generated=private_directory(directory / "render"), email_enabled=False)
            renderer = self.renderer or CardRenderer(render_settings)
            row_data = fresh["selected_data"]
            row = Row(fresh["row"], row_data, fresh["source"])
            name, tier, price = details(row)
            pid = "TEST-" + run_id
            token = TEST_TOKEN_PREFIX + secrets.token_urlsafe(24)
            changes = {"record_id": row.get("record_id") or "TEST-R-" + run_id,
                       "event_id": self.settings.event_id, "participant_id": pid, "ticket_token": token,
                       "normalized_email": recipient(row), "ticket_name": name, "ticket_tier": tier,
                       "source_id": row.source_id, "price_category": price,
                       "state": "CANCELLED", "ticket_status": "CANCELLED", "email_status": "DISABLED",
                       "render_status": "RENDERING", "render_attempts": "1", "last_error": "",
                       "updated_at": self.settings.now()}
            raw_changes = {key: literal(value) for key, value in changes.items()}
            audit = dict(fresh, run_id=run_id, selection_id=selection_id, participant_id=pid, token=token,
                         reservation_status="RESERVED_TEST_ONLY", expected=changed_snapshot(fresh["original"], raw_changes),
                         touched=list(raw_changes), cleanup="NOT_STARTED", verification=None, card_preview=None,
                         phase="PREFLIGHT")
            audit["expected_states"] = [deepcopy(audit["expected"])]
            private_json(audit_path, audit)  # Durable original + write intent BEFORE first write.
            # One prepared selection may start only one attempt, even after its
            # Sheet cells have been restored to the exact original values.
            fd = os.open(consumed, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "w") as handle:
                json.dump({"audit": str(audit_path)}, handle)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                # Recheck all approvals, source access, aliases and duplicates
                # after saving the backup, immediately before the identity write.
                if self.prepare(selection["source"], selection["row"]) != selection:
                    raise ControlledTestBlocked("Registration changed before test identity assignment.")
                audit["phase"] = "ASSIGN_IDENTITY"
                private_json(audit_path, audit)
                self.sheet.write(fresh["original"], raw_changes)
                if self.sheet.capture(fresh["source"], fresh["row"]) != audit["expected"]:
                    raise ControlledTestBlocked("Identity write could not be verified.")
                audit["phase"] = "RENDER"
                private_json(audit_path, audit)
                path = renderer.render(pid, name, token, ticket_tier=tier)
                # Archive first: even a verification failure leaves a clearly
                # invalid preview, never a production-looking raw card artifact.
                destination = directory / "INVALID-TEST-TICKET.png"
                try:
                    audit["phase"] = "VERIFY_CARD"
                    private_json(audit_path, audit)
                    audit["verification"] = self.verifier(render_settings, path, name, tier, token)
                finally:
                    archive_invalid(path, destination)
                    audit["card_preview"] = str(destination)
                    private_json(audit_path, audit)
                # Do not mark the temporary identity ACTIVE or touch email state.
                more = {"render_status": literal("RENDERED"), "card_path": literal(str(destination)),
                        "updated_at": literal(self.settings.now())}
                previous = deepcopy(audit["expected"])
                audit["expected"] = changed_snapshot(previous, more)
                audit["expected_states"].append(deepcopy(audit["expected"]))
                audit["touched"] = sorted(set(audit["touched"]) | set(more))
                audit["phase"] = "RENDER_METADATA"
                private_json(audit_path, audit)
                self.sheet.write(previous, more)
                if self.sheet.capture(fresh["source"], fresh["row"]) != audit["expected"]:
                    raise ControlledTestBlocked("Render metadata write could not be verified.")
                audit["test_result"] = "VERIFIED"
            except Exception as exc:
                audit["test_result"] = "STOPPED"
                audit["error_type"] = type(exc).__name__
            finally:
                leftover = render_settings.generated / pid / "card.png"
                if leftover.is_file() and not audit["card_preview"]:
                    try:
                        destination = directory / "INVALID-TEST-TICKET.png"
                        archive_invalid(leftover, destination)
                        audit["card_preview"] = str(destination)
                    except Exception:
                        # Preserve any failed output privately with an explicit
                        # invalid-test filename; cleanup must still be attempted.
                        audit["archive_error"] = True
                        try:
                            leftover.rename(leftover.with_name("UNVERIFIED-INVALID-TEST-TICKET.png"))
                        except OSError:
                            pass
                self.cleanup(audit, audit_path)
            return {"test_result": audit["test_result"], "verification": audit["verification"],
                    "cleanup": audit["cleanup"], "reservation_status": audit["reservation_status"],
                    "card_preview": audit["card_preview"], "audit": str(audit_path), "email_sent": False,
                    "failure_stage": audit["phase"] if audit["test_result"] == "STOPPED" else None,
                    "error_type": audit.get("error_type"),
                    "production_processing": "LOCKED", "email_enabled": False}


def recover(settings, sheet, audit_path):
    """Cleanup only; never rerender, reassign an identity or repeat issuance."""
    require_checkpoint_settings(settings)
    path = Path(audit_path).resolve()
    base = (settings.runtime / "controlled-tests").resolve()
    if not path.is_relative_to(base) or path.name != "audit.json":
        raise ControlledTestBlocked("Recovery requires a private controlled-test audit.")
    audit = json.loads(path.read_text())
    if audit.get("event_id") != settings.event_id or audit.get("profile") != settings.profile or audit.get("token", "").startswith(TEST_TOKEN_PREFIX) is not True:
        raise ControlledTestBlocked("Recovery audit does not belong to this test namespace/event.")
    with file_lock(settings.runtime / "worker.lock", blocking=False), file_lock(settings.runtime / "sheets.lock"):
        return ControlledRenderTest(settings, sheet).cleanup(audit, path)


def run_command(settings, args):
    """Separate explicit prepare/apply/recover surface, never a batch unlock."""
    require_checkpoint_settings(settings)
    if args.recover:
        if args.selection or args.apply or args.source or args.row:
            raise ControlledTestBlocked("Choose cleanup recovery alone.")
        restored = recover(settings, LiveTestSheet(settings), args.recover)
        print(json.dumps({"cleanup": "RESTORED_AND_VERIFIED" if restored else "RECOVERY_REQUIRED",
                          "email_enabled": False, "production_processing": "LOCKED"}))
        return
    if args.apply:
        if not args.selection or args.source or args.row:
            raise ControlledTestBlocked("Execution requires exactly one private prepared selection.")
        path = Path(args.selection).resolve()
        if not path.is_relative_to(settings.runtime.resolve()):
            raise ControlledTestBlocked("Selection must be private within the production runtime.")
        service = ControlledRenderTest(settings, LiveTestSheet(settings))
        result = service.execute(json.loads(path.read_text()))
        print(json.dumps(result, indent=2))
    else:
        if not args.source or not isinstance(args.row, int) or args.row < 2 or not args.selection:
            raise ControlledTestBlocked("Preparation requires one source, row >= 2 and a new private selection path.")
        path = Path(args.selection).resolve()
        if not path.is_relative_to(settings.runtime.resolve()) or path.exists():
            raise ControlledTestBlocked("Choose a new private selection filename within the production runtime.")
        service = ControlledRenderTest(settings, LiveTestSheet(settings))
        selection = service.prepare(args.source, args.row)
        selection["selection_id"] = str(uuid.uuid4())
        private_json(path, selection)
        print(json.dumps({"selection": str(path), "source": args.source, "row_locator": args.row,
                          "all_four_approvals": True, "unissued": True, "duplicate_review": "PASSED",
                          "mapped_tier": details(Row(args.row, selection['selected_data'], args.source))[1],
                          "participant_writes": 0, "email_enabled": False, "production_processing": "LOCKED"}))
