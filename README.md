# Event registration and QR check-in

This project connects Google Form response Sheets to personalized FreshCoat cards, opaque QR tickets, email delivery, and a staff check-in server. The original single-source **test** workflow is preserved. The new **production** service handles three sources with four mandatory staff approvals and finite, exact-selection batches.

**Current boundary:** local implementation and offline verification are complete. Production participant metadata writes, ticket issuance, recovery actions and email remain locked. The separately approved `production-init-headers` command is limited to the reviewed row-one header ranges, which were initialized and read back successfully on 2026-10-21. No switch or CLI flag bypasses that checkpoint. `.env.production` stays email-disabled. Do not replace the lock with the old single-source worker.

## Separate test and production profiles

Run commands from this project directory using the working environment:

```sh
.venv/bin/python -m event_checkin --profile test profile-info
.venv/bin/python -m event_checkin --profile production profile-info
```

`test` is the default; `EVENT_CHECKIN_PROFILE` sets a default and the CLI flag wins. Settings come exclusively from `.env.test` or `.env.production`. The preserved `.env` is a legacy test fallback only when `.env.test` is absent. Generic shell settings and dotenv interpolation cannot copy Sheet or SMTP credentials across profiles.

| Setting | Test | Production |
| --- | --- | --- |
| Settings | `.env.test` | `.env.production` |
| History / journals | existing `runtime/` | `runtime-production/` |
| Cards | existing `generated/` | `generated-production/` |
| Template | `templates/card.coat` | `templates/event-badgev2.coat` |
| Sources | original single Sheet | private `config/sources.production.json` |
| Execution | original worker | finite saved batches; live execution locked |

Production offline fixtures use **additional** `offline/` subdirectories under its runtime and card directories. Each synthetic demonstration has its own session directory for journals/cards, retained in its plan and checkpoint. Fixture journals never mix with future live records or other demonstrations. Original environments, secrets, test history, source archives and populated settings were preserved. Private settings, manifests, journals, plans and cards remain ignored. Fresh clones use the tracked `.env.*.example` files and `config/sources.production.example.json`; fill private values locally.

The loader rejects shared event IDs, overlapping profile storage, a test profile targeting a configured production Sheet, and enabled production email. The prepared event ID is `aws-scd-mindanao-2026`; confirm it at the next checkpoint and keep it permanent after ticket issuance. Organization SMTP credentials are deferred and cannot inherit personal test credentials.

## Try the production workflow entirely offline

These commands use only synthetic registrations. They never connect to Google or SMTP. The batch renders the real v2 template, so Bun and the existing FreshCoat dependencies are needed.

```sh
.venv/bin/python -m unittest discover -v
.venv/bin/python -m event_checkin --profile production check-config
.venv/bin/python -m event_checkin --profile production production-schema
.venv/bin/python -m event_checkin --profile production check-template
.venv/bin/python -m event_checkin --profile production preview --tier Pro

# Inspect recipients and save a finite selection. Choose a new plan filename each time.
.venv/bin/python -m event_checkin --profile production batch-preview \
  --fixture examples/production-registrations.json --all --limit 2 \
  --plan-out runtime-production/offline/demo-plan.json

# Deliberate local execution; renders two synthetic cards, records one waiting row, exits.
.venv/bin/python -m event_checkin --profile production batch-run \
  --fixture examples/production-registrations.json \
  --plan runtime-production/offline/demo-plan.json \
  --state-out runtime-production/offline/demo-state.json
```

The preview shows each selected name/email, original raw tier, `mapped_tier`, price category, each approval, sent status, blocking reasons, and `in_batch`. It never prints tokens. Preview output is private when using real data. Only `in_batch=true` entries can be rendered/sent by the saved plan. `--limit` caps eligible delivery attempts; waiting registrations do not consume it. Waiting rows in that finite selected snapshot may receive app-owned record IDs and `WAITING_APPROVAL`, without a participant ID, QR token, or ACTIVE ticket. This metadata work can exceed the delivery limit, but never expands beyond the saved snapshot. Eligible rows beyond the limit are untouched.

The synthetic batch saves a private checkpoint after every metadata change. **For every later preview or run, use the saved state as `--fixture`, not the original sample.** Re-running the same plan with that state cannot resend completed entries. The CLI refuses to overwrite existing plans or replace a checkpoint with the original fixture. A different `--state-out` starts a separate local demonstration; use a new plan too. Files are mode 0600 in private, ignored runtime directories.

```sh
# Safely resume the same finite local batch if it stopped:
.venv/bin/python -m event_checkin --profile production batch-run \
  --fixture runtime-production/offline/demo-state.json \
  --plan runtime-production/offline/demo-plan.json \
  --state-out runtime-production/offline/demo-state.json

# Inspect one source in the saved local state:
.venv/bin/python -m event_checkin --profile production batch-preview \
  --fixture runtime-production/offline/demo-state.json --source discount_15
```

