# FreshCoat staff check-in web application

This is a separate local application in the existing repository. It does not invoke the email CLI, issue tickets, render real badges, or load the existing `.env`, `.env.test`, or `.env.production` by default. No live operation or deployment was performed during implementation.

## Start a synthetic local application

To look up already-issued real tickets without admitting anyone, use the separate
[read-only registration preview](WEB_PREVIEW.md). The synthetic demo below only
contains development participants and will not recognize real issued tickets.

Run from the repository root. Python 3.12+ and Node 20+ are recommended. Keep the working email environment unchanged.

```sh
python3.12 -m venv .venv-web
.venv-web/bin/python -m pip install -r requirements-web.txt
cp .env.web.example .env.web
.venv-web/bin/python -m event_checkin.web.cli password --write
.venv-web/bin/python -m event_checkin.web.cli migrate
.venv-web/bin/python -m event_checkin.web.cli seed
.venv-web/bin/python -m event_checkin.web.cli qr-fixtures
npm --prefix frontend ci
npm --prefix frontend run build
.venv-web/bin/python -m uvicorn event_checkin.web.app:app --host 127.0.0.1 --port 8080 --no-proxy-headers
```

Open **http://localhost:8080**, enter the password you chose, and enter your nickname. Use that exact address: host and Origin checks deliberately reject other addresses. `seed` refuses to replace an existing fixture. To start a separate demo, select new private `WEB_DATABASE_URL` and `WEB_FIXTURE` paths in `.env.web`; do not delete an unresolved attendance database. There is no working default password. The password command saves only a PBKDF2-SHA256 hash (600,000 iterations, random salt) in ignored, mode-0600 `.env.web`.

If this host sets an unwritable Python cache prefix, prepend `PYTHONPYCACHEPREFIX=/private/tmp/freshcoat-pycache` to Python commands. Dependencies require registry access on first installation. The implementation environment had no registry access; verification used already installed/cached dependencies in an isolated `.venv-web`, without modifying the email virtualenv.

The default is **explicit single-server SQLite development mode**, not a database on each station. Three browsers use the same backend and database. SQLite uses WAL, FULL synchronization, foreign keys, `BEGIN IMMEDIATE`, and database uniqueness constraints, including across independent processes. It is a convenient local demonstration on one host. Live mode requires PostgreSQL and HTTPS. Do not put SQLite on a shared/network filesystem or run separate backend databases at each desk.

For local PostgreSQL, put a private `WEB_POSTGRES_PASSWORD` in `.env.web` and change `WEB_DATABASE_URL` to the matching URL shown in `.env.web.example`, then:

```sh
docker compose --env-file .env.web -f compose.web.yml up -d db
.venv-web/bin/python -m event_checkin.web.cli migrate
.venv-web/bin/python -m event_checkin.web.cli seed
```

Use a fresh synthetic fixture path if you already seeded SQLite. The Compose database port is loopback-only, 54329. It uses a named persistent volume; no credentials are checked in. Configure database backups before live use. Run migrations once before starting multiple application processes. The application also runs pending migrations at startup for the simple single-process local setup.

## Try scanning without a camera

The **Scan a QR image instead** control uses the actual browser decoder. Select `.web-runtime/qr/DEVELOPMENT-lite.png`, review Alex Rivera, then cancel: attendance must remain unchanged. Scan it again and explicitly select **Check in**. The result stays visible until **Scan next**. Scan the same image again to see the original check-in time and nickname with admission disabled.

Development QR fixtures include all five canonical tiers, similar names, a long name/email, a prior arrival, cancelled/invalid/unapproved/duplicate records, a reserved controlled-test token, an unknown token, and a URL that must never be navigated to. `examples/web-synthetic.json` and `event_checkin/web/seed.py` are synthetic only. `DEMO_` tokens are rejected in live mode; `INVALID_TEST_` controlled-render tokens are always rejected. No development bypass exists on a production endpoint.

The camera prefers the rear lens and offers device selection after permission is granted. jsQR 1.4.0 is vendored unchanged with its Apache-2.0 license from the project's existing installed FreshCoat dependency. It supports QR decoding without `BarcodeDetector`. An authenticated, size-bounded OpenCV endpoint provides an additional image/frame fallback if the browser decoder is unavailable. Fallback images are processed in memory and never saved or logged. Successful normal browser decoding sends only the opaque token for lookup. Neither path changes attendance.

