# Official three-entry pilot operating guide

The separately authorized [deliberate row-2 resend](DELIBERATE_PILOT_RESEND.md)
preserves its original uncertain attempt and uses a separate linked delivery with
its own stable Message-ID. Its extra authorization remains disabled. Use that
guide's `--remaining-first-sends` command to resume only rows 3 and 4 without
changing row 2's unresolved history.

Implementation and offline verification only. The pilot authorization is disabled
and `EMAIL_ENABLED=false`. No live preflight, activation or execution was performed
while preparing this implementation. These commands are for a separately
authorized operating session. General batches and background processing stay locked.

The existing `batch-run` command accepts one narrow `--pilot` scope. Its only plan
is the original, unchanged file below, SHA-256
`1a7f01a7e49542ff82a5c1d2f036b6b97d8f430ac912819826ca6fff757094a9`.
It binds exactly discount_15 rows 2, 3 and 4 and their three saved entry hashes.
The all-source preview scope does not authorize a fourth registration. No blocked
or over-limit entry can be added, substituted or backfilled.

Run all commands from the project directory:

```sh
cd /Users/benzdecastro/Documents/Projects/test_gforms/event-checkin
PLAN='/Users/benzdecastro/Documents/Projects/test_gforms/event-checkin/runtime-production/batch-reviews/official-pilot-20261101-134414.json'
```

## Read-only preflight

Keep both the pilot policy and email disabled. This reads all three exact reviewed
source schemas, mapped registration data, duplicate/approval/cancellation status
and existing local delivery history. It creates no identity, journal, card, plan
or Sheet update, and never constructs an SMTP connection. Output contains only
the original locators and bounded status/blocker information.

```sh
.venv/bin/python -m event_checkin --profile production batch-run \
  --pilot --plan "$PLAN" --preflight
```

Stop on any schema, selected-detail, eligibility or history mismatch. Review
corrections separately. Do not edit the plan or replace a fingerprint to bypass
a mismatch. Recognizing a ticket label is not payment approval. All four current
staff approvals remain mandatory.

## Read quota handling

Every Sheets GET, including gspread constructor requests and retries, shares one
credential-scoped read scheduler across the clients and all three sources in the
process. Requests start at least 1.25 seconds apart. This pacing is not a guarantee
against quota consumed by other processes or clients.

Only retryable reads are retried: quota responses (429 and recognized 403 quota
reasons), temporary server failures and read transport timeouts/connections.
There are at most five attempts per request, exponential delays of 2/4/8/16 seconds
plus 0–1 second jitter, a 30-second exponential-delay cap, and a 120-second per-read
retry/waiting budget. A larger `Retry-After` is honored only within the remaining
budget. Fixed progress messages report quota waiting/retry counts to stderr,
with updates during long waits; response bodies, URLs and participant data are
never included. Permission, schema and malformed-response failures stop rather
than retrying. Writes and SMTP submissions are never automatically retried.

The instrumented offline comparison used real gspread HTTP dispatch with a fake
transport/clock and the same synthetic three-source fixture. Counts exclude quota
retries and do not claim a live performance measurement:

| Operation | Previous read requests | Current read requests |
| --- | ---: | ---: |
| Pilot reader initialization | 6 | 0 |
| Fresh all-source check | 21 | 6 |
| Typed target capture | 5 | 1 |
| Tracking update before its single write | 52 | 7 |

Tracking-update reads fell 86.5%. Each all-source sweep first validates all three
schemas before reading any participant fields, then fetches each source's approved
column groups together with its schema and typed/effective cells. Each update
uses that fresh sweep plus one final target capture; it compares both original
cell types/formulas and effective values before writing only approved metadata.
No row/approval snapshot is reused across processing or SMTP boundaries.