Use a fresh plan after later approvals or newly submitted registrations. An entry that was waiting when selected cannot become an additional delivery inside that saved plan. A corrected recipient/name/tier invalidates an old selection. Interrupted rendering and known SMTP rejection support an explicit retry selection; uncertain email requires staff review first.

## Approved production header initialization

The event ID and the 24 tracking headers have been approved. Live header checks
confirmed regular `X1:AU1` and both discounts `V1:AS1`, after correction of the
15% tier header. A private snapshot of every original header, mapping, tab ID and
spreadsheet ID is in ignored `config/production-header-init.approved.json`.
Never commit or replace this snapshot to bypass a mismatch.

```sh
# Validate the exact approved snapshot and destinations; no Google writes:
.venv/bin/python -m event_checkin --profile production production-init-headers

# Authorized header initialization only; never processes registrations:
.venv/bin/python -m event_checkin --profile production production-init-headers --apply
```

This command requires the permanent event ID, disabled email and the participant
processing lock. Before any write it validates all three sources against the
original snapshot, then repeats that validation before each Sheet request. A
formula in a destination is treated as content even if it displays a blank.
Unexpected, partial, moved, reordered or extra headers stop the command.

Each Sheet receives one atomic batch containing only a necessary increase to
`gridProperties.columnCount` and `userEnteredValue` writes to the approved 24
cells in row 1. Formatting, notes, data validation and all other fields are
untouched. Reads are restricted to spreadsheet/tab properties and row 1. There
is no participant adapter, renderer or SMTP client in this path.

The command reads headers back after each write and checks all sources again at
the end. An already complete, exact header set is left unchanged. If a request
fails or its response is lost, stop participant operations and rerun this same
command to reconcile; completed Sheets are skipped. The three spreadsheets do
not share a transaction, so a failure may leave some complete and some pending.
Do not sort or edit headers while initialization runs: Sheets has no conditional
cell-write operation to eliminate edits in the final read/write interval.

This is a narrow exception to the production lock. It does not enable legacy
`init-sheet`, batches, queueing, ticket issuance, rendering, check-in or email.
The next checkpoint after verified initialization is a separately authorized
read-only inspection of real tier values/registration data and a review of the
first controlled issuance plan. No participant operation is authorized by header
initialization alone.

## Read-only aggregate production inspection

After explicit authorization to inspect participant fields, run:

```sh
.venv/bin/python -m event_checkin --profile production production-inspect
```

This separate command uses a read-only Sheets scope. It checks the initialized
reviewed schema and reads only name, email, raw ticket tier, the four mandatory
approvals, and `state`, `render_status`, `email_status`, `ticket_status`. It does
not fetch tokens, IDs, uploaded documents, payment references, other form answers
or frozen recipient data. It never uses the private per-recipient batch preview.

Output contains aggregate registration/contact/approval/status counts, exact
safe ticket-label counts, approved mapped tiers and duplicate-group counts.
Names and normalized emails are compared only in memory and are never printed
or saved. Unexpected free text in tier/status fields is withheld rather than
risk exposing a misplaced name, email or token. Exceptions are sanitized.
Inspection and production eligibility use the same exact source-specific resolver; neither infers aliases or strips price suffixes.

Registration counts include rows with any name, email, tier or processing state;
checkbox-only empty rows are excluded, so prefilled unchecked boxes do not inflate
totals. A completely blank submission cannot be distinguished from an empty row
without additional authorized fields. Email validation checks syntax, not inbox
ownership/deliverability; comparisons trim and case-fold without merging Gmail
dots or plus tags. Candidate counts exclude any duplicate email and any existing
processing status. A live issuance test still requires separate approval.

## Exact selection, retries, and resends

`batch-preview` requires exactly one selection scope:

| Scope | Flags |
| --- | --- |
| One entry | `--id regular:R-REPLACE` |
| Explicit list | `--id regular:R-FIRST --id discount_15:R-SECOND` |
| One source | `--source regular` (also `discount_15` or `discount_10`) |
| Event-wide | `--all` |

A `record_id` (`R-…`) identifies a registration before any ticket exists. A `participant_id` (`P-…`) identifies an issued ticket and is also accepted when unique; it can be source-qualified. IDs survive sorting while stopped. A new row without metadata appears as `regular:row:2`, for example. This is a temporary locator, not a permanent identity: a plan binds its source and recipient/name/tier snapshot and rejects changed/moved records. Once a permanent record ID is saved, use that ID. Missing, ambiguous or repeated targets fail; selection never falls through to someone else.

Normal `--action send` excludes already sent and failed jobs. Use `--action retry` for `READY`, `RENDER_ERROR`, `EMAIL_ERROR` or interrupted processing, and save a new plan. Use `--action resend` for an intentional second delivery of a sent ticket. A resend must be selected explicitly and receives a new message ID; the same resend plan cannot deliver twice. `--regenerate` redraws cards for that selection using the same participant IDs, tokens and frozen tiers. Cancelled/unknown ticket statuses remain blocked.

