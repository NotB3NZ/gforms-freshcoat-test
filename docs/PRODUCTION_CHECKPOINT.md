# Production schema — approved header initialization checkpoint

The user approved the permanent event ID, 24 headers and exact initialization
ranges after successful live header validation. The historical checkpoints below
record those phases. The latest controlled rendering-test implementation is
described at the end. General participant processing and sending remain locked.
The existing test system, private settings, credentials, templates and ticket
history are preserved.

## Exactly what would be appended

Every source uses the **same 24 exact app-owned headers**, in the order below.
The first 20 are the existing test tracking schema; the last four support this phase.
The app only appends missing headers, after validating all source mappings and
checking for duplicate labels. It never renames, reorders or overwrites form or
staff columns. Existing columns with these labels require inspection for app
ownership before approving initialization.

| Header | Why it is needed |
| --- | --- |
| `event_id` | Associate the record with the permanent event. |
| `participant_id` | Permanent issued-ticket identity; blank while waiting. |
| `ticket_token` | Opaque admission QR secret; blank while waiting. |
| `normalized_email` | Freeze the issued recipient and detect event-wide duplicates. |
| `ticket_name` | Freeze the issued display name for retries/regeneration. |
| `state` | Waiting, processing, ready, sent, failure or review status. |
| `render_status` | Distinguish rendering progress from delivery. |
| `email_status` | Track disabled, sending, failed, ambiguous or accepted delivery. |
| `duplicate_of` | Reserved canonical-registration reference for duplicate tracking; never a second ticket. |
| `card_path` | Private path to the rendered card. |
| `render_attempts` | Count card-generation attempts. |
| `email_attempts` | Count delivery intents for recovery/review. |
| `message_id` | Stable delivery-attempt identifier; explicit resends get a new one. |
| `last_error` | Sanitized explanation of a failure or review block. |
| `updated_at` | Last metadata-change timestamp. |
| `email_sent_at` | SMTP acceptance time, or staff review time when explicitly resolved. |
| `checked_in_at` | Existing attendance schema; batch processing leaves it untouched. |
| `checkin_request_id` | Existing idempotent check-in recovery schema; untouched by batches. |
| `ticket_status` | ACTIVE only after approval; cancellation cannot be reactivated by a retry. |
| `checked_in_by` | Existing staff station schema; untouched by batches. |
| `record_id` | **New:** permanent registration identity before approval/ticket issuance. |
| `source_id` | **New:** freeze physical registration source and retain source context. |
| `price_category` | **New:** retain/freeze regular or discount category separately from tier. |
| `ticket_tier` | **New:** freeze Lite/Regular/Pro for consistent renders, retries and resends. |

| Source | Tab | Last recorded original columns | Proposal if those headers are unchanged |
| --- | --- | --- | --- |
| `regular` | Participants | 23 (through W) | Append the 24 headers above in X:AU. |
| `discount_15` | Form Responses 1 | 21 (through U) | Append the same 24 headers in V:AS. |
| `discount_10` | Form Responses 1 | 21 (through U) | Append the same 24 headers in V:AS. |

These exact ranges were live-validated, explicitly approved and initialized on
2026-10-21. The scoped command re-reads all original headers and destinations on
every run; it leaves an exact complete header set untouched and rejects partial,
changed or misplaced tracking headers. Never insert into or overwrite original
form/staff ranges.

## Staff-owned columns that must remain read-only

| Exact header | Regular | Both discounts |
| --- | --- | --- |
| `Confirmation` | A | A |
| `Reference Number Matched?` | U | S |
| `Payment price match the price? (Discount/Regular)` | V | T |
| `Confirm if payment is received in our accont` | W | U |

All four must be boolean true or exact `TRUE`. Every other value blocks.
Name/email/tier header positions are also validated per source: C/D/H for regular,
C/D/M for both discounts. `accont` is the existing spelling and is preserved.

## Approved scope and remaining checkpoint

The user confirmed `aws-scd-mindanao-2026` permanently and approved all 24 headers.
Live row-one checks passed for all three sources after the 15% tier label was
corrected to `15% OFF Tickets` at M1, with no duplicate or conflicting headers.
The user then explicitly authorized initialization of regular `X1:AU1` and both
discounts `V1:AS1`, including only necessary grid expansion and read-back checks.

This authorization does not cover participant rows, tracking values, queueing,
issuance, rendering, checkbox changes, organization credentials or email.
Participant operations remain locked with `EMAIL_ENABLED=false`.

## Scoped initialization command

`production-init-headers` validates the private approved snapshot without Google
writes; `production-init-headers --apply` applies only the approved ranges. The
private snapshot is `config/production-header-init.approved.json` and is ignored.
All original headers must match exactly. Destinations must be wholly empty or
already contain the complete exact 24 headers in their approved positions.

