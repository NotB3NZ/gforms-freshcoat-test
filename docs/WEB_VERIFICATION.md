# Web application verification — 2026-11-23

Only synthetic fixtures and temporary databases were used. No Google clients were opened by tests, no emails were sent, no real tickets were generated, and no production attendance or hosting settings were changed. Existing issuance/email/source/identity modules were left unchanged.

| Check | Observed result |
| --- | --- |
| `.venv-web/bin/python -m unittest discover -v` | **452 tests passed**, including 18 web tests and all 434 existing regression tests; actual synthetic FreshCoat rendering regressions passed |
| Expanded `.venv-web/bin/python -m unittest tests.test_web_app -v` | **21 tests passed**, including three later cases for the background worker, Secure cookies on HTTPS and a Sheets write whose response is lost |
| Independent spawned database processes | Three processes, separate connections: **one accepted**, two duplicate results, identical original timestamp, one export outbox record |
| HTTP API through FastAPI TestClient | Password/nickname gate, unauthorized endpoints, expiry/logout, CSRF/Origin, throttling, attribution, QR/manual lookup and admission, request misuse, retry/restart reconciliation passed |
| Transaction failure | Forced outbox failure rolled attendance and request journal back together |
| Import and sync | Existing attendance, cancelled/invalid/test/duplicate tickets, five tiers, aliases, shared-email approvals, source mapping, frozen identities, partial/failed snapshots, Unicode search, attendance preservation and backoff/restart passed |
| Background worker | Initial import and queued export passed; a deliberately stalled external read did not block a concurrent admission |
| Actual QR decoding | Synthetic QR decoded by vendored browser jsQR in Node and backend OpenCV; lookup did not admit |
| `npm --prefix frontend test` | **7 tests passed**: request retention/storage failures, camera lifecycle/races/error recovery and actual jsQR decoding |
| Frontend syntax checks / build | Passed; dependency-free copy build, vendored jsQR license included |
| `node --test tests/apps-script.test.js` | Passed |
| Documented CLI migration / seed / dry-run | Passed against a disposable private database: 12 registrations, 7 eligible, 1 prior arrival, zero omitted rows |
| Browser test runner | Implemented; execution stopped because local TCP listening is denied (`operation not permitted`) |
| Installed headless Chrome launch | Attempted, failed before browser startup in sandbox |
| Computer-use browser QA | Browser host unavailable; native app access timed out |
| Phone/desktop visual inspection | **Not completed**; no screenshots represented as verified |
| PostgreSQL integration | **Not run**: Docker daemon unavailable and registry downloads blocked; PostgreSQL configuration/migrations and repeatable test command supplied |
| Physical Safari/Chrome cameras | **Not tested**; frontend lifecycle tests simulate devices, and the browser runner supplies a simulated canvas stream when run on a capable host |

Use [WEB_APP.md](WEB_APP.md) for exact setup/test commands, the browser runner, PostgreSQL option, private data boundaries and live cutover procedure. Before event use, run PostgreSQL and browser checks on the selected host, inspect 320/390/768/1440px screenshots, and verify actual phones/cameras and trusted HTTPS. Hosting/domain, production database provider, connectivity fallback, final sync/freshness settings, registration editing, walk-ins and item claims remain open decisions.
