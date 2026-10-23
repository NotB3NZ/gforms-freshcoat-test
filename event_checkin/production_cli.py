"""Production command surface: local fixture execution, explicit read-only live previews."""
import json
import uuid
from dataclasses import replace
from pathlib import Path

from .cards import CardRenderer
from .config import ConfigurationError, production_sources
from .mailer import SMTPMailer
from .production import BatchProcessor, preview, private_json
from .production_sheets import FixtureSheets, ProductionSheets, PRODUCTION_METADATA, source_mapping

COMMANDS = ("production-schema", "production-headers", "production-init-headers", "production-inspect", "production-render-test", "batch-preview", "batch-run", "batch-resolve-email")


def register(commands):
    controlled = commands.add_parser("production-render-test", help="One explicit no-email rendering test with guarded restoration; general production stays locked")
    controlled.add_argument("--source", choices=("regular", "discount_15", "discount_10"))
    controlled.add_argument("--row", type=int, help="Temporary locator; preparation binds exact source/form/approval/tracking values")
    controlled.add_argument("--selection", type=Path, help="Private prepared selection file within production runtime")
    controlled.add_argument("--apply", action="store_true", help="Execute exactly the selected registration, verify and clean up")
    controlled.add_argument("--recover", type=Path, help="Cleanup only from this controlled test's private audit; never rerender")
    commands.add_parser("production-schema", help="Local-only proposal for production tracking columns")
    commands.add_parser("production-headers", help="Read only production tab metadata and headers; never participant rows")
    audit = commands.add_parser("production-inspect", help="Read only approved name/email/tier/approval/status columns; print aggregates only")
    audit.add_argument("--plan", type=Path, help="Private reviewed source/header snapshot; uses the approved initialization plan by default")
    initialize = commands.add_parser("production-init-headers", help="Revalidate the approved row-one plan; participant operations stay locked")
    initialize.add_argument("--plan", type=Path, help="Private reviewed header snapshot; defaults to config/production-header-init.approved.json")
    initialize.add_argument("--apply", action="store_true", help="Apply only the approved grid expansion and 24 row-one headers, then verify")
    inspect = commands.add_parser("batch-preview", help="Private no-send selection preview; save a finite plan for deliberate execution")
    mode = inspect.add_mutually_exclusive_group(required=True)
    mode.add_argument("--fixture", type=Path, help="Synthetic local JSON; no Google or email")
    mode.add_argument("--live", action="store_true", help="Explicitly read real participant details; no Sheet writes or email")
    selection = inspect.add_mutually_exclusive_group(required=True)
    selection.add_argument("--id", dest="ids", action="append", help="Repeat for an exact list; source:R-ID, P-ID, or source:row:N")
    selection.add_argument("--source", choices=("regular", "discount_15", "discount_10"))
    selection.add_argument("--all", dest="all_sources", action="store_true")
    inspect.add_argument("--limit", type=int, default=25, help="Maximum eligible delivery attempts; waiting rows do not consume it")
    inspect.add_argument("--action", choices=("send", "retry", "resend"), default="send")
    inspect.add_argument("--regenerate", action="store_true", help="Regenerate selected cards with the same identities and issued tiers")
    inspect.add_argument("--plan-out", type=Path, help="Private JSON within this profile's runtime directory")
    run = commands.add_parser("batch-run", help="Execute exactly a saved plan and exit; live production remains locked")
    run.add_argument("--plan", type=Path, required=True)
    run.add_argument("--fixture", type=Path, help="Synthetic local fixture; live execution stays locked")
    run.add_argument("--state-out", type=Path, help="Private checkpoint JSON within runtime-production/offline")
    resolve = commands.add_parser("batch-resolve-email", help="Record a staff delivery-review outcome for exactly one ID")
    resolve.add_argument("identifier")
    resolve.add_argument("--outcome", required=True, choices=("sent", "retry"))
    resolve.add_argument("--fixture", type=Path)
    resolve.add_argument("--state-out", type=Path)


class NoEmail:
    def send(self, *args):
        raise ConfigurationError("Fixture execution cannot send email.")


def runtime_path(settings, path):
    path = Path(path).resolve()
    if not path.is_relative_to(settings.runtime.resolve()):
        raise ConfigurationError("Save private batch files inside the selected runtime directory.")
    return path


