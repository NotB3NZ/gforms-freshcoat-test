# Production batch commands

Production processing is **locked except for an explicitly authorized saved plan**.
Email and authorizations are currently disabled. The reusable commands below are
for a future authorized operating session. Preparation is local only; preflight
and diagnosis read Google, while execution/recovery can write the selected rows.
No command below runs in the background or substitutes another registration.

The original [three-entry pilot](OFFICIAL_PILOT.md), its history, personal-sender
commands and separately authorized [deliberate resend](DELIBERATE_PILOT_RESEND.md)
retain their legacy controls. Do not combine `--pilot` or resend controls with
`--authorized-plan`. Fixture examples later in this guide are entirely offline.

## Reusable production plan: prepare and review

Run from the project directory. The first prepared plan is the unchanged reviewed
selection **discount_15 rows 5–9**:

```sh
cd /Users/benzdecastro/Documents/Projects/test_gforms/event-checkin
PLAN='runtime-production/batch-reviews/next-five-after-resend-fix.json'
```

Its SHA-256 is
`a8137eecd44fcdf185ad672bdfc5182e8f8215f4eeece10982bacf148b580caa`.
The private authorization is
`config/batch-authorizations/0e66b621-bfa0-43e5-b134-22ba2166feb3.json`,
prepared with `enabled=false`. Existing plans and authorization bindings are never
overwritten to expand a selection. For a later reviewed plan, set `PLAN` to its
new saved filename and prepare a separate disabled policy:

```sh
.venv/bin/python -m event_checkin --profile production batch-run \
  --authorized-plan --plan "$PLAN" --prepare-authorization
```

Preparation requires `EMAIL_ENABLED=false` and performs no Google/SMTP access,
identity allocation, participant updates or card rendering. It binds the exact
plan bytes/path/ID, event, entries, tracking scope, organization sender, reviewed
source/header configuration, badge geometry/template, fonts, email HTML,
transport configuration and runtime/card locations. Credentials are neither
copied into the policy nor printed. Only the policy file is created (0600 in a
private, Git-ignored directory). Repeating preparation verifies the same disabled
policy; it does not reset an enabled or changed policy.

The next read-only preflight is:

```sh
.venv/bin/python -m event_checkin --profile production batch-run \
  --authorized-plan --plan "$PLAN" --preflight
```

Keep both email and this policy disabled. Preflight validates all three current
source schemas, the five exact saved registrations, fresh approvals, duplicates,
cancellation, frozen identity and existing acceptance history. It never creates
journals, identities, cards or Sheet updates, and never constructs SMTP. Review
every reported blocker. `ALREADY_SENT` means another verified path already has
acceptance; the stale selection cannot execute. Preserve it and create a fresh
normal preview rather than changing/resetting its rows or receipts.