Camera access requires localhost **on the device running the browser**, or HTTPS. A phone's `localhost` refers to the phone, not your laptop. To test three phones against one laptop, arrange a trusted HTTPS reverse proxy and configure its exact `WEB_ORIGIN`; hosting and the real domain remain undecided. Do not disable TLS validation. Microphone access is never requested. The camera stops during review, navigation, hidden tabs, logout, and page exit. Returning to a hidden tab requires an explicit camera restart.

## Staff operation and recovery

All staff share the same permissions: Scan, Participants, Overview, Activity, and Sync now. There is no administrator role or account table. Password verification creates an expiring server session, then nickname completion is required before participant access. Nicknames are trimmed, validated, rendered with `textContent`, and are **self-declared labels, not verified identities**. A different operator logs out and signs in with their own nickname.

Authentication cookies are random, stored only as SHA-256 digests on the server, host-only, HttpOnly, SameSite=Strict, and Secure on HTTPS. The HTTP exception is restricted to localhost. Sessions expire after 8 hours by default and are invalidated on logout. An independent internal session ID is retained for audit; the cookie secret is never used as an attendance/audit identifier. CSRF tokens plus exact Origin/Host checks protect mutations; login uses the same Origin checks and JSON-only requests. Database-backed throttling limits sign-in attempts per peer and globally. The documented Uvicorn command disables forwarded-client headers; a later trusted proxy setup must be reviewed explicitly.

Reviewing/scanning/searching does not admit anyone. Confirmation creates one durable request ID in browser local storage **before** submitting. Only database IDs, event, request ID, and method are retained, not tokens/names/emails. While the request is unresolved, another participant cannot be confirmed. A timeout says **Unable to confirm — retry** and preserves that ID. Refresh re-reads the original request result; retry reuses the same ID. Do not clear browser storage or switch event/server to escape a pending request. If a session expires, sign in again and reconcile the saved request; any committed result retains its original staff attribution. Successful database responses, not browser optimism or Sheets writes, establish admission.

A second station reviewing the same person receives **Already checked in at … by …** after the first station commits. A new request is a duplicate; replaying the same request returns its original result. Reusing a request ID for another participant, method, or session is rejected. `/api/requests/{id}` permits any signed-in staff member with the known request ID to reconcile an outcome after re-login, without impersonating the original staff session.

## Architecture and field ownership

`event_checkin/web/` is a separate FastAPI application; the existing `server.py`, scanner, check-in service, email commands, adapters and production authorization gates are preserved. Plain HTML/CSS/ES modules in `frontend/` keep the client small. FastAPI serves its built assets and same-origin authenticated APIs. No CDN, frontend framework, Redis, service worker, offline admission, or separate cache service is required.

| Owner | Fields / behavior |
| --- | --- |
| Sheets and existing issuance workflow | Registration details, four approvals, raw/source-specific tier aliases, category, ticket status, render state, permanent record/event/participant/source IDs, frozen issued name/email/tier/token identity, reviewed shared-email and Special recipient exceptions |
| Web database | Attendance timestamp, nickname snapshot, internal staff session reference, QR/manual method, request result, durable attendance export outbox |
| Browser | Temporary review state and one unresolved request's non-secret IDs; never admission authority |

The importer reuses `production.reasons`, `validate_production_identities`, `issued_identity`, exact tier resolution, `apply_approved_groups`, and the production source mappings. Sending/retrying email is a separate decision: admission does not require successful delivery, and email review/error states alone do not invalidate an otherwise approved, rendered, consistent ticket. Invalid, duplicate, cancelled, unrendered, unapproved, inconsistent, unknown-tier, and controlled-test records remain ineligible. An issued identity change, duplicate permanent identity/token, missing previously imported record, missing source, or failed source read rejects the entire snapshot. The last validated snapshot remains available with an error and visible sync age. A form-detail edit against a frozen identity makes the ticket ineligible; changing frozen issued fields themselves requires reconciliation instead of silent remapping.

Source `regular` and tier `Regular` are independent fields. Approved shared-email bookings retain separate participant identities. The app does not impose a new email uniqueness rule. Unissued rows with a stable `record_id` appear as ineligible; rows without this event and a permanent record ID are omitted and counted in sync status. No row number is a database identity. Existing shared-email approvals retain their existing physical-row attestation policy and must be re-reviewed if those source rows move.

Participant lists omit token/digest fields. The database stores a token SHA-256 digest and an issued-identity digest rather than raw ticket tokens. Search uses Unicode-normalized/casefolded name/email values and bounded pagination. API request bodies are not logged. Do not enable proxy/APM request-body capture for lookup or decoder endpoints.

