# Production batch commands — phase 1, offline only

Live batch execution is **LOCKED**. These examples use a separate fictional fixture,
local journals and local cards. They do not access Google, authenticate with SMTP,
send email, enable flags or touch real registration history. Keep
`EMAIL_ENABLED=false`. Run from the project directory with Bun/FreshCoat installed.

## Choose a scope and eligible-attempt limit

| What to select | Flags |
| --- | --- |
| One permanent registration ID | `--id regular:R-SYN-REG-002` |
| Several permanent IDs | Repeat `--id SOURCE:R-ID` |
| One source | `--source regular`, `discount_15`, or `discount_10` |
| All three sources | `--all` |
| An inclusive row range in one source | `--source discount_15 --start-row 10 --end-row 13` |

Pair the scope with `--limit N` for a positive integer, or `--limit all` for every
eligible registration in this finite preview. The default is **25 eligible
attempts**. `--all` still means all **sources**; it does not remove the limit.
For every eligible registration across all sources, use `--all --limit all`.

Both row bounds are required, must be at least 2, and include their endpoints.
Row 1 contains headers. Row bounds require `--source` and cannot accompany IDs or
`--all`. A range outside the current data produces an empty selection.

Limits count eligible attempts, not scanned rows or blocked registrations. If fewer
than N qualify, only those available are selected. Order is the configured source
order followed by ascending physical row number, including for explicit ID lists.
This fixture's order is regular, discount_15, discount_10. There is no round-robin,
random ordering or oldest-submission ordering in phase 1.

## Make previews and save exact plans

Use a new review folder and new plan filenames. Existing plans are never replaced.

```sh
FIXTURE='examples/batch-selection-phase1.json'
REVIEW='runtime-production/offline/selection-phase1-demo'
mkdir -m 700 -p "$REVIEW"
```

One registration, then a separate preview for several registrations:

```sh
.venv/bin/python -m event_checkin --profile production batch-preview \
  --fixture "$FIXTURE" --id regular:R-SYN-REG-002 --limit 1 \
  --plan-out "$REVIEW/one.json"

.venv/bin/python -m event_checkin --profile production batch-preview \
  --fixture "$FIXTURE" --id regular:R-SYN-REG-004 \
  --id discount_15:R-SYN-D15-012 --limit all \
  --plan-out "$REVIEW/ids.json"
```

Up to two eligible registrations from one source, then up to three total:

```sh
.venv/bin/python -m event_checkin --profile production batch-preview \
  --fixture "$FIXTURE" --source regular --limit 2 \
  --plan-out "$REVIEW/source.json"

.venv/bin/python -m event_checkin --profile production batch-preview \
  --fixture "$FIXTURE" --all --limit 3 \
  --plan-out "$REVIEW/global.json"
```

Every eligible registration in the finite snapshot, then the inclusive range:

```sh
.venv/bin/python -m event_checkin --profile production batch-preview \
  --fixture "$FIXTURE" --all --limit all \
  --plan-out "$REVIEW/all-eligible.json"

.venv/bin/python -m event_checkin --profile production batch-preview \
  --fixture "$FIXTURE" --source discount_15 --start-row 10 --end-row 13 --limit all \
  --plan-out "$REVIEW/range.json"
```

In this fixture, row 11 is already sent. The range preview selects exactly rows
**10, 12 and 13**. A numeric limit of 2 would select 10 and 12, showing 13 separately
as eligible beyond the limit. `--source regular --limit 100` selects only its two
available eligible entries. `--all --limit all` selects six entries.

Existing explicit row locators remain available, including when a row already
has a record ID:

```sh
.venv/bin/python -m event_checkin --profile production batch-preview \
  --fixture "$FIXTURE" --id regular:row:4 --limit 1
```

Prefer source-qualified permanent IDs. `SOURCE:R-ID` binds a registration;
`SOURCE:P-ID` can identify an issued ticket but does not authorize another delivery.
Unique unqualified IDs remain accepted for compatibility. Missing, ambiguous or
repeated IDs fail. Row locators bind the registration currently found there; a
replacement registration cannot silently inherit the selection. Permanent IDs
allow the original registration to be found after sorting; do not sort while a
processor is running.

## Read the preview

The preview defaults to source order, scope, requested limit and a `summary` for
each source in the requested scope:

- `actual_registrations`: rows with any name, email, tier or stored app metadata.
- `selected_eligible_registrations`: eligible attempts within the global limit.
- `eligible_beyond_limit`: eligible registrations beyond that limit.
- `already_sent_registrations`: rows with sent state or email status.
- `blocked_registrations` and `blocked_by_reason`: registrations with blockers
  other than the already-sent exclusion, grouped by reason.