An `EMAIL_REVIEW` result, or interrupted `PROCESSING`/`SENDING` intent, means SMTP may have accepted a message. Review provider logs using its saved `message_id`, then record one exact outcome with `batch-resolve-email SOURCE:R-ID --outcome sent|retry`. Fixture demonstrations additionally require `--fixture` and may specify `--state-out`. This command sends nothing. A retry outcome checks current approvals and frozen details, records a known retryable job, and still requires a separate `--action retry` preview/run. Live resolution remains locked. SMTP cannot guarantee exactly-once delivery; neither a preview nor a restart resolves an uncertain send.

## Production approvals and duplicate policy

The three sources retain their own source ID and price category. Tier is a separate field. The existing exact canonical values `Lite`, `Regular`, and `Pro` remain accepted. The user also approved exactly the source-specific aliases listed below. The renderer binds `displayName` and lower-case `ticketTier`. It rejects the unused capitalized `TicketTier` declaration as unbound. Python still generates the opaque-token QR at x=400/y=450/size=400 on the 1200×900 card. Both design archives remain unchanged.

| Source | Exact original Sheet label | Canonical tier |
| --- | --- | --- |
| `regular` | `Lite (₱199)` | `Lite` |
| `discount_15` | `Lite (₱170)` | `Lite` |
| `discount_15` | `Pro (₱297)` | `Pro` |

`discount_10` has no approved price-suffixed aliases. Matching is exact: different
case, whitespace, price, source or wording is not accepted. The shared resolver
is in `event_checkin/tiers.py`; there is no generic price-suffix stripping or
source-category-to-tier inference. Unknown labels continue to require review.

The original Sheet label stays unchanged. Eligibility, duplicate comparisons and
future authorized ticket metadata/rendering use the canonical tier, while the
source's price category remains a separate value. Selection fingerprints still
bind the original raw label, so editing it invalidates an existing selection even
if the edited label would map to the same tier. Aliases cannot bypass any staff
approval, duplicate conflict, cancellation, recipient-change or delivery-review
rule. Their local implementation does not enable live processing.

Every issuance, retry and resend requires **all four** current staff approvals:

| Required exact header | Regular | Both discounts |
| --- | --- | --- |
| `Confirmation` | A | A |
| `Reference Number Matched?` | U | S |
| `Payment price match the price? (Discount/Regular)` | V | T |
| `Confirm if payment is received in our accont` | W | U |

The spelling `accont` is deliberate. Exact labels and source-specific positions are validated; moved, missing or unexpected duplicate headers stop the batch. The [explicitly reviewed duplicate email-header positions](docs/REVIEWED_PRODUCTION_HEADERS.md) are the sole production exception, with column C retained as the recipient source. Only boolean `true` or the exact Sheets string `TRUE` qualifies. Blank, `FALSE`, numeric 1, malformed or unreadable values block. Staff review payment first and use Confirmation as their final release switch. The app never sets any staff checkbox, verifies a bank transaction, or downloads uploaded ID/payment-proof files.

All three sources must be accessible even for a targeted operation, so duplicate detection fails closed. An unapproved earlier submission does not claim the email over a later approved one. Equivalent approved duplicates have one canonical registration in source/Sheet order. Conflicting approved name, tier or price details require review, including conflicts appearing during processing. An existing issued canonical identity is retained; a new submission cannot mint a replacement. Resolve conflicts by reviewing the original registrations and approvals, not by deleting tokens or forcing retries.

Recipient email is trimmed/case-folded; Gmail dots and plus tags are not combined. Once issued, the participant ID, token, recipient, name, tier and source/category are frozen. Form edits do not reroute a retry: they block for review. Durable local reservations preserve identities across uncertain Sheet writes. Back up `production-batches.sqlite3` together with the Sheets; it contains private claims and per-plan recovery state.

Processing takes one fixed selection and exits. It discovers accumulated rows directly, without requiring an Apps Script submission trigger. No production trigger installation is needed. The existing test trigger/worker stays separate. Production reads are paced conservatively across sources; API failures stop the run rather than silently skipping a source. Processes must share the same local runtime directory. Stop processing before sorting/deleting/editing rows: Sheets offers no conditional update or transaction spanning staff edits and SMTP, so a final approval read cannot eliminate an edit occurring after that read.

## Read-only production checks and the next human checkpoint

`production-schema` is local-only. The following separate command **contacts Google but requests only spreadsheet/tab metadata and row-one headers**, with a read-only Sheets scope:

```sh
.venv/bin/python -m event_checkin --profile production production-headers
```

It reports the missing app-owned columns for every source, without retrieving participant rows or writing cells. Header-only live checks and the separately approved initialization were completed on 2026-10-21. All 24 headers now exist in the approved positions; the scoped initializer checks the exact original snapshot before any future action.

`batch-preview --live` is an **operator-requested private read of real participant data**. It reads only mapped name/email/tier, four approvals, and app metadata, never file contents. It does not initialize, queue, render or send. It was implemented and tested with fakes, and was not invoked against real rows in this phase. An example for a later authorized inspection is:

```sh
.venv/bin/python -m event_checkin --profile production batch-preview \
  --live --all --limit 10 --plan-out runtime-production/review-plan.json
```

Live `batch-run --plan runtime-production/review-plan.json` remains **locked**, as do legacy production `worker`, `init-sheet`, queue/retry/resend, check-in/scanner and email commands. There is no `--force` option. The general live adapter remains locked; only the separately approved `production-init-headers` path can initialize its fixed header ranges. These commands will not begin working merely because a plan exists or staff have approved rows.

The separately scoped `production-render-test` command supports one explicitly
selected, unissued registration and mandatory cleanup, with `EMAIL_ENABLED=false`.
It does not unlock batches or sending. Prepare a new private selection with
`--source SOURCE --row ROW --selection runtime-production/selection.json`.
Preparation validates every source and all four approvals, exact tier mapping,
duplicate policy, existing processing history and local reservations, then saves
the original typed tracking cells and selected registration privately. Row numbers
are locators only; any changed selection stops execution.

Only after the operator has authorized that exact test, use the same command with
`--selection runtime-production/selection.json --apply`. Each selection is usable
for one attempt. The test identity stays `CANCELLED` and its `INVALID_TEST_` QR is
permanently rejected by admission and normal production processing. Its reservation
is isolated from both normal identity journals. The current template renders the
personalized card; verification decodes the QR and compares name/tier text pixels
against a separate reference with the expected literal text. The template is
unchanged. The archived preview has a prominent invalid-test banner.

Every write intent and original value is saved in a private
`runtime-production/controlled-tests/RUN_ID/audit.json` before the corresponding
write. Only 17 fixed tracking fields may change; Form answers, approvals,
attendance and delivery history are outside the write scope. Cleanup restores
only an exact expected snapshot, preserves formulas/value types, verifies readback
and retires the isolated reservation. Lost responses are reconciled by reading
back, without blindly repeating writes. Concurrent edits or partial writes leave
the recovery audit intact and stop restoration. Sheets has no compare-and-set
write API; immediate rereads and local locks narrow the read/write gap, but cannot
prevent a human edit in that gap. Keep the selected row and schema stable during
the test. `production-render-test --recover PATH_TO_AUDIT` performs cleanup only;
it never reassigns an identity, rerenders or sends. An archived test QR remains
invalid even if cleanup needs review. Private audit and card paths are printed;
participant values and tokens are never printed by this command.

Unexpected controlled-preview errors report only a bounded stage, trusted local
function/line, underlying exception class, HTTP status and allowlisted Google API
reason. Suppressed exception contexts are inspected without printing exception
messages, response bodies or request payloads. Original-header drift in **any**
source still blocks preparation before participant reads; diagnostic reporting
does not change or approve the saved schema.

The event ID and exact row-one append ranges have now been explicitly approved after live header validation; see [docs/PRODUCTION_CHECKPOINT.md](docs/PRODUCTION_CHECKPOINT.md). Only `production-init-headers --apply` may perform that scoped initialization. Existing form/staff columns must never be renamed, reordered or overwritten. **Next checkpoint after verified initialization:** separately authorize private participant/tier inspection and review a first bounded issuance plan. Participant writes and organization sending remain locked. Final card/email HTML design, deployment and camera work remain deferred.

The older setup sections below describe the original single-source **test** workflow. Use `.env.test` for their settings.

## What was preserved and verified

- `templates/card.coat`, `freshcoat/`, the original `.venv/`, existing generated files, and service-account file are preserved.
- The actual card is **1200 × 900**. Its text layer uses `{{displayName}}`, and `displayName` is declared in the field schema. Renaming a layer alone would not bind a field.
- The default QR rectangle is **x=400, y=450, size=400**, matching the reserved black square below the name. The QR includes a white quiet zone of at least four modules. Rendering refuses coordinates that intersect a text layer or fall outside the card.
- FreshCoat renders a temporary copy with the name text set to shrink within its existing box. Layer positions, colors, font choice, and the source archive are preserved. Embedded fonts are reused. If you later introduce external relative assets, bundle them into the `.coat` archive before rendering.
- Local tests render the real template with different names, confirm name pixels change, preserve the event band, test a long name, and decode the generated QR with OpenCV.
- The final production badge's separate [long-name and Google Sans verification](docs/FINAL_BADGE_CHECKS.md) covers 160-character names, pinned local 400/700 faces, fallback rejection and synthetic-only preview reproduction without changing the installed badge or legacy test template.
- `.gitignore` covers credentials, `.env.test` variants, environments, cards, scanner pending requests, and the server journal. Examples remain trackable. Coverage is tested in a temporary Git repository independently of local Git state. `.env.example` contains blank Sheet settings; enter live settings only in your ignored `.env.test`.

## Clone this project

FreshCoat is a Git submodule pinned to the version used by this implementation. Clone both repositories with:

```sh
git clone --recurse-submodules https://github.com/NotB3NZ/gforms-freshcoat-test.git
cd gforms-freshcoat-test
```

