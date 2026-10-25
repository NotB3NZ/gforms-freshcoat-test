"""Explicit personal-sender delivery for regular row 2. General production stays locked."""
import hashlib
import json
import re
import sqlite3
import uuid
import time
from contextlib import closing
from copy import deepcopy
from dataclasses import replace
from email.utils import parseaddr
from pathlib import Path
from types import SimpleNamespace

from .cards import CardRenderer, google_sans_files, inspect_template
from .config import ConfigurationError, load_settings
from .controlled_render import LiveTestSheet, assert_unissued, check_normal_reservations, verify_render
from .header_init import EVENT_ID, HeaderTransport, load_plan, classify
from .mailer import SMTPMailer
from .production import (BatchProcessor, approved, details, fingerprint, preview, private_json, reasons,
                         recipient, target, validate_production_identities)
from .production_sheets import PRODUCTION_METADATA, source_mapping, column_number
from .test_identity import is_test_token
from .diagnostics import diagnostic_operation, diagnostic_context, safe_diagnostic

SOURCE, NUMBER = "regular", 2
TEMPLATE_SHA256 = "7e3c7247d68dd490d2987ae616d40cf344805a38d6a36023a9176d97327830d4"
WRITE_FIELDS = frozenset(("record_id", "event_id", "participant_id", "ticket_token", "normalized_email",
    "ticket_name", "ticket_tier", "source_id", "price_category", "state", "ticket_status", "last_error",
    "render_status", "render_attempts", "card_path", "updated_at", "email_status", "email_attempts",
    "message_id", "email_sent_at"))


def require(condition, message):
    if not condition:
        raise ConfigurationError(message)