The shared reader paces all Sheets GETs by credential at 1.25 seconds, including
gspread initialization requests. Only retryable reads receive bounded backoff:
at most five attempts, exponential delays plus jitter, and a 120-second per-read
waiting budget. Waiting invalidates earlier snapshots and refreshes schema,
approval and duplicate checks. Exhaustion stops the operation. Writes and SMTP
submission are never automatically retried. The detailed quota behavior in the
[pilot guide](OFFICIAL_PILOT.md#read-quota-handling) applies to reusable plans too.

## Explicit authorization and run — future operating session only

After reviewing preflight, enable only this policy, with email still disabled:

```sh
.venv/bin/python -m event_checkin --profile production batch-run \
  --authorized-plan --plan "$PLAN" --activate --authorize-plan
```

Activation makes no Google/SMTP connection and does not enable email. In a
separately authorized sending session, edit `.env.production` privately:

```sh
nano .env.production
```

Set `EMAIL_ENABLED=true` only when ready to send. Keep the existing organization
SMTP credentials private. Both configured sender fields must match
`awscc.upmindanao@gmail.com`. Execute the exact saved plan explicitly:

```sh
.venv/bin/python -m event_checkin --profile production batch-run \
  --authorized-plan --plan "$PLAN" --authorize-plan
```

All three gates are necessary: the explicit command flag, this exact enabled
policy, and enabled email. `EMAIL_ENABLED=false` blocks SMTP even if the other
gates are present. The policy is checked throughout execution and again after
authentication. Changing/revoking it or changing bound configuration/assets
stops further processing. Other plans, workers and general production commands
gain no authority from this policy or email setting.

Only the saved selected entries receive approved app metadata writes. All-source
reads are for current schema/eligibility/duplicate safeguards, not discovery of
replacement recipients. Stable record IDs, ticket reservations and Message-IDs
are retained across interruptions. Badges, decoded QR/name/tier pixels and the
frozen HTML/plain-text/inline-image/attachment MIME are verified before DATA.
Acceptance is committed durably before final tracking writes. SMTP acceptance is
not proof of inbox delivery.

## Deactivate and diagnose

When work stops, disable this plan locally, then return email to false:

```sh
.venv/bin/python -m event_checkin --profile production batch-run \
  --authorized-plan --plan "$PLAN" --deactivate
nano .env.production
```

Set `EMAIL_ENABLED=false`. Deactivation remains available after plan/config/asset
drift and changes only the policy's enabled flag. It clears no ticket, journal,
receipt, Message-ID or delivery history.

With both gates disabled, diagnose the unchanged plan read-only:

```sh
.venv/bin/python -m event_checkin --profile production batch-run \
  --authorized-plan --plan "$PLAN" --diagnose
```

Diagnosis opens the existing journal read-only and prints bounded original
targets/statuses, not names, addresses, passwords or ticket tokens. It allocates
nothing and sends nothing. Reusable scope bindings and delivery receipts share
the established production batch journal; legacy pilot/resend history is kept.

## Recover the same plan without duplicate sends

| Diagnosed outcome | Next action |
| --- | --- |
| Unstarted/reserved/verified initialization, no delivery intent | Resume the unchanged plan after review and activation. Reservations alone do not require a retry. |
| Completed or durably accepted | Completed entries are skipped. Acceptance can reconcile tracking without submitting another message. |
| Known-unsent/render failure | Activate, enable email, and explicitly retry only that original target. |
| `INTENT` / `UNCERTAIN` | Stop. Review the original Message-ID in private provider logs; never automatically retry. |
| Another path already accepted, or binding/history mismatch | Stop and review. Preserve both histories; normal plans cannot adopt/reset another operation's identity. |

For a verified failed original row 5, after activation and enabled email:

```sh
.venv/bin/python -m event_checkin --profile production batch-run \
  --authorized-plan --plan "$PLAN" --retry 'discount_15:row:5' --authorize-plan
```

Targeted retry leaves other unfinished entries untouched and reuses the same
identity and Message-ID. `--retry` cannot release an uncertain submission.

For an existing acceptance receipt, or provider-confirmed acceptance of an
uncertain original attempt, first deactivate and disable email. Record the
reviewed fact and reconcile tracking **without SMTP**:

```sh
.venv/bin/python -m event_checkin --profile production batch-run \
  --authorized-plan --plan "$PLAN" --resolve 'discount_15:row:5' \
  --outcome accepted --evidence 'REPLACE_WITH_PRIVATE_ACCEPTANCE_REFERENCE' \
  --authorize-plan
```

Existing acceptance timestamps and later cancellations are preserved. Accepted
deliveries cannot be downgraded or released for retry. Only when provider evidence
confirms the uncertain submission was **not accepted**, with both gates disabled:

```sh
.venv/bin/python -m event_checkin --profile production batch-run \
  --authorized-plan --plan "$PLAN" --resolve 'discount_15:row:5' \
  --outcome not-sent --evidence 'REPLACE_WITH_PRIVATE_NON_ACCEPTANCE_REFERENCE' \
  --authorize-plan
```

This sends nothing; subsequent sending still needs activation, enabled email and
the explicit targeted retry command. Use rows 6–9 only when that is the diagnosed
original target. Resume other eligible unfinished work with the original run
command. Deactivate and disable email again afterward.

Never delete journals, restore old rows, alter a saved plan/fingerprint, clear
delivery fields or issue replacement identities to bypass recovery. In particular,
row 2's original uncertain pilot attempt and successful linked resend remain
separate; their acceptance is an exclusion from normal sending, not authority to
rewrite the original uncertainty.

## Offline fixture examples

The remaining fixture examples use fictional data, separate local journals and
synthetic cards. They never contact Google/SMTP, enable email or change production
records. Keep `EMAIL_ENABLED=false`; install Bun/FreshCoat for fixture rendering.

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
- `already_sent_registrations`: rows with sent state/email status or verified
  durable SMTP acceptance, including separately linked deliberate resends.
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
Preview reads existing acceptance evidence without creating or changing journals.
Pilot receipts bind the archived plan/action, event, registration and frozen
ticket reservation. Deliberate resend receipts additionally verify their own
Message-ID, original unresolved receipt, immutable authorization binding and
acceptance event. Single-entry acceptance checkpoints must match their saved
selection and unambiguous action/reservation. Missing, conflicting or ambiguous
bindings block normal selection and execution for review.

A verified linked resend counts as already sent even if the original row still
shows a pre-send or uncertain status. This does not mark the original attempt
accepted, complete it, update the row or replace either receipt. Normal execution
rejects an unfinished saved selection that now has verified acceptance before
creating batch actions, and checks evidence again at later processing gates.
Existing saved plans stay unchanged for audit; generate a fresh preview after
receipt eligibility changes. Explicit resend and recovery authorizations remain
separate from normal sending; acceptance evidence never enables them.

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

**Unscoped live `batch-run`, live resolution and background workers remain locked.**
Reusable production plans require the separate `--authorized-plan` controls above;
the legacy pilot retains `--pilot`/`--authorize-pilot`. Fixture switches cannot be
combined with either production scope. No live preflight, activation, sending or
production scheduling was performed while implementing these commands.