If you already cloned without `--recurse-submodules`, run `git submodule update --init --recursive` from the project root. Install FreshCoat and Python dependencies using the commands below, adjusting the project path to your clone. Credentials, `.env.test`, generated cards, runtime journals, and Python environments are intentionally excluded from Git; configure them locally.

## Working directory and Python

Run every Python command below from:

```sh
cd /Users/benzdecastro/Documents/Projects/test_gforms/event-checkin
```

Your existing `.venv/bin/python` has the dependencies and runs the local tests. It is Python 3.9 with LibreSSL and emits compatibility warnings from Google authentication and urllib3. For account and event use, prefer the installed Python 3.12 with OpenSSL. A separate `.venv312` exists but still lacks application dependencies (including FastAPI). Its migration remains incomplete; the production implementation used the working environment. The original environment was not modified.

When ready to prepare the newer environment, run in your own terminal:

```sh
cd /Users/benzdecastro/Documents/Projects/test_gforms/event-checkin
/opt/homebrew/bin/python3.12 -m venv .venv312
.venv312/bin/python -m pip install -r requirements.txt
.venv312/bin/python -m pip check
.venv312/bin/python -m unittest discover -v
```

Success means `No broken requirements found` and tests ending in `OK`. The real-template test needs Bun and `freshcoat/node_modules`; it reports a skip if either is missing. After this succeeds, substitute `.venv312/bin/python` for `.venv/bin/python` in the remaining commands. On another computer, use your Python 3.12 executable instead of the Homebrew path.

FreshCoat dependencies are already present here. To reinstall them if needed, the working directory is different:

```sh
cd /Users/benzdecastro/Documents/Projects/test_gforms/event-checkin/freshcoat
"$HOME/.bun/bin/bun" install --frozen-lockfile
```

The Python renderer finds Bun on PATH or at `~/.bun/bin/bun`. Set `BUN_BIN` privately if it lives elsewhere. It invokes the existing `freshcoat/packages/cli/src/bin.ts` using a list of subprocess arguments, never a shell command.

## Safe local commands

These do not use Google, send email, or open the camera:

```sh
cd /Users/benzdecastro/Documents/Projects/test_gforms/event-checkin
.venv/bin/python -m unittest discover -v
.venv/bin/python -m event_checkin check-template
.venv/bin/python -m event_checkin preview
open generated/LOCAL-PREVIEW/card.png
```

The preview QR is fake and cannot admit a registered participant. Treat real cards and ticket tokens as private.

## Checkpoint 1 — enter local Sheet settings, then stop

Do this first. Do not install the trigger or enter email credentials yet.

1. Create your private configuration if it does not exist:

   ```sh
   cd /Users/benzdecastro/Documents/Projects/test_gforms/event-checkin
   test -f .env.test || cp .env.test.example .env.test
   chmod 600 .env.test
   open -e .env.test
   ```

2. Fill `SPREADSHEET_ID` with the part between `/d/` and `/edit` in your Sheet URL. Fill `SHEET_TAB` with the exact response tab title at the bottom of the Sheet. Choose a permanent `EVENT_ID`, such as `aws-student-day-2026`. Keep this ID the same for the whole event.
3. Set `NAME_HEADER` and `EMAIL_HEADER` to the exact cells in row 1 containing participant names and email addresses. Keep `EMAIL_ENABLED=false` and leave SMTP/staff values empty for now. Save the file. Do not paste credentials or the file into chat.
4. Verify local configuration without contacting Google:

   ```sh
   .venv/bin/python -m event_checkin check-config
   ```

Success prints `Local Sheet settings are present. Google account access has not been tested.` Tell your assistant that checkpoint 1 is done, then wait before dependent setup.

`EVENT_TIMEZONE` controls worker and attendance timestamps using an IANA timezone; the default is `Asia/Manila`. `HEADERS_FILE` optionally points to a JSON mapping of logical field names to your actual Sheet headers. Copy `config/headers.example.json` to `config/headers.json` and include only the overrides you need. Every logical name is listed in `event_checkin/config.py`. Match metadata overrides in Apps Script too. Never change metadata names while jobs are active.

## Later checkpoint — share and verify the Sheet

Proceed with your assistant after checkpoint 1.

1. Open the service-account JSON locally and find only its `client_email` value. In the Google Sheet's **Share** dialog, give that service account **Editor** access. Keep the JSON/private key private; it is used only to authenticate the Google API client.
2. With the correct config, add metadata headers without queuing or emailing old responses:

   ```sh
   cd /Users/benzdecastro/Documents/Projects/test_gforms/event-checkin
   .venv/bin/python -m event_checkin init-sheet
   .venv/bin/python -m event_checkin check-sheet
   ```

3. Success means new metadata headers appear after your existing headers, and `check-sheet` reports connection/header verification and a row count. It never prints participant data. A 403/404 requires checking the Sheet ID, sharing, and API enablement. Do not disclose the raw credential or API response in chat.

The client opens the configured spreadsheet ID and exact tab directly. It does not search Drive or infer that a prior setup step worked.