Any retry waiting invalidates a partial sweep. It must restart with fresh source
checks, approvals, duplicates, cancellation, identity and history. A wait during
the final target capture likewise requires a new all-source sweep. Each sweep or
final-capture operation permits at most one refresh restart; repeated invalidation
at that boundary stops for review. The post-auth
and final pre-submission checks still obtain fresh snapshots. After quota exhaustion,
diagnose the unchanged original plan using the read-only command below. Retained
identity claims and Message-IDs are reused; uncertain intent still needs provider
review, and accepted receipts reconcile without resubmission.

## Activation and execution — future authorized session only

The private policy is `config/pilot-batch.approved.json`; its checked-in example
contains no credentials. It pins the plan, three entries, organization sender,
reviewed manifest/header snapshot and final badge template. It is already present
locally with `enabled=false`. Activate only this policy with email still disabled:

```sh
.venv/bin/python -m event_checkin --profile production batch-run \
  --pilot --plan "$PLAN" --activate --authorize-pilot
```

Privately configure the organization SMTP account in `.env.production` using the
existing settings. `SMTP_USERNAME` and the address in `SMTP_FROM` must both be
`awscc.upmindanao@gmail.com`. Credentials are never stored in the plan/policy or
printed by these commands. After approved preflight and activation, edit the
intended setting to `EMAIL_ENABLED=true`:

```sh
nano .env.production
```

Execute exactly the saved three-entry plan:

```sh
.venv/bin/python -m event_checkin --profile production batch-run \
  --pilot --plan "$PLAN" --authorize-pilot
```

`EMAIL_ENABLED=false` always prohibits both SMTP connection and submission.
There is no force switch. A disabled/changed policy, wrong sender, changed plan,
schema or ticket details stops execution. Other production commands gain no
authorization from this policy or the email flag.

The runner reuses the established batch journal, identity claims, renderer and
HTML confirmation builder. It writes only app tracking fields on the three bound
rows. Before submission it confirms persisted intent, all four approvals,
duplicates, cancellation, frozen ticket data, decoded QR and independently verified
name/tier pixels. HTML, plain text, CID PNG and attachment are compared with a
message built from that same frozen data. Eligibility is checked again after badge
verification and SMTP authentication, immediately before submission.

Keep the single-host process and runtime directory together. Do not sort or edit
selected rows while it runs. Sheets has no compare-and-set operation; the final
read/write interval cannot be made atomic with a human edit.

## Deactivation

Disable pilot authorization first. This is local only and remains available if
source schemas or the template have drifted:

```sh
.venv/bin/python -m event_checkin --profile production batch-run \
  --pilot --plan "$PLAN" --deactivate
nano .env.production
```

Set `EMAIL_ENABLED=false` in the editor. Deactivation cancels no issued ticket
and clears no tracking, Message-ID, acceptance receipt or delivery history.

## Diagnosis and recovery

Deactivate and disable email before review. Diagnose the original plan:

```sh
.venv/bin/python -m event_checkin --profile production batch-run \
  --pilot --plan "$PLAN" --diagnose
```

Diagnosis is read-only, including SQLite opened with `mode=ro`/`query_only`.
It does not repair tracking or resume sending. Review an uncertain attempt using
its existing Message-ID in private journals/provider logs. Diagnosis prints no
recipient addresses or QR tokens. In `runtime-production/production-batches.sqlite3`,
the existing `actions` table retains stable record IDs and Message-IDs.
Pilot-only `pilot_bindings`, `pilot_delivery` and append-only
`pilot_delivery_events` tables retain scope, frozen identity hash, intent, outcomes
and private provider-review references.

Diagnosis separates `registration_phase` from delivery evidence. `RESERVED`
means only an action's stable record ID/Message-ID was allocated; it does not mean
processing started and does not require a retry. `INITIALIZED` means the matching
registration metadata was saved. `IDENTITY_RESERVED` additionally requires a
matching local identity claim whose ticket fields have not yet been persisted.
`ISSUED` requires verified Sheet identity/frozen details matching that claim.
An issued entry left at `PROCESSING`/`RENDERING` with no delivery intent can resume
using its existing identity. None of these classifications bypasses current
approvals, duplicate review, cancellation or delivery-history checks.