def run(settings, args):
    if settings.profile != "production":
        raise ConfigurationError("Use --profile production for the separate production batch tools. The test worker is unchanged.")
    if not settings.event_id:
        raise ConfigurationError("Set a separate permanent production EVENT_ID locally before preparing batches.")
    if args.command == "production-render-test":
        from .controlled_render import run_command, ControlledTestBlocked
        try:
            return run_command(settings, args)
        except ControlledTestBlocked as exc:
            raise ConfigurationError(str(exc)) from None
        except Exception as exc:
            # API/render exceptions may contain personal fields or credentials.
            # Recovery lives in runtime-production/controlled-tests/*/audit.json.
            from .diagnostics import safe_diagnostic
            raise ConfigurationError("Controlled rendering test stopped. Safe diagnostic: " +
                                     json.dumps(safe_diagnostic(exc), sort_keys=True) +
                                     ". No email was sent; production remains locked.") from None
    if args.command == "production-init-headers":
        from .header_init import run_command
        return run_command(settings, args)
    if args.command == "production-inspect":
        from .inspection import run_command
        return run_command(settings, args)
    if args.command == "production-schema":
        sources = production_sources(settings)
        for source in sources:
            source_mapping(source)
        print(json.dumps({"sources": [s["id"] for s in sources], "app_owned_columns": list(PRODUCTION_METADATA),
                          "added_beyond_test": ["record_id", "source_id", "price_category", "ticket_tier"],
                          "live_initialization": "LOCKED; append only missing reviewed columns after the human checkpoint"}, indent=2))
        return
    if args.command == "production-headers":
        print(json.dumps(ProductionSheets(settings).schema_plan(), indent=2))
        return
    fixture = getattr(args, "fixture", None)
    plan = None
    artifact_settings = settings
    if fixture:
        # Offline fixtures never share journals/cards with future real production runs.
        artifact_settings = replace(settings, runtime=settings.runtime / "offline", generated=settings.generated / "offline", email_enabled=False)
        sheet = FixtureSheets(json.loads(fixture.read_text()))
        if args.command == "batch-run":
            plan = json.loads(args.plan.read_text())
            session = plan.get("fixture_session")
            if not session or sheet.data.get("session_id", session) != session:
                raise ConfigurationError("Fixture session does not match this saved plan; create a fresh preview using the saved checkpoint.")
        else:
            session = sheet.data.get("session_id") or str(uuid.uuid4())
        try:
            if str(uuid.UUID(session)) != session:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise ConfigurationError("Invalid local fixture session; create a fresh synthetic preview.") from None
        sheet.data["session_id"] = session
        settings = replace(artifact_settings, runtime=artifact_settings.runtime / session,
                           generated=artifact_settings.generated / session)
    else:
        if args.command != "batch-preview":
            settings.require_operational()  # Block before Google, rendering or SMTP adapters.
        sheet = ProductionSheets(settings, writable=args.command != "batch-preview")
    if args.command == "batch-preview":
        report, plan = preview(settings, sheet, ids=args.ids, source=args.source, all_sources=args.all_sources,
                               limit=args.limit, action=args.action, regenerate=args.regenerate)
        if fixture:
            plan["fixture_session"] = session
        print(json.dumps({"private_preview": report, "delivery_attempts": sum(e["process"] for e in plan["entries"]),
                          "waiting_snapshot_entries": sum(not e["process"] for e in plan["entries"])}, indent=2, ensure_ascii=False))
        if args.plan_out:
            path = runtime_path(artifact_settings, args.plan_out)
            if path.exists():
                raise ConfigurationError("Choose a new plan filename; existing batch plans are preserved.")
            private_json(path, plan)
            print("Private finite plan saved: " + str(path))
        return
    state_path, persist = None, None
    if fixture:
        state_path = runtime_path(artifact_settings, args.state_out or artifact_settings.runtime / "fixture-state.json")
        if state_path.exists() and fixture.resolve() != state_path:
            raise ConfigurationError("Resume using the saved --fixture checkpoint as input, or choose a new --state-out path. Existing fixture state was preserved.")
        persist = lambda: private_json(state_path, sheet.data)
    mailer = NoEmail() if fixture or args.command == "batch-resolve-email" else SMTPMailer(settings)
    service = BatchProcessor(settings, sheet, CardRenderer(settings), mailer, persist=persist)
    if args.command == "batch-resolve-email":
        service.resolve_email(args.identifier, args.outcome)
        print("Exact registration review outcome saved. No email sent.")
    else:
        plan = plan or json.loads(args.plan.read_text())
        outcomes = service.execute(plan)
        print("Finite batch complete: " + json.dumps({result: outcomes.count(result) for result in set(outcomes)}, sort_keys=True))
    if state_path:
        persist()
        print("Private fixture checkpoint: " + str(state_path))