## Later checkpoint — install the Apps Script trigger

The complete local script is `apps-script/form-submit.gs`. It is prepared for `eventId='tech-event-2026'` and `sheetTab='Responses'`, matching the original test configuration; no metadata header overrides are currently configured. **Editing the local `.gs` file does not install or update anything in Google.**

1. Open your registration spreadsheet and choose **Extensions → Apps Script**.
2. Inspect the existing files first. If this event registration trigger is already present, replace only its dedicated file. Otherwise, use **+ → Script** to create `RegistrationTrigger.gs` and paste the entire local script there. Do not replace or delete unrelated code. All `.gs` files share global names: if unrelated code defines the same constants/functions as this script, stop and report the conflicting names before pasting. Do not keep two copies of this trigger in the project.
3. Save the project. Select **setupTrigger** in the function dropdown and click **Run**. Complete Google's sign-in/authorization yourself. Only one account should install it; re-running setup removes that installer's duplicate registration triggers while preserving unrelated handlers.
4. Open **Triggers** (the clock icon). Verify exactly one trigger owned by your account with function `onRegistrationSubmit`, event source **From spreadsheet**, and event type **On form submit**. Apps Script **Executions** should show that `setupTrigger` completed successfully.
5. Submit a **new** test registration through the actual linked Google Form. Do not type a row into the spreadsheet or manually run `onRegistrationSubmit`; neither supplies the real submission event.
6. Check the new row on `Responses`: `event_id=tech-event-2026`, `state=PENDING`, and a populated `updated_at`. Confirm a successful `onRegistrationSubmit` run under **Executions**, then report the result to your assistant and stop. Keep email disabled and leave the worker stopped for this checkpoint. The existing response should remain unchanged.

The trigger sets `PENDING` last, never assigns tokens, and never resets a row already being processed. Old responses are left alone. If you explicitly want to queue old unprocessed responses:

```sh
.venv/bin/python -m event_checkin queue-unprocessed
```

This queues rows without an identity and with an empty state or corrected `INVALID` state, for this event or no assigned event. It does not send anything itself. Stop the worker first when changing setup.

## Worker and processing states

With email disabled, process one queued row:

```sh
cd /Users/benzdecastro/Documents/Projects/test_gforms/event-checkin
.venv/bin/python -m event_checkin worker --once
```

Success means permanent `participant_id` and `ticket_token` appear with `ticket_status=ACTIVE`, then `render_status=RENDERED`, `email_status=DISABLED`, and `state=READY`. The private card path is recorded. For periodic processing, run this in its own terminal and leave it open:

```sh
.venv/bin/python -m event_checkin worker
```

Ctrl+C stops polling. The default handles one row per pass and waits 30 seconds. `WORKER_BATCH_SIZE` and `WORKER_POLL_SECONDS` are configurable; conservative settings reduce Sheets quota pressure. Check-in and worker traffic share that API quota. Errors stop the current pass and later passes re-read saved state.

| State | Meaning / next action |
| --- | --- |
| `PENDING` | Queued for processing. |
| `PROCESSING` | Identity saved; rendering/email may be underway. Automatically recovered next pass. |
| `READY` | Card rendered; email disabled. Retry after email is authorized/configured. |
| `RENDER_ERROR` | Rendering failed. Email was not attempted for this render; fix the cause and retry. |
| `EMAIL_ERROR` | SMTP connection failed or message explicitly rejected. Fix the cause and retry. |
| `EMAIL_REVIEW` | Email may have been sent, or the job stopped during a send. Review provider logs before deciding. |
| `SENT` | SMTP accepted the message, or staff confirmed delivery during review. This does not guarantee inbox delivery. |
| `DUPLICATE` | This email already has a canonical registration for this event. No second token/card/email is issued. |
| `INVALID` | Missing/invalid name/email. Correct the response and queue it again. |

The separate `render_status`, `email_status`, attempt counters, and `last_error` identify the failing stage. Error messages are sanitized and never contain tokens, SMTP passwords or credential contents. `message_id` remains stable for retries of an uncertain/failed attempt, but **SMTP does not provide guaranteed exactly-once delivery**.

Emails are normalized with whitespace trimming and case folding. Plus tags and Gmail dot variants are not combined. One canonical ticket is allowed per normalized email per event. An already issued ticket wins even after rows move; otherwise the first response processed in Sheet order wins. Duplicates get their own permanent record ID and `duplicate_of`, but no ticket. Correcting an invalid first response after another registration has won makes the corrected response a duplicate.

An ID is a UUID-based `P-…` value; a row number is only a temporary cell locator. IDs and tokens are first reserved by email/event in the private `runtime/issuance.sqlite3` journal, then saved and confirmed in Sheets before rendering. Even an interrupted or uncertain initial Sheet write reuses that reserved pair. A canonical ticket's ID, token, normalized recipient, and displayed name are frozen before rendering. Retries/resends reuse them. Editing original form cells after issuance does not reroute an existing ticket; stop processing and review such corrections explicitly. Never edit or delete identity, attendance, or email-state cells to force a retry.