The shared diagnosis/execution classifier selects normal `send` eligibility for
untouched reservations and verified initialization interrupted before submission.
Actual render/email failures or known-unsent receipts retain `retry_required=true`
and need explicit targeted retry. Uncertain delivery still requires provider
review; accepted receipts reconcile without another submission. Unknown or
inconsistent partial tracking requires review rather than being cleared/reset.

| Receipt | Meaning and next action |
| --- | --- |
| `NO_INTENT` | No pilot SMTP intent recorded. Resume only after eligibility review and activation. |
| `INTENT` / `UNCERTAIN` | SMTP may have accepted. Never automatically retry. Review provider logs. |
| `UNSENT` | Known connection/pre-submission failure or explicit SMTP rejection. Explicit targeted retry required. |
| `RETRY_AUTHORIZED` | Staff recorded provider evidence of non-acceptance. Activation, enabled email and targeted retry are still required. |
| `ACCEPTED` | Durable SMTP acceptance. Complete tracking without another submission. |

SMTP acceptance is not confirmed inbox delivery. Acceptance is committed with
SQLite `synchronous=FULL` and confirmed by readback before final Sheet completion.
If that Sheet write or response is lost, the receipt prevents resubmission. If
final Sheet readback does not confirm completion, the local action stays unfinished
and can reconcile from its acceptance receipt without resubmission. If
acceptance cannot be durably recorded, the attempt stays uncertain. Later staff
review uses review time as its acceptance timestamp; it does not assert an
independently observed provider timestamp or inbox delivery.

For an existing local acceptance receipt, or provider-confirmed acceptance of an
uncertain attempt, reconcile one original target while policy and email remain
disabled. Supply the actual private evidence reference:

```sh
.venv/bin/python -m event_checkin --profile production batch-run \
  --pilot --plan "$PLAN" --resolve 'discount_15:row:2' \
  --outcome accepted --evidence 'REPLACE_WITH_REVIEWED_ACCEPTANCE_REFERENCE' \
  --authorize-pilot
```

This records completion without SMTP. It preserves identities, frozen details,
Message-IDs, cancellation and prior acceptance timestamps. Accepted or completed
delivery cannot be released for retry.

Only after provider evidence confirms an uncertain attempt was **not accepted**:

```sh
.venv/bin/python -m event_checkin --profile production batch-run \
  --pilot --plan "$PLAN" --resolve 'discount_15:row:2' \
  --outcome not-sent --evidence 'REPLACE_WITH_REVIEWED_NON_ACCEPTANCE_REFERENCE' \
  --authorize-pilot
```

Resolution sends nothing. For this released attempt, or a recorded known-unsent
failure, follow the activation steps and privately enable email. Retry only that
target:

```sh
.venv/bin/python -m event_checkin --profile production batch-run \
  --pilot --plan "$PLAN" --retry 'discount_15:row:2' --authorize-pilot
```

Use `discount_15:row:3` or `discount_15:row:4` only when that is the diagnosed
original target. Targeted retries leave other unfinished entries untouched and
reuse the original identity, QR token, frozen details, badge and Message-ID.
Passing `--retry` cannot release uncertain delivery.

After review/activation, resume remaining eligible work with the unchanged plan
and original execution command. Completed entries are skipped; accepted receipts
reconcile without sending again. Failed entries still need explicit retries;
uncertain entries remain blocked for provider review. Deactivate and set
`EMAIL_ENABLED=false` again when the pilot stops.

Never restore snapshots over issued tickets, delete journals to force a retry,
clear delivery fields, or create a replacement plan to bypass recovery. There is
no cleanup/restore command in this scope. Staff checks, Form answers and
unselected registrations are never written by the pilot.
