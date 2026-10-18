# Event registration and QR check-in

This project connects your Google Form response Sheet to a Python registration worker, your existing FreshCoat card, email delivery, and a staff-only webcam check-in server.

The code is implemented and locally tested. **Stop at checkpoint 1 below for now.** Google account access, the installed Apps Script trigger, real email delivery, and webcam access still need your participation. No real email has been sent and no server has been deployed or opened publicly.

## What was preserved and verified

- `templates/card.coat`, `freshcoat/`, the original `.venv/`, existing generated files, and service-account file are preserved.
- The actual card is **1200 × 900**. Its text layer uses `{{displayName}}`, and `displayName` is declared in the field schema. Renaming a layer alone would not bind a field.
- The default QR rectangle is **x=400, y=450, size=400**, matching the reserved black square below the name. The QR includes a white quiet zone of at least four modules. Rendering refuses coordinates that intersect a text layer or fall outside the card.
- FreshCoat renders a temporary copy with the name text set to shrink within its existing box. Layer positions, colors, font choice, and the source archive are preserved. Embedded fonts are reused. If you later introduce external relative assets, bundle them into the `.coat` archive before rendering.
- Local tests render the real template with different names, confirm name pixels change, preserve the event band, test a long name, and decode the generated QR with OpenCV.
- `.gitignore` covers credentials, `.env` variants, environments, cards, scanner pending requests, and the server journal. Examples remain trackable. Coverage is tested in a temporary Git repository independently of local Git state. `.env.example` contains blank Sheet settings; enter live settings only in your ignored `.env`.

## Clone this project

FreshCoat is a Git submodule pinned to the version used by this implementation. Clone both repositories with:

```sh
git clone --recurse-submodules https://github.com/NotB3NZ/gforms-freshcoat-test.git
cd gforms-freshcoat-test
```

If you already cloned without `--recurse-submodules`, run `git submodule update --init --recursive` from the project root. Install FreshCoat and Python dependencies using the commands below, adjusting the project path to your clone. Credentials, `.env`, generated cards, runtime journals, and Python environments are intentionally excluded from Git; configure them locally.

## Working directory and Python

Run every Python command below from:

```sh
cd /Users/benzdecastro/Documents/Projects/test_gforms/event-checkin
```

Your existing `.venv/bin/python` has the dependencies and runs the local tests. It is Python 3.9 with LibreSSL and emits compatibility warnings from Google authentication and urllib3. For account and event use, prefer the installed Python 3.12 with OpenSSL. A separate `.venv312` was created, but dependency installation could not finish. On resuming, pip again reported no available distribution for the pinned FastAPI version in this session; installation in Python 3.12 remains unverified. The original environment was not modified.

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
   test -f .env || cp .env.example .env
   chmod 600 .env
   open -e .env
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

1. In the **response spreadsheet**, open **Extensions → Apps Script**. Copy `apps-script/form-submit.gs` into the project.
2. Replace `CONFIG.eventId` and `CONFIG.sheetTab` with the same values as `.env`. Apply metadata header overrides only if you configured them in Python.
3. Save. Select `setupTrigger` in the function dropdown, click **Run**, and complete Google's sign-in/authorization yourself. This creates an installable **From spreadsheet → On form submit** trigger. Only one account should install it; re-running setup removes that installer's duplicate triggers.
4. Submit a new test registration using the actual Google Form. Its new response row should show your `event_id`, `state=PENDING`, and `updated_at`. Apps Script **Executions** should show a successful run. Do not manually run `onRegistrationSubmit`; it requires a real submission event.

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

## Later checkpoint — authorize one real email test

Do not run a real send until you identify the test recipient and explicitly authorize the test with your assistant. Stop the polling worker first. Enter SMTP settings privately in `.env`: host, port, username, password or provider app password, sender, and `SMTP_SECURITY=starttls` (usually port 587) or `ssl` (usually 465). Plain SMTP is not supported. The service account is not an SMTP password and does not automatically authorize email.

After authorization, set `EMAIL_ENABLED=true` temporarily and send the **fake preview only** to that one approved recipient:

```sh
cd /Users/benzdecastro/Documents/Projects/test_gforms/event-checkin
.venv/bin/python -m event_checkin send-test --to approved-recipient@example.com --authorize-send
```

Replace the example address with the approved recipient. This command never processes pending registrations. Success first means SMTP acceptance; verify the inbox/spam folder and open the image attachment. If the command fails after submission, inspect provider logs before repeating it. Set `EMAIL_ENABLED=false` again while reviewing setup. Enable ongoing registration emails only when you are ready to authorize the worker to deliver to all queued recipients.

## Later checkpoint — staff server and webcam

Create a separate staff credential locally, after `.env` exists. This writes the key directly into the ignored file without printing it:

```sh
cd /Users/benzdecastro/Documents/Projects/test_gforms/event-checkin
.venv/bin/python - <<'PY'
import os, secrets
from dotenv import set_key
set_key('.env', 'STAFF_API_KEY', secrets.token_urlsafe(32))
os.chmod('.env', 0o600)
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

Set `STAFF_STATION_NAME` in `.env` to a recognizable label, such as `Entrance A`. On first admission, `checked_in_by` records this label with the timestamp and request ID in the same Sheet update. Lost-response retries retain the original station. This is a station label supplied by an authenticated scanner, not a verified individual staff identity; scanners currently share one staff credential.

Grant camera permission yourself if macOS requests it. Use `CAMERA_INDEX` for a different camera. Show one real issued QR at a time. Only the **ACCEPTED** result confirms admission. Another request for the same ticket shows **Already checked in — do not admit**; an unknown/cancelled ticket shows **Invalid or cancelled ticket — do not admit**. A network/Google/auth failure shows **Unable to confirm — do not admit; retrying**. `Q` closes the scanner; `R` retries a pending scan.

The scanner suppresses repeated submissions while a QR remains visible; it can submit again after the code is absent for two seconds. A timed-out request is saved in `runtime/scanner-pending.json` and retried with the **same request ID**, even after restarting the scanner. It will not replace an unresolved scan with another person's ticket. Keep the pending file until the outcome is confirmed.

The server requires `X-Staff-Key` on every `/check-in` request; participant tokens are separate secrets and cannot authorize staff requests. A valid rendered ticket becomes usable even when email is disabled, allowing private card testing. Attendance is recorded with the event timezone. Retries of a completed request return its original result; reusing a request ID for a different token is rejected.

## Coordination and operational limits

Use **one central server process and one worker process on the same computer**, sharing the same `.env` and `RUNTIME_DIR`. The whole check-in lookup/decision/Sheet write and durable request journal are protected by a thread lock and an OS file lock. Worker Sheet changes use the same lock and update only their own metadata cells. Server and worker singleton locks reject accidental extra local instances.

This does not coordinate independent hosts, containers with different runtime directories, or arbitrary Apps Scripts editing issued tickets. Multiple independent check-in servers/workers require a shared transactional database or distributed coordination before use. Do not use network filesystems for runtime locks/SQLite. This version is deliberately local; remote cameras need an authenticated HTTPS deployment and a separate deployment review. Non-local plain HTTP scanner URLs are refused. Nothing here publishes the system.

Pause both processes before sorting, deleting, or manually changing existing response rows/metadata. Appending normal Form submissions is supported. IDs are looked up afresh between jobs, so sorting while stopped does not change identity; Sheets has no conditional cell-write operation to protect against a person sorting during an API write.

Back up the Sheet and private `runtime/` directory while processes are stopped. Retain both databases for the event: `checkin.sqlite3` stores request results across restarts and uses token hashes, while `issuance.sqlite3` contains reserved IDs, emails and raw ticket tokens needed for recovery. QR-bearing cards, both journals, the Sheet token column, `.env`, service credentials, and scanner pending files remain private. Do not clear check-in timestamps or request IDs to allow repeat admission.

## Code map and verification limits

| File | Purpose |
| --- | --- |
| `event_checkin/config.py`, `sheets.py`, `locking.py` | Shared settings, header mapping, Google access, local coordination. |
| `issuance.py`, `cards.py`, `mailer.py`, `worker.py` | Durable ticket reservations, actual FreshCoat rendering, QR composition, SMTP and recovery. |
| `checkin.py`, `server.py` | Durable request journal and staff-protected FastAPI endpoint. |
| `scanner.py` | OpenCV camera UI, visibility suppression and persistent request retries. |
| `apps-script/form-submit.gs` | Installable spreadsheet form-submit trigger source. |
| `tests/` | Fake Sheet/SMTP tests, HTTP endpoint tests and actual card/QR tests. |

All **46 tests passed** when this project was resumed on 18 October 2026, including the original 39 tests. Additional regression tests cover cancelled/unknown statuses, cancellation without identity or attendance loss, duplicate registration after cancellation, station recording across failed writes/restarts, and API station validation. The suite also covers first admission, duplicate/invalid tickets, simultaneous scans/retries, recovery, identity reservations, rendering/email failures, ambiguous sends, header mappings, metadata-only RAW writes, staff authentication, unavailable attendance, malformed server responses, QR suppression, pending scan recovery, and ignore coverage. It uses fake Google and email services; no accounts or hardware are required. Offline tests execute the actual Apps Script source against fake Google objects to check queuing order, identity protection, event mismatch handling, lock release, repeat trigger installation, and agreement with Python's metadata columns; Google authorization and installed trigger execution remain unverified.

The resumed local preview is at `generated/RESUMED-LOCAL-PREVIEW/card.png`. It uses a fake QR and preserves the earlier previews. The original `.coat` archive's SHA-256 remained `c9e727bb7280f9effa5712ebe7d6e2e95e5741e74b1f6b71778edfb24fec8bc2` before and after rendering. FreshCoat's checkout has no local Git changes. The project's existing Python environment also reports `No broken requirements found`.

Still unverified: live Sheet sharing/read/write permissions, exact response headers until configured, real installed trigger authorization/execution, SMTP account acceptance and inbox delivery, webcam permission/device behavior, and live Google quota/latency under event load. The fresh Python 3.12 dependency installation also awaits network access in your terminal. Follow the checkpoints in order.

Implementation references: [FreshCoat CLI source](freshcoat/packages/cli/README.md), [gspread worksheet API](https://docs.gspread.org/en/latest/api/models/worksheet.html), [Python SMTP behavior](https://docs.python.org/3/library/smtplib.html), and [FastAPI lifespan](https://fastapi.tiangolo.com/advanced/events/).