def checksum(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def scope_path(settings):
    return settings.root / "config/single-entry-send.approved.json"


def policy_identity(policy):
    return {key: policy[key] for key in ("version", "event_id", "source", "row", "sender",
                                       "template_sha256", "manifest_sha256", "headers_sha256")}


def read_policy(settings):
    try:
        policy = json.loads(scope_path(settings).read_text())
        require((settings.profile, settings.event_id, policy["version"], policy["event_id"], policy["source"], policy["row"])
                == ("production", EVENT_ID, 1, EVENT_ID, SOURCE, NUMBER), "Single-entry authorization is only for regular row 2.")
        require(type(policy["row"]) is int and type(policy["enabled"]) is bool, "Invalid single-entry authorization types.")
        sender = policy["sender"]
        require(isinstance(sender, str) and bool(re.fullmatch(r"[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+", sender))
                and sender.casefold() != "awscc.upmindanao@gmail.com", "Pin the personal sender address privately.")
        require(policy["template_sha256"] == TEMPLATE_SHA256 == checksum(settings.template)
                and (settings.frame, settings.name_field, settings.tier_field, settings.qr_x, settings.qr_y, settings.qr_size)
                == ("front", "displayName", "ticketTier", 378, 228, 444), "Final badge configuration changed.")
        require(policy["manifest_sha256"] == checksum(settings.sources_file)
                and policy["headers_sha256"] == checksum(settings.root / "config/production-header-init.approved.json"),
                "Approved production mappings or snapshot changed; new review required.")
        return policy
    except (OSError, ValueError, KeyError, TypeError):
        raise ConfigurationError("Private single-entry authorization is missing or invalid.") from None


def private_selection_path(settings, path):
    path = Path(path).resolve()
    require(path.is_relative_to(settings.runtime.resolve()) and path.suffix == ".json",
            "Save the single-entry selection privately inside production runtime.")
    return path


def read_adapter(settings):
    # This clone is used ONLY by read-only schema/cell snapshot code, whose old
    # initialization checkpoint requires disabled email. It is never a mailer.
    # Sending always checks the actual production flag separately, without overrides.
    reader = LiveTestSheet(replace(settings, email_enabled=False))
    reader.dataset = reader.reader.dataset
    return reader


def fixed_row(rows, selection=None):
    validate_production_identities(rows)
    matches = [row for row in rows if row.source_id == SOURCE and row.number == NUMBER]
    require(len(matches) == 1, "regular row 2 is unavailable or ambiguous; do not select another row.")
    row = matches[0]
    if selection:
        require(fingerprint(row) == selection["fingerprint"], "The selected registration changed or moved; stop for review.")
    return row


def safe_blockers(values):
    return [value.split(" registration ")[0] + " registration (identity withheld)"
            if value.startswith("duplicate of ") else value for value in values]


def prepare(settings, selection_path, reader=None):
    policy = read_policy(settings)
    require(not settings.email_enabled and not policy["enabled"], "Disable email and the single-entry scope before preparing.")
    path = private_selection_path(settings, selection_path)
    require(not path.exists(), "Existing selections are preserved; choose a new private filename.")
    inspect_template(settings); google_sans_files()
    reader = reader or read_adapter(settings)
    rows = reader.rows()
    row = fixed_row(rows)
    checks = {"source": SOURCE, "row": NUMBER, "all_source_schemas_verified": True,
              "approvals": {key: "TRUE" if approved(row.get(key)) else "not TRUE" for key in
                            ("confirmation", "reference_matched", "price_matched", "payment_received")},
              "mapped_tier": details(row)[1], "blockers": safe_blockers(reasons(row, rows, settings.event_id)),
              "email_enabled": False, "smtp_connected": False, "participant_writes": 0,
              "real_identity_issued": False, "general_production_processing": "LOCKED"}
    try:
        assert_unissued(row, rows, settings.event_id)
        require(sum(r.source_id == SOURCE and fingerprint(r) == fingerprint(row) for r in rows) == 1,
                "The intended registration cannot be uniquely bound.")
        check_normal_reservations(settings, row)
    except (ConfigurationError, RuntimeError) as exc:
        if str(exc) not in checks["blockers"]:
            checks["blockers"].append(str(exc))
    if checks["blockers"]:
        private_json(path.with_suffix(".preflight.json"), checks)
        return checks
    original = reader.capture(SOURCE, NUMBER)
    fresh = fixed_row(reader.rows())
    require(fresh.data == row.data and reader.capture(SOURCE, NUMBER) == original,
            "Registration or original cells changed during preparation.")
    frozen = preview(settings, SimpleNamespace(rows=lambda: rows, live=reader.live, dataset=reader.dataset),
                     ids=[target(row)], limit=1, selection_version=1)[1]
    require(len(frozen["entries"]) == 1 and frozen["entries"][0]["process"], "One eligible entry is required.")
    selection = {"version": 1, "source": SOURCE, "row": NUMBER, "event_id": EVENT_ID,
                 "policy": policy_identity(policy), "fingerprint": fingerprint(row), "selected_data": row.data,
                 "original": original, "plan": frozen}
    private_json(path, selection)
    policy.update(selection_path=str(path), selection_sha256=checksum(path))
    private_json(scope_path(settings), policy)
    checks.update(selection=str(path), eligible=True, unissued=True, local_reservations="CLEAR")
    private_json(path.with_suffix(".preflight.json"), checks)
    return checks


def load_selection(settings, path, policy):
    path = private_selection_path(settings, path)
    require(str(path) == policy.get("selection_path") and checksum(path) == policy.get("selection_sha256"),
            "Selection is not the privately bound reviewed selection.")
    selection = json.loads(path.read_text())
    require((selection["version"], selection["source"], selection["row"], selection["event_id"])
            == (1, SOURCE, NUMBER, EVENT_ID) and type(selection["row"]) is int,
            "Selection is not regular row 2.")
    require(selection["policy"] == policy_identity(policy), "Sender or approved scope changed after preparation.")
    plan = selection["plan"]
    require(plan["action"] == "send" and plan["limit"] == 1 and not plan["regenerate"]
            and len(plan["entries"]) == 1 and plan["entries"][0]["source"] == SOURCE
            and plan["entries"][0]["process"] is True
            and plan["entries"][0]["fingerprint"] == selection["fingerprint"], "Invalid single-entry plan.")
    return selection


@diagnostic_operation("single_entry_journal_read", source=SOURCE)
def original_action(settings, selection):
    path = settings.runtime / "production-batches.sqlite3"
    if not path.exists():
        return None
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            db.execute("PRAGMA query_only=ON")
            plan = selection["plan"]
            return db.execute("SELECT record_id FROM actions WHERE plan_id=? AND target=?",
                              (plan["plan_id"], plan["entries"][0]["target"])).fetchone()
    except sqlite3.Error:
        raise ConfigurationError("Existing identity journal requires review.") from None


def diagnostic_artifact(path, label, payload):
    destination = Path(path).with_name(Path(path).stem+"."+label+"-"+str(time.time_ns())+".json")
    private_json(destination, payload)
    return destination


def delivery_evidence(status, initial):
    """Absence of completion alone never establishes non-submission."""
    bound = all(status.get(key) for key in ("bound_registration_matches", "record_id_matches_action",
        "participant_identity_matches_reservation", "frozen_ticket_details_match_reservation",
        "frozen_recipient_matches_reservation", "journal_plan_digest_matches"))
    if not bound:
        return "UNCERTAIN", "Current tracking cannot be verified as this bound attempt."
    if status.get("durable_smtp_acceptance_recorded"):
        return "CONFIRMED_ACCEPTED", "An acceptance checkpoint for this exact bound selection was retained."
    if status["email_status"] == "SENT" and status["email_sent_at_present"] and status["message_id_present"]:
        return "CONFIRMED_ACCEPTED", "SENT and its sent timestamp were recorded after the SMTP acceptance boundary."
    untouched = not any(initial.get(key) for key in ("email_status", "email_attempts", "email_sent_at", "message_id"))
    if (untouched and status["state"] == "PROCESSING" and status["render_status"] == "RENDERED"
            and status["email_status"] == "" and str(status["email_attempts"]) == "0"
            and not status["message_id_present"] and not status["email_sent_at_present"]
            and not status["completion_recorded"] and not status["any_related_action_completed"]):
        return "NOT_SUBMITTED", "No durable SENDING intent, email-attempt increment or Message-ID exists; this path requires their confirmed readback before SMTP."
    return "UNCERTAIN", "Completion and SMTP acceptance cannot be established from the retained tracking evidence."


def safe_tracking_status(value, allowed):
    return value if value in allowed else "UNRECOGNIZED_VALUE_WITHHELD"


def safe_attempt_count(value):
    return str(value or "0") if re.fullmatch(r"[0-9]{1,8}",str(value or "0")) else "INVALID_COUNT_WITHHELD"


@diagnostic_operation("single_entry_read_tracking", source=SOURCE)
def diagnose_existing(settings, path, transport=None):
    """No processor, writable adapter, renderer, identity reservation or SMTP object."""
    policy = read_policy(settings)
    require(not settings.email_enabled and not policy["enabled"], "Disable email and the single-entry scope for diagnosis.")
    selection = load_selection(settings, path, policy)
    plan = load_plan(settings, settings.root/"config/production-header-init.approved.json")
    transport = transport or HeaderTransport(settings, plan, apply=False)
    snapshots = {}
    for entry in plan["sources"]:
        with diagnostic_context("single_entry_read_header", entry["source"]["id"]):
            snapshot = transport.read(entry)
            require(classify(entry, snapshot) == "initialized", "Approved headers do not match; diagnosis stopped.")
            snapshots[entry["source"]["id"]] = snapshot
    entry = next(e for e in plan["sources"] if e["source"]["id"] == SOURCE)
    mapping = source_mapping(entry["source"])
    positions = {key: column_number(mapping[key][1]) for key in ("email", "name", "tier")}
    header = snapshots[SOURCE]["headers"]
    positions.update({key: header.index(key)+1 for key in PRODUCTION_METADATA})
    from gspread.utils import rowcol_to_a1
    prefix = "'"+entry["source"]["tab_name"].replace("'", "''")+"'!"
    ranges = [prefix+rowcol_to_a1(NUMBER,col)+":"+rowcol_to_a1(NUMBER,col) for col in positions.values()]
    transport.pace()
    data = transport.http.values_batch_get(entry["source"]["spreadsheet_id"], ranges,
                                          params={"valueRenderOption":"UNFORMATTED_VALUE"})
    columns = data.get("valueRanges", [])
    require(len(columns) == len(positions), "Incomplete single-row tracking read.")
    values = {}
    for key, column in zip(positions, columns):
        cells = column.get("values", [])
        require(len(cells) <= 1 and (not cells or len(cells[0]) <= 1), "Unexpected tracking range shape.")
        values[key] = cells[0][0] if cells and cells[0] else ""
    values["_price_category"] = entry["source"]["price_category"]
    from .sheets import Row
    row = Row(NUMBER, values, SOURCE)
    require(fingerprint(row) == selection["fingerprint"], "The current physical row no longer matches the bound registration.")
    journal = settings.runtime/"production-batches.sqlite3"
    require(journal.is_file(), "The existing action journal is unavailable; no replacement was created.")
    with diagnostic_context("single_entry_journal_read", SOURCE), closing(sqlite3.connect(journal.resolve().as_uri()+"?mode=ro",uri=True)) as db:
        db.execute("PRAGMA query_only=ON")
        frozen = selection["plan"]
        action = db.execute("SELECT record_id,message_id,done FROM actions WHERE plan_id=? AND target=?",
                            (frozen["plan_id"],frozen["entries"][0]["target"])).fetchone()
        initial = Row(NUMBER, selection["selected_data"], SOURCE)
        claim = db.execute("SELECT * FROM claims WHERE event_id=? AND email=?",(settings.event_id,recipient(initial))).fetchone()
        saved_plan = db.execute("SELECT digest FROM plans WHERE id=?",(frozen["plan_id"],)).fetchone()
        require(action and claim, "The existing journal action or reservation is unavailable; stop without replacing it.")
        completed = db.execute("SELECT COUNT(*) FROM actions WHERE record_id=? AND done=1",(action[0],)).fetchone()[0]
    from .production import digest
    report = {"source":SOURCE,"row":NUMBER,"all_source_schemas_verified":True,
        "state":safe_tracking_status(row.get("state"),{"","PROCESSING","PENDING","READY","SENT","EMAIL_ERROR","EMAIL_REVIEW","RENDER_ERROR","REGISTRATION_REVIEW","WAITING_APPROVAL","CANCELLED","DUPLICATE","INVALID"}),
        "render_status":safe_tracking_status(row.get("render_status"),{"","RENDERING","RENDERED","FAILED"}),
        "email_status":safe_tracking_status(row.get("email_status"),{"","SENDING","SENT","FAILED","AMBIGUOUS","DISABLED","RETRY"}),
        "render_attempts":safe_attempt_count(row.get("render_attempts")),"email_attempts":safe_attempt_count(row.get("email_attempts")),
        "completion_recorded":bool(action[2]),"any_related_action_completed":bool(completed),
        "message_id_present":bool(row.get("message_id")),"message_id_matches_action":row.get("message_id")==action[1],
        "email_sent_at_present":bool(row.get("email_sent_at")),"bound_registration_matches":True,
        "journal_plan_digest_matches":bool(saved_plan and saved_plan[0]==digest(frozen)),
        "record_id_matches_action":row.get("record_id")==action[0]==claim[2],
        "participant_identity_matches_reservation":(row.get("participant_id"),row.get("ticket_token"))==tuple(claim[3:5]),
        "frozen_ticket_details_match_reservation":(row.get("ticket_name"),row.get("ticket_tier"),row.get("price_category"),row.get("source_id"))==tuple(claim[5:]),
        "frozen_recipient_matches_reservation":row.get("normalized_email")==claim[1],
        "card_exists":(settings.generated/claim[3]/"card.png").is_file(),
        "card_path_matches_reservation":bool(row.get("card_path")) and Path(row.get("card_path")).resolve()==(settings.generated/claim[3]/"card.png").resolve(),
        "email_enabled":False,"scope_enabled":False,"participant_writes":0,"smtp_connected":False}
    acceptance = False
    for event_path in Path(path).parent.glob(Path(path).stem+".smtp-evidence-*.json"):
        event = json.loads(event_path.read_text())
        if event.get("accepted") is True and event.get("selection_sha256") == checksum(path):
            acceptance = True
    report["durable_smtp_acceptance_recorded"] = acceptance
    report["smtp_submission"], report["evidence"] = delivery_evidence(report,selection["selected_data"])
    report["private_report"] = str(diagnostic_artifact(path,"diagnosis",report))
    return report


class SingleSettings:
    """Only require_email is scoped. require_operational remains hard-locked."""
    def __init__(self, settings, policy, mode, reload_settings=None):
        self.base, self.policy, self.mode, self.reload = settings, policy, mode, reload_settings

    def __getattr__(self, key):
        return getattr(self.base, key)

    @diagnostic_operation("single_entry_email_settings", source=SOURCE)
    def require_email(self):
        require(self.mode in ("apply", "retry"), "This scope does not authorize SMTP.")
        require(self.base.email_enabled, "EMAIL_ENABLED=false prohibits SMTP sending.")
        current = self.reload() if self.reload else self.base
        policy = read_policy(current)
        require(current.email_enabled and policy["enabled"], "Email or the one-entry scope is disabled.")
        require(policy == self.policy, "Single-entry authorization changed during execution.")
        require(parseaddr(current.smtp_from)[1].casefold() == policy["sender"].casefold()
                and current.smtp_user.casefold() == policy["sender"].casefold(),
                "SMTP_FROM and SMTP_USERNAME must match the privately pinned personal sender.")
        require(bool(current.smtp_host and current.smtp_user and current.smtp_password and current.smtp_from)
                and current.smtp_security in ("starttls", "ssl"), "Complete the private personal SMTP settings.")
        require(all(getattr(current, key) == getattr(self.base, key) for key in
                    ("smtp_host", "smtp_port", "smtp_security", "smtp_user", "smtp_password", "smtp_from")),
                "SMTP settings changed during execution; no send authorized.")

    def require_write(self):
        if self.mode != "resolve":
            self.require_email()
        else:
            require(read_policy(self.base) == self.policy, "Single-entry authorization changed.")


class SingleSheet:
    """All-source reads; only exact metadata cells on the bound physical row can be written."""
    def __init__(self, settings, reader, selection, write_transport=None):
        self.settings, self.reader, self.selection = settings, reader, selection
        self.live, self.dataset = reader.live, reader.dataset
        self.http = None
        self.write_transport = write_transport

    def require_write(self):
        self.settings.require_write()

    @diagnostic_operation("single_entry_read_registration", source=SOURCE)
    def rows(self):
        rows = self.reader.rows()
        row = fixed_row(rows, self.selection)
        known = original_action(self.settings, self.selection)
        initial = self.selection["selected_data"].get("record_id")
        require(not row.get("record_id") or row.get("record_id") in (initial, known[0] if known else None),
                "Row 2 acquired an unrelated record identity; stop.")
        if row.get("ticket_token") or row.get("participant_id"):
            require(known and row.get("record_id") == known[0], "This scope cannot adopt a ticket issued by another workflow.")
        return rows

    @diagnostic_operation("single_entry_tracking_update", source=SOURCE)
    def update(self, row, changes):
        self.require_write()
        require(row.source_id == SOURCE and row.number == NUMBER and changes and set(changes) <= WRITE_FIELDS,
                "Only regular row 2's authorized tracking fields may be written.")
        fresh = fixed_row(self.rows(), self.selection)
        require(fresh.data == row.data, "Registration changed immediately before its metadata update.")
        snapshot = self.reader.capture(SOURCE, NUMBER)
        require(fixed_row(self.rows(), self.selection).data == fresh.data,
                "Registration changed during original-cell capture.")
        self.require_write()
        if self.write_transport:
            self.write_transport(snapshot, changes)
        else:
            if self.http is None:
                import gspread
                client = gspread.service_account(filename=str(self.settings.service_account),
                                                scopes=["https://www.googleapis.com/auth/spreadsheets"])
                client.set_timeout((10, 30)); self.http = client.http_client
            require(self.reader.capture(SOURCE, NUMBER) == snapshot, "Typed cells changed immediately before write.")
            entry = next(e for e in self.reader.plan["sources"] if e["source"]["id"] == SOURCE)
            requests = [{"updateCells": {"range": {"sheetId": entry["source"]["tab_id"],
                "startRowIndex": NUMBER-1, "endRowIndex": NUMBER,
                "startColumnIndex": snapshot["cells"][key]-1, "endColumnIndex": snapshot["cells"][key]},
                "rows": [{"values": [{"userEnteredValue": {"stringValue": str(value)}}]}],
                "fields": "userEnteredValue"}} for key,value in changes.items()]
            with diagnostic_context("single_entry_tracking_write", SOURCE):
                self.http.batch_update(entry["source"]["spreadsheet_id"], {"requests": requests})
        row.data.update({key: str(value) for key,value in changes.items()})


class SingleProcessor(BatchProcessor):
    """Seal the reused processor to one action/entry, including resolution-only scopes."""
    def execute(self, plan):
        require(self.s.mode in ("apply", "retry"), "A resolution-only scope cannot issue or send tickets.")
        original = self.sheet.selection["plan"]
        entries = plan.get("entries", [])
        expected_id = (original["plan_id"] if self.s.mode == "apply" else
                       str(uuid.uuid5(uuid.UUID(original["plan_id"]), "explicit-single-entry-retry")))
        require(plan.get("plan_id") == expected_id and plan.get("action") ==
                ("send" if self.s.mode == "apply" else "retry") and type(plan.get("limit")) is int
                and plan["limit"] == 1 and not plan.get("regenerate") and len(entries) == 1
                and entries[0].get("process") is True and entries[0].get("source") == SOURCE
                and entries[0].get("target") == original["entries"][0]["target"]
                and entries[0].get("fingerprint") == self.sheet.selection["fingerprint"],
                "Only the original bound one-entry action is authorized.")
        return super().execute(plan)

    def resolve_email(self, identifier, outcome):
        known = original_action(self.s, self.sheet.selection)
        require(self.s.mode == "resolve" and known and identifier == SOURCE+":"+known[0],
                "Only the explicitly selected one-entry delivery may be resolved.")
        return super().resolve_email(identifier, outcome)


@diagnostic_operation("single_entry_execution", source=SOURCE)
def execute(settings, path, mode, *, reader=None, renderer=None, mailer_factory=SMTPMailer,
            write_transport=None, reload_settings=None):
    policy = read_policy(settings)
    selection = load_selection(settings, path, policy)
    scoped = SingleSettings(settings, policy, mode, reload_settings)
    scoped.require_write()  # Disabled email fails before Google, identity journals or rendering.
    reader = reader or read_adapter(settings)
    sheet = SingleSheet(scoped, reader, selection, write_transport)
    rows = sheet.rows(); row = fixed_row(rows, selection)
    known = original_action(settings, selection)
    if not known:
        require(mode == "apply", "No existing one-entry delivery attempt can be retried or resolved.")
        assert_unissued(row, rows, settings.event_id); check_normal_reservations(settings, row)
        require(row.data == selection["selected_data"] and reader.capture(SOURCE, NUMBER) == selection["original"],
                "Prepared approvals, original cells or history changed; prepare again while disabled.")
    plan = deepcopy(selection["plan"])
    if mode == "retry":
        require(row.get("email_status") not in ("SENDING", "AMBIGUOUS", "SENT")
                and row.get("state") not in ("EMAIL_REVIEW", "SENT"), "Uncertain or completed delivery cannot be retried without review.")
        require(not reasons(row, rows, settings.event_id, "retry"), "Approval, identity or registration checks block retry.")
        plan["action"] = "retry"
        plan["plan_id"] = str(uuid.uuid5(uuid.UUID(plan["plan_id"]), "explicit-single-entry-retry"))
        plan["entries"][0]["record_id"] = known[0]

    def before_submit(message, address, name, pid, card_path, mid, tier):
        scoped.require_email()
        current_rows = sheet.rows(); current = fixed_row(current_rows, selection)
        require((current.get("state"), current.get("email_status"), current.get("message_id")) ==
                ("PROCESSING", "SENDING", mid), "Durable send intent changed; no SMTP submission.")
        blocked = [b for b in reasons(current, current_rows, settings.event_id, "retry")
                   if b != "uncertain delivery requires staff review"]
        require(not blocked and (address, name, pid, tier) == (current.get("normalized_email"),
                current.get("ticket_name"), current.get("participant_id"), current.get("ticket_tier")),
                "Final approval, recipient, tier or identity check failed.")
        require(current.get("render_status") == "RENDERED" and
                Path(current.get("card_path") or "").resolve() == Path(card_path).resolve(),
                "The recorded badge path or render status changed.")
        require(not is_test_token(current.get("ticket_token")), "Invalid test tokens cannot be sent as issued tickets.")
        processor.journal.claim(scoped, current)
        with diagnostic_context("single_entry_badge_verification", SOURCE):
            verify_render(settings, card_path, name, tier, current.get("ticket_token"))
        images = [p for p in message.walk() if p.get_content_type() == "image/png"]
        require(len(images) == 2 and all(p.get_payload(decode=True) == Path(card_path).read_bytes() for p in images),
                "Inline badge or attachment changed after message construction.")

    mailer = mailer_factory(scoped, before_submit=before_submit) if mode != "resolve" else None
    if mailer is not None and hasattr(mailer,"diagnostic_sink"):
        def record_smtp_stage(stage, error):
            diagnostic = (safe_diagnostic(error,stage,source=SOURCE) if error is not None else
                          {"stage":stage,"source":SOURCE,"exception_class":None,"http_status":None,"google_api_reason":None})
            diagnostic_artifact(path,"smtp-evidence",{"diagnostic":diagnostic,
                "accepted":stage=="single_entry_smtp_accepted","selection_sha256":checksum(path)})
        mailer.diagnostic_sink = record_smtp_stage
    processor = SingleProcessor(scoped, sheet, renderer or CardRenderer(settings), mailer)
    if mode == "resolve":
        return processor  # Caller invokes only the existing exact-ID resolution routine.
    outcomes = processor.execute(plan)
    private_json(Path(path).with_suffix(".result.json"), {"outcomes":outcomes,"source":SOURCE,"row":NUMBER,
                 "general_production_processing":"LOCKED"})
    return outcomes


def _run_command(settings, args):
    if getattr(args,"diagnose",False):
        print(json.dumps(diagnose_existing(settings,args.selection),indent=2))
        return
    if args.prepare:
        report = prepare(settings, args.selection)
        print(json.dumps(report, indent=2))
        return 1 if report["blockers"] else 0
    if args.resolve:
        processor = execute(settings, args.selection, "resolve")
        selection = load_selection(settings, args.selection, read_policy(settings))
        known = original_action(settings, selection)
        require(known is not None, "No one-entry delivery attempt exists to resolve.")
        processor.resolve_email(SOURCE+":"+known[0], args.resolve)
        print("Single-entry review outcome recorded; no SMTP connection or email.")
        return
    require(args.authorize_send, "Use --authorize-send explicitly for this one-entry apply/retry.")
    mode = "retry" if args.retry else "apply"
    result = execute(settings, args.selection, mode,
                     reload_settings=lambda: load_settings("production", settings.root, single_entry=True))
    print(json.dumps({"outcomes":result,"source":SOURCE,"row":NUMBER,"general_production_processing":"LOCKED"}))


def run_command(settings, args):
    try:
        return _run_command(settings, args)
    except Exception as exc:
        diagnostic = safe_diagnostic(exc, "single_entry_execution", source=SOURCE)
        # An append-only diagnostic artifact, never a replacement selection,
        # action, identity, Message-ID or delivery-history record.
        try:
            path = private_selection_path(settings, args.selection)
            report_path = path.with_name(path.stem+".diagnostic-"+str(time.time_ns())+".json")
            private_json(report_path, {"diagnostic":diagnostic,"email_enabled":settings.email_enabled,
                                      "general_production_processing":"LOCKED"})
        except Exception:
            pass
        print(json.dumps({"status":"STOPPED","diagnostic":diagnostic,
                          "general_production_processing":"LOCKED"}, sort_keys=True))
        return 1
