# Preview verification — 23 November 2026

The supplied patch was reviewed and applied to the actual `event-checkin` repository at base commit `09ed4a80f926a18e120cf87e06a539bbbc7ed071`. The working tree was clean before application. No applicable AGENTS.md was found. `git apply --check` succeeded; the patch was applied once. The existing demo/email configurations were preserved.

Two small compatibility adjustments accompany the patch: source status can use the existing demo backend's `synthetic` flag before that process is restarted, and preview tests skip cleanly in the separate email environment when optional web test dependencies are absent (consistent with the existing web tests).

## Automated fixture checks

- 76 Python tests across `tests.test_web_preview`, `tests.test_web_app`, `tests.test_profiles`, `tests.test_adapters` and `tests.test_google_reads` passed.
- 10 frontend tests passed, including the three preview review tests.
- Frontend JavaScript syntax checks, production build and Git whitespace checks passed.

These tests use synthetic participants and real-format tokens. They do not establish successful reads of actual registrations. They cover authenticated QR/image lookup and search without admission, historical attendance display, API/service/CLI admission rejection, exports blocked at both synchronizer and adapter, database-mode separation, readonly adapter construction, reviewed mappings and unchanged demo/live admission requirements.

## Actual local setup and source attempt

`preview-setup` created owner-only `.env.web.preview` in the actual repository. Private comparisons verified that its event ID/timezone match the existing production configuration, its password hash matches the existing demo, its database differs from the demo database, `WEB_READ_ONLY_PREVIEW=true`, `WEB_SYNTHETIC=false`, and `WEB_LIVE_EXPORT=false`. No exported `WEB_*` overrides were present. The production source manifest and configured credential file exist. Neither credentials nor password hashes were printed.

The actual command `WEB_ENV_FILE=.env.web.preview .venv-web/bin/python -m event_checkin.web.cli import --dry-run` **failed before reading registrations**. Its exception chain ended in DNS name resolution failure. Independent resolver checks also failed for Google authentication, the Sheets API and a general comparison host (resolver errno 8). This is an environment/network blocker, not evidence of bad service-account permissions, worksheet mappings or attendee identities. Those later checks were not reached; source validation was not weakened and no source was skipped.

The actual import was not run after the failed dry-run. The isolated preview database contains **0 participants, 0 attendance records, 0 request results and 0 outbox records**. It is bound to preview mode with admissions closed. These are empty local database counts, not counts of the actual registration sources. Successful source import counts remain unknown.

Local issued-ticket images are available, but no real issued QR could be matched against real imported registrations because the import was blocked. No attendee names, emails, ticket tokens or ticket filenames are included here. The preview configuration and database are ignored by Git and remain private local files.

## Preview process and browser status

The preview was **not started** after the failed dry-run. A port capability probe independently showed that this sandbox denies binding `127.0.0.1:8081` (`operation not permitted`). Browser inventory reported that the browser access host was unavailable. The existing demo on port 8080 was not stopped or reconfigured. There is no preview PID to stop.

The configured preview URL is **http://localhost:8081**, but it is not currently served by this task. Retry the setup guide's dry-run from a terminal with Google network access and local listening permission. Only after it succeeds, run the actual import and start command. When run in a terminal, stop the preview with Ctrl-C; restart with the same preview Uvicorn command. Do not replace `.env.web`, change issued tokens or open admissions.

## Still required

- Successful readonly access and validation of every configured real source, followed by actual import and aggregate counts.
- A real issued QR lookup and matching manual search, with correct name/email/tier/status and unchanged attendance/outbox counts.
- Browser confirmation of the exact preview label and disabled check-in button; QR upload and physical camera/device verification.
- PostgreSQL admission testing against a separate fresh operational database, multi-device HTTPS tests and later production cutover approval. Preview remains localhost-only and its database must not become an admission database.

No emails were sent, tickets generated/reissued, Google Sheets updated, production attendance recorded, exports enabled or deployment settings changed.