Use the actual permanent ID from the Sheet in place of `P-REPLACE`:

```sh
.venv/bin/python -m event_checkin retry P-REPLACE
.venv/bin/python -m event_checkin worker --once
```

If other rows are pending, the worker handles them in order; repeat passes or leave polling running until the selected row is processed.

For `EMAIL_REVIEW`, check the provider's sent-message/log records using the saved `message_id`, then choose one outcome:

```sh
# Delivery confirmed by staff; no new email is sent:
.venv/bin/python -m event_checkin resolve-email P-REPLACE --outcome sent
# Or staff explicitly decides a retry is appropriate, accepting duplicate-delivery risk:
.venv/bin/python -m event_checkin resolve-email P-REPLACE --outcome retry
.venv/bin/python -m event_checkin worker --once
```

An intentional resend of a `SENT` card uses `resend P-REPLACE`, followed by a worker pass. It keeps the original token and attendance status; a used ticket remains used. Duplicate registrations cannot be retried into new tickets.

### Cancel an issued ticket

To invalidate an issued ticket, replace `P-REPLACE` with its permanent participant ID:

```sh
.venv/bin/python -m event_checkin cancel P-REPLACE
```

Success prints `Ticket cancelled. Identity, email history and attendance were preserved.` The Sheet shows `ticket_status=CANCELLED`. New scans reject the ticket, and retries/resends cannot reactivate it. Its identity/token and prior email/attendance records remain intact. Re-registering the same email does not issue a replacement ticket. The command refuses to run during an active local worker pass; stop the polling worker before cancelling. An email already accepted by SMTP cannot be recalled.

Ticket validity is separate from processing `state`: a rendered `ACTIVE` ticket is usable, while `CANCELLED` and unknown status values are rejected. For compatibility, a blank status on an older rendered ticket is treated as active. Run `init-sheet` with processes stopped to append the two new headers (`ticket_status`, `checked_in_by`) to an existing installation. Completed request IDs retain their original response for recovery; cancellation affects new scans, without rewriting historical admission decisions.

Default tracking labels are listed below. `HEADERS_FILE` can map them to the labels you already use; confirm your real Sheet headers at the sharing checkpoint before initializing anything.

| Tracking field | Default Sheet header |
| --- | --- |
| Participant ID | `participant_id` |
| QR Token | `ticket_token` |
| Ticket Status | `ticket_status` |
| Email Status | `email_status` |
| Checked-in Time | `checked_in_at` |
| Checked-in By | `checked_in_by` |
| Last Request ID | `checkin_request_id` |
| Last Error | `last_error` |

## Local HTML confirmation review

The production confirmation can now be built offline with a plain-text alternative, a CID-linked badge and a downloadable PNG. The [integration and visual-review notes](docs/CONFIRMATION_EMAIL_REVIEW.md) describe synthetic Lite, Regular and Pro previews, unchanged source templates, regression results and manual settings for a later separately authorized sending phase. No Google access or SMTP credentials are needed for these local checks; production email remains disabled.

## Later checkpoint — authorize one real email test

Do not run a real send until you identify the test recipient and explicitly authorize the test with your assistant. Stop the polling worker first. Enter SMTP settings privately in `.env.test`: host, port, username, password or provider app password, sender, and `SMTP_SECURITY=starttls` (usually port 587) or `ssl` (usually 465). Plain SMTP is not supported. The service account is not an SMTP password and does not automatically authorize email.

After authorization, set `EMAIL_ENABLED=true` temporarily and send the **fake preview only** to that one approved recipient:

```sh
cd /Users/benzdecastro/Documents/Projects/test_gforms/event-checkin
.venv/bin/python -m event_checkin send-test --to approved-recipient@example.com --authorize-send
```

Replace the example address with the approved recipient. This command never processes pending registrations. Success first means SMTP acceptance; verify the inbox/spam folder and open the image attachment. If the command fails after submission, inspect provider logs before repeating it. Set `EMAIL_ENABLED=false` again while reviewing setup. Enable ongoing registration emails only when you are ready to authorize the worker to deliver to all queued recipients.

## Later checkpoint — staff server and webcam

Create a separate staff credential locally, after `.env.test` exists. This writes the key directly into the ignored file without printing it:

```sh
cd /Users/benzdecastro/Documents/Projects/test_gforms/event-checkin
.venv/bin/python - <<'PY'
import os, secrets
from dotenv import set_key
set_key('.env.test', 'STAFF_API_KEY', secrets.token_urlsafe(32))
os.chmod('.env.test', 0o600)
print('Staff credential saved privately.')
PY
```

Start one central server in a terminal:

```sh
.venv/bin/python -m event_checkin server
```

It binds only `127.0.0.1:8000`. `/health` reports that the process runs, not that Google is reachable. API docs and access logs are disabled. In another terminal on the same computer:

```sh
cd /Users/benzdecastro/Documents/Projects/test_gforms/event-checkin
.venv/bin/python -m event_checkin scanner
```

Set `STAFF_STATION_NAME` in `.env.test` to a recognizable label, such as `Entrance A`. On first admission, `checked_in_by` records this label with the timestamp and request ID in the same Sheet update. Lost-response retries retain the original station. This is a station label supplied by an authenticated scanner, not a verified individual staff identity; scanners currently share one staff credential.

Grant camera permission yourself if macOS requests it. Use `CAMERA_INDEX` for a different camera. Show one real issued QR at a time. Only the **ACCEPTED** result confirms admission. Another request for the same ticket shows **Already checked in — do not admit**; an unknown/cancelled ticket shows **Invalid or cancelled ticket — do not admit**. A network/Google/auth failure shows **Unable to confirm — do not admit; retrying**. `Q` closes the scanner; `R` retries a pending scan.

The scanner suppresses repeated submissions while a QR remains visible; it can submit again after the code is absent for two seconds. A timed-out request is saved in `runtime/scanner-pending.json` and retried with the **same request ID**, even after restarting the scanner. It will not replace an unresolved scan with another person's ticket. Keep the pending file until the outcome is confirmed.

The server requires `X-Staff-Key` on every `/check-in` request; participant tokens are separate secrets and cannot authorize staff requests. A valid rendered ticket becomes usable even when email is disabled, allowing private card testing. Attendance is recorded with the event timezone. Retries of a completed request return its original result; reusing a request ID for a different token is rejected.

## Coordination and operational limits

Use **one central server process and one worker process on the same computer**, sharing the same `.env.test` and `RUNTIME_DIR`. The whole check-in lookup/decision/Sheet write and durable request journal are protected by a thread lock and an OS file lock. Worker Sheet changes use the same lock and update only their own metadata cells. Server and worker singleton locks reject accidental extra local instances.

This does not coordinate independent hosts, containers with different runtime directories, or arbitrary Apps Scripts editing issued tickets. Multiple independent check-in servers/workers require a shared transactional database or distributed coordination before use. Do not use network filesystems for runtime locks/SQLite. This version is deliberately local; remote cameras need an authenticated HTTPS deployment and a separate deployment review. Non-local plain HTTP scanner URLs are refused. Nothing here publishes the system.

Pause both processes before sorting, deleting, or manually changing existing response rows/metadata. Appending normal Form submissions is supported. IDs are looked up afresh between jobs, so sorting while stopped does not change identity; Sheets has no conditional cell-write operation to protect against a person sorting during an API write.

Back up the Sheet and private `runtime/` directory while processes are stopped. Retain both databases for the event: `checkin.sqlite3` stores request results across restarts and uses token hashes, while `issuance.sqlite3` contains reserved IDs, emails and raw ticket tokens needed for recovery. QR-bearing cards, both journals, the Sheet token column, `.env.test`, service credentials, and scanner pending files remain private. Do not clear check-in timestamps or request IDs to allow repeat admission.

## Code map and verification limits

| File | Purpose |
| --- | --- |
| `config.py`, `sheets.py`, `locking.py` | Isolated settings and original test adapter/coordination. |
| `production_sheets.py` | Three-source schema validation, read-only live access, protected metadata initialization/updates, synthetic fixtures. |
| `production.py`, `production_cli.py` | Approval/duplicate policy, private selection plans, finite execution, frozen claims and delivery recovery. |
| `issuance.py`, `cards.py`, `mailer.py`, `worker.py` | Original test workflow, FreshCoat rendering, QR composition and SMTP. |
| `checkin.py`, `server.py`, `scanner.py` | Existing test check-in API and webcam client. |
| `apps-script/form-submit.gs` | Existing test-only submission queue trigger. |

The previous local phase passed all **112 tests**. This header-initialization checkpoint passed **89 selected offline regression tests**, including **15 new initialization tests**, without launching FreshCoat or rendering real cards. Run `.venv/bin/python -m unittest discover -v`. The full suite covers the existing test workflow and production approval combinations, malformed values, header positions/access, later approvals, exact selections, bounded batches, changed recipient/tier/approvals before SMTP, duplicate conflicts, token/tier preservation, explicit resends, uncertain sends and lost-write recovery. Tests use fake Google/SMTP; actual FreshCoat rendering and QR decoding run locally. No account or hardware access is needed. The working Python 3.9 environment emits the known Google-auth/LibreSSL warnings. Python 3.12 still lacks application dependencies; its migration was not attempted in this phase.

The original test workflow was exercised live by the user before this phase. The authorized aggregate-only tier inspection has been completed. Still unverified here: live issuance, API quota/latency under event load, organization SMTP acceptance/inbox delivery, and camera hardware. Participant fields were read only during the explicitly authorized aggregate inspection; no personal details were printed or saved. The local alias implementation makes no live requests, changes no staff approvals and sends no email. The explicitly approved 24 row-one tracking headers were initialized and verified in all three sources; participant operations remain locked. Preserve the remaining checkpoints.