The implementation revalidates all sources before writes, grows only necessary
column counts, uses one atomic batch per Sheet with a `userEnteredValue` field
mask, and verifies row-one read-back. It never reads response rows or invokes
participant processing, rendering or SMTP. Per-Sheet atomicity supports reruns
after lost responses; it is not a transaction across the three spreadsheets.

The next checkpoint after verified header initialization is separate approval
for read-only participant/tier inspection and review of a bounded first issuance
selection. Keep `EMAIL_ENABLED=false` and participant operations locked.

## Completed live initialization — 2026-10-21

All three sources were revalidated before any write and again before each Sheet
request. Every original header, mapping and destination matched the reviewed
snapshot. The command wrote only the approved header values and read them back
after each write, then completed a final all-source header verification.

| Source | Verified headers written | Grid columns before → after | Added grid columns |
| --- | --- | --- | --- |
| `regular` / Participants | `X1:AU1` (24 headers) | 27 → 47 | 20 |
| `discount_15` / Form Responses 1 | `V1:AS1` (24 headers) | 24 → 45 | 21 |
| `discount_10` / Form Responses 1 | `V1:AS1` (24 headers) | 24 → 45 | 21 |

Only `gridProperties.columnCount` and the approved header cells'
`userEnteredValue` fields were mutated. Existing form/staff headers were unchanged
on read-back. Formatting, validation, notes and response rows were outside the
write masks. No participant rows were read; no tracking values below row 1 were
populated; no checkbox, queue, ticket, card or email operation was performed.

The implementation passed 15 focused initialization tests and 89 selected offline
regression tests in total, using fake Google/SMTP/renderers. This checkpoint did
not launch FreshCoat or render real cards. Rerun safety and before/after-commit
recovery were verified with fakes.

**Stopped at the next checkpoint:** participant/tier inspection and any issuance
remain separately authorized steps. Production participant operations are still
locked, and the unchanged private profile has `EMAIL_ENABLED=false`.

## Approved exact tier aliases — local implementation only

Following authorized aggregate-only participant/tier inspection, the user approved
exactly these mappings:

| Source | Original label, preserved in Sheets | Canonical tier |
| --- | --- | --- |
| `regular` | `Lite (₱199)` | `Lite` |
| `discount_15` | `Lite (₱170)` | `Lite` |
| `discount_15` | `Pro (₱297)` | `Pro` |

Canonical `Lite`, `Regular`, `Pro` inputs retain their existing support. No new
Regular alias or discount_10 price-suffixed alias is approved. No case/whitespace
normalization or arbitrary suffix removal is performed. Unknown labels block.
The same resolver is used by production eligibility and aggregate reporting;
the earlier inspection's heuristic proposals have been removed.

Original form cells and price categories are unchanged. All four approval checks
and existing duplicate-review rules still apply to normal processing, retries
and resends. Raw labels remain part of saved-selection fingerprints.

This local change makes no live requests, assigns no live identities and performs
no issuance, rendering or SMTP operation. **Stop before the single-entry test:**
participant operations remain locked and `EMAIL_ENABLED=false`. The test still
needs separate explicit authorization after staff complete all four checks for
one selected registration and any duplicate issues are resolved.

Verification for the local alias checkpoint: **90 offline tests passed**, including
**12 focused alias tests**. Each approved alias was checked against all 16
TRUE/FALSE combinations of the four approvals. Unknown and source-mismatched
labels, malformed approvals, retry/resend gates, duplicate conflicts and raw-label
selection changes remain blocked. No live single-entry test was executed.

## Controlled rendering test with cleanup — implementation checkpoint

The user has authorized one bounded rendering test and cleanup for the exact
registration from a successful preview. The preceding `discount_15`, row 38
preview did **not** complete successfully, so no verified registration snapshot
exists from that preview. Target clarification is pending before any live test.
No live identity, card or test metadata has been created by this implementation.

`production-render-test` is a separate narrow path; it does not change
`Settings.require_operational()` or enable mail. All four current approvals,
exact source-specific tier aliases, canonical duplicate rules, unissued state,
all-source access and absence of prior local reservations must pass. A new private
single-use selection binds raw registration values, approvals, original typed
tracking values and headers. The selected locator and complete snapshot are
checked again before live execution. There is no force/bypass option.

An authorized test uses an isolated `TEST-` identity with an `INVALID_TEST_` token.
The token is permanently inadmissible in check-in and blocked by normal production
policy, independent of Sheet metadata or cleanup success. During the test,
`ticket_status` and `state` stay `CANCELLED`; email stays `DISABLED`. Neither normal
identity/claims journal is modified. Original Form/staff values are never written.
Only the fixed 17 test tracking fields can be written, using `userEnteredValue`
masks that preserve all formatting, validation, notes and other content.