The SQL migrations under `event_checkin/web/migrations/` work with PostgreSQL and SQLite and are tracked in `schema_migrations`. PostgreSQL admission/import transactions lock the selected event row. SQLite uses its transaction write lock. These short transactions revalidate event eligibility and attendance, then commit attendance, request result, and outbox together. The attendance primary key `(event_id, participant_id)` enforces one first arrival independently of application locks. Timestamps are UTC epoch seconds; Sheets exports use timezone-aware UTC ISO strings; display uses configured `Asia/Manila` initially.

## Registration import and attendance export

Synthetic initial import and subsequent import validation:

```sh
.venv-web/bin/python -m event_checkin.web.cli import --dry-run
.venv-web/bin/python -m event_checkin.web.cli import
```

Dry-run may initialize/migrate the configured database, but does not apply registration or attendance changes or export anything. It prints counts, not participant data. Initial existing Sheet attendance is imported with its original timezone-aware timestamp/nickname, method `legacy`, and no new export. Blank/older Sheet attendance never replaces database attendance. Conflicting nonblank attendance requires review; a new legacy arrival after admissions open fails validation. Timestamp strings without an explicit timezone fail closed.

A background thread requests registration refresh every 45 seconds by default. Sync now queues a pass; status and errors are visible in Overview. The dashboard polls every 15 seconds. Registration changes take effect only after a successful validated sync. A snapshot older than 120 seconds is visibly marked stale; the final production freshness policy is undecided. Admission requires an active backend/database connection; there is no browser-cache admission. A successful check-in does not wait for Sheets.

A database lease coordinates sync passes across backend processes without holding an admission transaction during Google I/O. Run one backend process initially; multiple workers share the same database and lease. Each pass imports a complete snapshot, then exports at most 100 pending attendance records in batches per source worksheet. Exports re-resolve event/source/record/participant IDs and frozen issued identity immediately before writing only `checked_in_at`, `checked_in_by`, and `checkin_request_id` as RAW values. They do not add live headers or write nickname session IDs/methods into unsupported Sheet columns. They preserve form, approval, identity, tier, source, and email metadata. Sheets does not support conditional stable-ID updates: **freeze row sorting/deletion during operation**; append-only form responses are supported.

Outbox records survive restarts. Failed/uncertain writes retry the same immutable attendance values with exponential backoff (up to 15 minutes), on a subsequent sync pass. A partially successful multi-source batch can be replayed safely. Import/export failures never erase a committed arrival. Overview distinguishes registration-sync errors, pending exports, and export errors.

To exercise an outage locally, stop any in-progress fake sync, edit the private `.web-runtime/sheets.json` to set `"fail_exports": true` or `"fail_reads": true` at its top level, then request sync. Confirm a different eligible attendee while exports fail: attendance must save and pending exports must increase. Restart the backend, remove the flag, and allow backoff to elapse. Export only changes the three attendance fields on the correct stable source record. Do not edit/reorder a fake file during an export pass.

## Later live dry-run and cutover — not activated by implementation

Final hosting/domain, database provider, network fallback and freshness policy are pending decisions. Live access requires explicit later authorization and configuration; the following is an operator procedure for that later work.