- `empty_checkbox_only_rows_excluded`: rows with no name, email, tier or stored
  app metadata, regardless of whether their staff checkboxes are checked.

Partially completed registrations and rows with any processing, identity or
delivery history remain registrations. The source's derived `_price_category`
does not establish a registration; stored `price_category` does. Blank gaps in
the mapped read snapshot count as excluded, but unused Sheet capacity does not.
Already-sent rows with additional problems also count as blocked. Reason counts
may overlap because a row can have several blockers. With `--action resend`,
already-sent rows can also count as selected or beyond the limit. Duplicate
reasons are grouped without the canonical target ID; eligibility is unchanged.

Add `--details` for the private participant lists and exact tracking targets:

```sh
.venv/bin/python -m event_checkin --profile production batch-preview \
  --fixture examples/batch-preview-reporting.json --all --limit 1 --details
```

The same command without `--details` shows just the summary and selection context.
The synthetic reporting fixture includes blank/checkbox-only rows, incomplete
registrations, processing history, sent status and equivalent duplicates.
The detail lists are:

- `selected_entries`: exact eligible attempts saved for execution.
- `blocked_entries`: registrations in the requested scope that failed eligibility,
  including sent rows, with every blocking reason.
- `eligible_beyond_limit`: eligible registrations excluded by the attempt limit.
- `tracking_scope.targets`: the only registrations permitted app tracking updates
  by this saved plan. Blocked and over-limit entries receive no writes.

Each entry shows physical row, target ID, mapped tier, all four approval results
and current status. Tokens are not printed. Names/email in these examples are
fictional; future participant previews would need private handling.

Saving a preview creates no registration IDs, claims, rendering, tracking updates
or SMTP connection. A plan contains exact target/fingerprint/issued-identity
bindings, not an instruction to fill a quota from future submissions. Later
approvals and new submissions require a fresh preview. Execution rechecks every
source for duplicates and all current safeguards. If a selected registration
changes or is blocked, no other participant is substituted. A selected row may
receive app metadata recording that block; Form answers, staff checkboxes and
unselected rows remain untouched.

## Execute only the saved synthetic plan

This command renders three local badges and saves offline tracking checkpoints.
With email disabled their final state is `READY` / `DISABLED`, not `SENT`.

```sh
.venv/bin/python -m event_checkin --profile production batch-run \
  --fixture "$FIXTURE" --plan "$REVIEW/range.json" \
  --state-out "$REVIEW/range-state.json"
```

The plan's `fixture_session` identifies its isolated journal under
`runtime-production/offline/SESSION/production-batches.sqlite3`. Cards go under
`generated-production/offline/SESSION/`. These are separate from real journals and
cards. The synthetic fixture contains no SMTP/Google credentials.

Resume that exact plan using the saved checkpoint:

```sh
.venv/bin/python -m event_checkin --profile production batch-run \
  --fixture "$REVIEW/range-state.json" --plan "$REVIEW/range.json" \
  --state-out "$REVIEW/range-state.json"
```

Completed actions are skipped with the same durable identities. Do not resume
from the original sample or overwrite a checkpoint. For a later preview, use the
latest saved checkpoint as `--fixture` and a new `--plan-out` filename. Separate
plans prepared from the pristine fixture are independent demonstrations; they
must use different checkpoint filenames.

Empty selections execute as a no-op: no journal, claims, tracking or rendering.
The CLI may still save the explicitly requested offline checkpoint file.

## Preserved recovery rules and phase boundary

Normal sends exclude already-sent registrations, failed jobs requiring retry,
uncertain delivery, cancellation, invalid details and approval/duplicate conflicts.
The four approvals and reviewed tier/header rules are unchanged. Failed jobs
require a separate `--action retry` preview; deliberate resends require
`--action resend`. Frozen issued details and Message-IDs remain protected.
SENDING/AMBIGUOUS delivery requires provider evidence and explicit resolution;
previewing or restarting does not authorize an SMTP retry.

New plans use format version 2 and contain only selected eligible entries. Older
version-1 plans remain supported for compatibility and may include explicitly
saved waiting-row tracking work. Their existing scope is not rewritten. The
personal-sender and controlled-test paths retain their existing plan format.
Legacy test operations and the organization inbox-test command remain separate.

**Live `batch-run` and live resolution remain locked before any adapter opens or
plan runs.** No live preflight, approval, credential setup, activation, SMTP send
or production scheduler is part of phase 1. Source-priority fairness, operational
batch sizes, audit review and future activation/recovery procedures remain later
decisions; none enables live execution here.