The current template is rendered without modification. QR decoding must match the
test token. Name and tier pixels must match an independent render of the current
template with those bindings replaced by the expected literal text. The retained
private card has an invalid-test banner and filename; temporary verification
files are removed. No raw participant data or QR token is printed.

The private audit records original values, headers, expected snapshots and write
intents before writes. Cleanup accepts only an exact original or expected test
snapshot, reconciles uncertain writes with readback, restores original tracking
values/formulas/types, verifies that the test identity is absent from all sources,
and permanently retires its isolated local reservation. Unrelated registrations
and existing journals/history are untouched. A changed/partial snapshot blocks
restoration and preserves the audit for `--recover` cleanup only. No blind restore
or repeated issuance is allowed. Sheets lacks a compare-and-set API, so a human
edit between the final read and a write remains a platform limitation.

**Historical implementation status:** waiting for the selected registration to be clarified. Keep
`EMAIL_ENABLED=false`; normal production processing, sending, and deployment
remain locked. Do not interpret offline verification as a successful live test.

Offline verification: **182 tests passed**, including **29 focused controlled-test
checks**. These include an actual synthetic render of the current template,
correct/incorrect name and tier pixel comparisons, QR decoding, all 16 approval
combinations, duplicate/issued-history blocks, constrained write masks, original
value/formula/type restoration, isolated journal preservation, concurrent edits,
partial/uncertain writes, idempotent cleanup and rejection of a repeated selection.

## Read-only diagnosis of row 37 preview — 2026-10-21

The target is explicitly confirmed as `discount_15`, spreadsheet row **37**.
Rendering is not authorized during this diagnosis. The underlying failure is a
local schema-integrity guard: `SheetIntegrityError` in `header_init.classify`, at
the comparison of original headers with the approved snapshot. There is no HTTP
failure or Google API error reason for this exception.

A fresh header-only read confirmed that regular and discount_15 still match the
approved schema. In **discount_10, C1 and D1 differ**, and the configured name/email
header mappings do not validate. Its 24 tracking headers still match. All sources
must validate for duplicate checking; therefore row 37 preparation stops before
participant reads. Its current four approvals, tier, duplicate status and unissued
state are **not verified by this attempt**. Do not substitute row 38, relax the
header guard, rewrite live headers or silently update the approved snapshot.

The generic error handler now preserves safe diagnostics: fixed operation stage,
trusted function/line, underlying exception class, HTTP status and allowlisted
Google reason, without raw messages/responses/payloads. Exception context remains
available even when an upstream handler uses `raise ... from None`. Focused tests
cover privacy, suppressed contexts and preservation of the all-source schema gate.
No Sheets were modified; no identities assigned, cards rendered or mail sent.
`EMAIL_ENABLED=false` and general production processing remain locked.

The final fresh preparation repeated after the diagnostic changes stopped at
`validate_header_snapshot_discount_10`, `header_init.classify:64`, with underlying
`SheetIntegrityError`, no HTTP status and no Google API reason. **62 focused
offline tests passed**, including diagnostic privacy, header/approval mappings,
exact aliases, all approval combinations, identity gates and production locks.
No FreshCoat rendering was run during this diagnosis. The approved snapshot and
all live cells remain unchanged; discount_10 C1/D1 need schema review before a
controlled rendering test can proceed.

## Completed controlled rendering test — discount_15 row 37

After the user corrected discount_10 C1/D1 and completed all four approvals for
the confirmed target, fresh read-only preparation passed all three source
schemas, approvals, exact tier mapping (`Pro`), duplicate policy, unissued-state
checks and local-reservation checks. The saved selection was revalidated again
before any live tracking-cell write. No neighboring registration was selected.

The single authorized test completed with `test_result=VERIFIED` and
`cleanup=RESTORED_AND_VERIFIED`. The personalized name and Pro tier pixels matched
the independent literal-text reference; the QR decoded to the isolated invalid
test token. The current template hash was unchanged. All original tracking values
were restored and exact target-cell readback verified; all-source verification
confirmed the test identity was absent. The isolated reservation is `RETIRED`.
The archived QR was independently decoded and confirmed permanently rejected by
the reserved test-token admission guard without accessing admission state.

Private artifacts (directories 0700, audit/card files verified 0600):

- `runtime-production/controlled-tests/db69ad3e-3406-45f5-b959-eac1c4703682/audit.json`
- `runtime-production/controlled-tests/db69ad3e-3406-45f5-b959-eac1c4703682/INVALID-TEST-TICKET.png`

The archived preview preserves the 1200×900 card area and appends a prominent
invalid/retired test banner. Original Form answers and staff checkboxes were never
written; no other registration was processed and no email was sent. Normal
production journals were not used for the test reservation. General production
processing, general Sheet writes and sending remain locked; `EMAIL_ENABLED=false`.
The registration is restored to its original unissued state. Stop at this
checkpoint; this test does not authorize production issuance or email.