1. Back up Sheets and the database privately. Stop the legacy check-in server/scanners and **all other attendance writers**. The legacy Sheets attendance writer must never run alongside the new authoritative web service after cutover. Normal email tooling retains its separate approvals; this app grants no email or issuance authorization.
2. Provision a shared PostgreSQL database and trusted HTTPS origin. Generate a new private staff password hash. Use the original permanent event ID; never invent replacement ticket IDs/tokens. Set `WEB_SYNTHETIC=false`, `WEB_LIVE_EXPORT=false`, and configure `WEB_LIVE_CONFIG` as the repository/configuration root containing a reviewed `.env.production` with email disabled and approved source manifest/approval files. This is an explicit read boundary; the default application never opens it. Keep web database/configuration distinct from email runtime journals.
3. Run migrations, then `import --dry-run`. The live adapter uses the existing readonly `ProductionSheets` read path, exact reviewed column mappings, optional Special source and recipient exceptions, and shared-email approvals. It creates no headers and invokes no email/issuance operation. Resolve incomplete identities, missing schemas, duplicate tokens, controlled tests, and source failures before proceeding. Confirm the expected eligible counts and source/tier categories; don't treat every response as eligible.
4. Run `import` with admissions still closed. Review **all existing attendance** against the Sheet snapshot in a private database client: `SELECT participant_id,checked_in_at,checked_in_by,method FROM attendance WHERE event_id = '<configured event ID>' ORDER BY checked_in_at;`. Compare totals and timestamps, including cancelled records with historical attendance. Resolve conflicts explicitly; the app offers no undo/reset. Do not erase existing attendance to make import succeed.
5. Once reconciliation is complete and the legacy writers are stopped, run `open-admissions --reconciled`. This explicit CLI acknowledgement opens the event. The synthetic `seed` command performs this only for its isolated demo.
6. Only with later explicit live-write authorization, set `WEB_LIVE_EXPORT=true` and start the web service. This enables a separate attendance-only export boundary; it does not call or weaken the existing production/email `require_operational()` gates. Observe the first export, its destination source and preserved unrelated fields. Verify DB backups, cookie/TLS settings, session expiration and three independent devices before opening the desk.
7. Monitor sync age/errors and export backlog. Stop admission operation and reconcile deliberately if a conflicting legacy writer appears. A network/hosting offline fallback requires a separate approved design; staff must not admit using stale browser state.

## Verification commands and limits

```sh
# Includes existing issuance, email, identity, source, tier and rendering regressions.
.venv-web/bin/python -m unittest discover -v
# Focused web API, import/export, transaction rollback and multiprocess race suite.
.venv-web/bin/python -m unittest tests.test_web_app -v
npm --prefix frontend run check
npm --prefix frontend test
npm --prefix frontend run build
node --test tests/apps-script.test.js
```

For PostgreSQL integration, use a **dedicated disposable test database**, never a live one. The tests create synthetic events and modify session/throttle tables:

```sh
WEB_TEST_DATABASE_URL='postgresql+psycopg://freshcoat:REPLACE_WITH_TEST_DB_PASSWORD@localhost:54329/freshcoat_test' .venv-web/bin/python -m unittest tests.test_web_app -v
```

The browser runner starts its own synthetic server, temporary SQLite database, random password and QR file. It never uses the real dataset. It verifies image QR review/cancel/confirmation, lost-response recovery after refresh, logout, simulated canvas-camera capture/release, keyboard focus trapping, and overflow/action placement at 320/390/768/1440 pixels, saving screenshots under ignored `frontend/test-results/`. Install optional test tooling separately:

```sh
npm install --prefix .web-runtime/browser --no-save playwright@1.63.0
.web-runtime/browser/node_modules/.bin/playwright install chromium
FRESHCOAT_PLAYWRIGHT_MODULE="$(pwd)/.web-runtime/browser/node_modules/playwright/index.mjs" npm --prefix frontend run test:browser
```

Alternatively set `FRESHCOAT_CHROME` to an installed Chrome executable. `FRESHCOAT_WEB_PYTHON` selects another isolated Python executable. Review the generated screenshots, including long email wrapping, mobile bottom navigation, dialog action reachability and visible focus. A simulated stream does not verify a physical camera or Safari.

Implementation verification: the full original-plus-web Python run passed 452 tests; the expanded 21-case web suite also passed after adding background-worker, HTTPS-cookie and uncertain-Sheets-response checks. Seven frontend unit tests passed, including the actual vendored jsQR decoder and camera lifecycle simulations. Frontend syntax checks/build passed. See `docs/WEB_VERIFICATION.md` for the final recorded checks.

Environment limits: Docker's daemon was unavailable, so PostgreSQL execution was not verified here. Local TCP listening is denied by the sandbox; installed headless Chrome also failed to launch, and computer-use browser access was unavailable. Consequently a listening-server/browser run and visual screenshot inspection could not be completed in this environment. API end-to-end tests ran through FastAPI's in-process HTTP transport with real persistence and independent database processes. Do not interpret that as phone/desktop browser visual sign-off.

Before event use, run the browser suite and PostgreSQL suite, visually inspect phone/tablet/desktop screenshots, then verify actual iPhone Safari/Android Chrome and laptop cameras, rear camera selection, denied/in-use/no-device cases, HTTPS, keyboard/soft-keyboard safe areas, lighting/focus, and scanning printed tickets. Final production database provider/hosting/domain, connectivity fallback, sync/freshness settings, editing registrations, walk-ins, cancellations from the app, attendance undo/reset, and kit/merchandise claims remain undecided or outside v1. Attendance is not an item claim.
