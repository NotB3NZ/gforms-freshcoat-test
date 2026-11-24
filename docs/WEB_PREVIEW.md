# Real-registration preview

This mode lets staff scan an already-issued QR or search actual registrations before live admission is enabled. It reads the existing Google Sheets using the current production source mappings, reviewed exceptions, event ID and QR tokens. It never changes the source Sheets, sends email, issues tickets, opens admissions or records a new check-in.

Imported historical attendance is copied into the private local preview database for display only. Authentication sessions and synchronization metadata also persist locally. "Read-only" refers to registration source data and admission operations, not to a filesystem that receives no writes.

## Setup after applying these source changes

From the existing event-checkin repository:

```sh
.venv-web/bin/python -m event_checkin.web.cli preview-setup
WEB_ENV_FILE=.env.web.preview .venv-web/bin/python -m event_checkin.web.cli import --dry-run
WEB_ENV_FILE=.env.web.preview .venv-web/bin/python -m event_checkin.web.cli import
npm --prefix frontend run build
WEB_ENV_FILE=.env.web.preview .venv-web/bin/python -m uvicorn event_checkin.web.app:app --host 127.0.0.1 --port 8081 --no-proxy-headers
```

Open http://localhost:8081 and use the same staff password as the local demo, then enter a nickname. Keep the process bound to 127.0.0.1 with proxy headers disabled. Preview is for this computer; phone/LAN access and production deployment remain separate tasks.

Run one command at a time. Continue to import/server startup only after the dry-run succeeds. The dry-run opens Google with readonly scope, validates all configured sources, and prints aggregate counts. It may initialize the isolated preview database, but does not apply registrations/attendance or export anything.

If working in an isolated source checkout, use the existing repository as the source:

```sh
/path/to/existing/event-checkin/.venv-web/bin/python -m event_checkin.web.cli preview-setup --source-root /path/to/existing/event-checkin
```

Use that same Python executable for the other commands, from the isolated checkout. No source credentials are copied into the code checkout; the private preview configuration references the existing configuration root. Only the existing staff password hash is reused.

The setup command:
- Reads the production event ID and timezone rather than inventing a replacement.
- Reads existing .env.web to reuse the configured staff password hash.
- Creates .env.web.preview with owner-only permissions and refuses to overwrite it.
- Sets WEB_SYNTHETIC=false, WEB_READ_ONLY_PREVIEW=true, WEB_LIVE_EXPORT=false.
- Uses a distinct local database at .web-runtime/preview/registrations.sqlite3.
- Leaves .env.web, .env.production, Google credentials, issued tokens and email runtime files unchanged.

The preview requests registration-read-only settings from the production loader. Email is disabled and SMTP credentials are cleared in memory before profile validation, so an email-enabled production profile can also supply registration reads. All event/storage isolation checks and normal production sending locks remain active. You do not need to turn off the working email workflow or edit its configuration to run this preview. Existing reviewed shared-email/Special mappings are retained.

## What to verify

The header must say "Real registrations · Read-only preview", not "Synthetic demo". A persistent notice explains that attendance changes are disabled.

After a successful registration import:
1. Scan/upload a real issued ticket and verify its name, email and tier.
2. Verify the same registration through participant search.
3. Confirm that the admission button is disabled and labelled "Read-only preview".
4. Check existing attendance displays its recorded time/name without creating a new arrival.
5. Inspect Overview for import success/time and aggregate counts. Sync now requests a read-only refresh.

A backend connection alone does not prove that registrations were imported. The source mode and registration-sync result are displayed separately. Unknown tickets can still mean a failed/incomplete import, wrong event, unissued token or ticket outside the configured sources; do not regenerate tickets merely to resolve a lookup mismatch.

Preview uses the existing source validation rules and rejects inconsistent snapshots. A successful import is not automatic sign-off that every record is eligible. Review any ineligible reasons privately.

## Enforcement

- Password/nickname authentication, same-origin/CSRF checks and session expiry remain active.
- Preview sessions use a distinct cookie name so running demo and preview on different localhost ports does not replace one another's login cookies.
- The backend rejects all check-in confirmations and open-admissions operations, independently of UI state and even if an event flag is manually set open.
- Synchronization cannot export attendance; the adapter also rejects export calls independently.
- Preview rejects WEB_LIVE_EXPORT=true, synthetic mode, missing production configuration, the default synthetic event ID and non-localhost origins.
- Live admission still requires PostgreSQL and HTTPS; preview is not a bypass for operating a live desk on HTTP/SQLite.
- A database is bound to synthetic, preview or live mode. A preview must start with a fresh database, and a preview database cannot later be used for admission.
- Development ticket generation is unavailable in preview, and development/controlled-test tokens remain invalid for real-data lookup.
- Real registration data stays in ignored, private local files. Do not commit or attach the preview database/configuration or real tickets.

Use a fresh database for live cutover, import/reconcile existing attendance and follow docs/WEB_APP.md. Do not turn off the preview flag on its database or open admissions during this verification step.

## Checks

```sh
.venv-web/bin/python -m unittest tests.test_web_preview_startup tests.test_web_preview tests.test_web_app -v
npm --prefix frontend run check
npm --prefix frontend test
npm --prefix frontend run build
```

The preview tests use synthetic identities with real-format tokens; no Google or SMTP access occurs during tests. They cover lookup and QR decoding, authentication, API/service/CLI write rejection, database separation, readonly adapter construction, export suppression, configuration creation and the preservation of demo/live behavior.

Actual real-source access, browser visuals, physical camera behavior and PostgreSQL admission tests must be reported separately from these automated checks.

## Stop and restart

This session's actual dry-run and process status are recorded in [WEB_PREVIEW_VERIFICATION.md](WEB_PREVIEW_VERIFICATION.md). When the preview is running in a terminal, stop it with **Ctrl-C**. Restart from the repository root with:

```sh
WEB_ENV_FILE=.env.web.preview .venv-web/bin/python -m uvicorn event_checkin.web.app:app --host 127.0.0.1 --port 8081 --no-proxy-headers
```

If the dry-run failed, resolve that failure and complete the import before starting. Port 8080 and its `.env.web` remain the separate demo. A permission error binding port 8081 must be resolved in the local execution environment; do not expose preview on a LAN address to work around it.
