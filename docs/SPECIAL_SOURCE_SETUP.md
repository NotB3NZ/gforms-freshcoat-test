# Optional Special source: review and setup

The active three-source manifest and header plan stay unchanged. `special` is
optional and joins `--all`, source ordering and duplicate checks only in a
configured four-source dataset. Existing authorizations remain bound to their
original bytes, source dataset and header schema; this change does not expand or
rewrite their scope. Keep email and every sending authorization disabled.

The candidate uses the existing spreadsheet, `SPECIAL | REGIS` / `2055698387`,
name D, delivery email C, choice M (`Please confirm`), approvals A/S/T/U and price
category `special`. The sole allowed duplicate is `Email Address` at C/F. F is
never read as participant delivery data and is never a fallback for blank or
invalid C. Exact choices are `Special Ticket (₱180)` → `Special Ticket` and
`Pass` → `Pass`. Pass is complimentary; both choices still require all four
staff approvals. No checkbox is inferred or written.

## Current candidate status

Private files are under `runtime-production/source-candidates/special/`:

- `sources.production.json`: four-source candidate; the original three entries
  are copied unchanged. D's header text is provisional until audited.
- `production-header-init.candidate.json`: candidate snapshot with 21 original
  Special headers and tracking range V1:AS1. Unobserved headers are null and
  `requires_live_verification` is true until a successful header-only audit.
- `header-audit.json`: reports whether the destination was actually verified.

A draft cannot pass header-plan validation or initialize anything. The supplied
audit reported 21 populated headers through U; that does not establish that
V1:AS1 is blank. Google OAuth DNS was unavailable during implementation, so the
live blank-range verification remains pending. Do not promote the draft.

## Commands safe to run before activation

Run from the repository:

```sh
cd /Users/benzdecastro/Documents/Projects/test_gforms/event-checkin
.venv/bin/python -m unittest tests.test_special_source -v
.venv/bin/python -m unittest discover -q
```

Recreate an unverified local draft without account access:

```sh
.venv/bin/python -m event_checkin --profile production production-prepare-special --draft
```

When Google access is available, complete the candidate with a read-only audit:

```sh
.venv/bin/python -m event_checkin --profile production production-prepare-special --audit
```

The audit uses a read-only Google scope, reads metadata and row 1 only, validates
the existing three initialized tabs, captures all 21 Special headers and verifies
the exact C/F duplicate. It reads row 1 using FORMULA so a formula displaying an
empty result is occupied. Extra headers, partial tracking, or any populated
destination stop preparation. A successful audit saves a verified candidate
locally; it neither switches configuration nor initializes headers. Re-run the
audit immediately before any later initialization.

## Later setup commands, for separate review only

These commands were **not executed**. They write Special tracking headers and
switch local configuration, so they belong to the later approved setup step.
They never enable SMTP or activate a sending policy. The initializer rechecks
all four tabs before writing, leaves already initialized tabs untouched, writes
only 24 headers in V1:AS1 (and necessary grid expansion), then verifies readback.

```sh
.venv/bin/python -m event_checkin --profile production production-prepare-special --audit

.venv/bin/python - <<'PY'
from dataclasses import replace
from pathlib import Path
from event_checkin.config import load_settings
from event_checkin.header_init import HeaderInitializer, HeaderTransport, load_plan
from event_checkin.special_source import require_disabled_authorizations

base = load_settings('production')
require_disabled_authorizations(base)
folder = base.runtime / 'source-candidates/special'
candidate = replace(base, sources_file=folder / 'sources.production.json')
plan = load_plan(candidate, folder / 'production-header-init.candidate.json')
transport = HeaderTransport(candidate, plan, apply=True)
print(HeaderInitializer(candidate, plan, transport).run(apply=True))
PY
```

Only after successful initialization and readback, retain the original three-source
configuration and install the reviewed candidate:

```sh
CANDIDATE=runtime-production/source-candidates/special
mkdir -m 700 "$CANDIDATE/three-source-before-switch"
install -m 600 config/sources.production.json "$CANDIDATE/three-source-before-switch/sources.production.json"
install -m 600 config/production-header-init.approved.json "$CANDIDATE/three-source-before-switch/production-header-init.approved.json"
install -m 600 "$CANDIDATE/sources.production.json" config/sources.production.json
install -m 600 "$CANDIDATE/production-header-init.candidate.json" config/production-header-init.approved.json
.venv/bin/python -m event_checkin --profile production production-init-headers
```

Create a fresh selected-entry plan using the existing preview/authorization flow.
Use new filenames; do not replace a historical plan or authorization:

```sh
.venv/bin/python -m event_checkin --profile production batch-preview --live \
  --source special --limit 25 \
  --plan-out runtime-production/batch-reviews/special-reviewed.json
.venv/bin/python -m event_checkin --profile production batch-run --authorized-plan \
  --plan runtime-production/batch-reviews/special-reviewed.json --prepare-authorization
.venv/bin/python -m event_checkin --profile production batch-run --authorized-plan \
  --plan runtime-production/batch-reviews/special-reviewed.json --preflight
```

Once configured, `batch-preview --live --all --limit 25` includes Special in the
same configured source order and cross-source duplicate checks. No command above
activates a sending authorization, issues a participant ticket, or sends email;
the only live write in this later procedure is explicit row-one initialization.
