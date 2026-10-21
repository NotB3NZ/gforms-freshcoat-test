# Production schema proposal — awaiting human review

Local implementation and offline tests are complete. Live writes, initialization,
issuance, recovery and sending remain locked. The existing test system, private
settings, credentials, templates and ticket history are preserved.

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

These ranges are conditional, **not live validation results from this phase**.
Re-read all three header rows before initialization. If any tracking headers are
already present and confirmed app-owned, append only missing ones after the current
last header; never insert into or overwrite the original ranges.

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

## The next exact checkpoint

1. Confirm `aws-scd-mindanao-2026` as the permanent production event ID, or choose
   the final ID before any live issuance. Do not change it after issuance.
2. Review and explicitly approve the 24 app-owned headers above for all three
   sources, including the four additions. Inspect any existing matching labels
   for ownership before approving them as tracking columns.
3. In a separately authorized step, run the read-only `production-headers` command
   and review its append plan. A changed/missing approval or form header must be
   resolved through schema review, never guessed from similar text.
4. Only after that checkpoint, authorize the narrowly scoped live schema append
   and a subsequent controlled issuance verification. This implementation does
   not expose an unlock flag; enabling live writes needs an explicitly reviewed
   follow-up change. Keep `EMAIL_ENABLED=false` throughout schema work.

No live schema append, participant-row inspection, queueing, issuance, SMTP
configuration or real send was performed by this phase. Real raw tier labels,
Google behavior/quotas and organization delivery remain unverified. Organization
email setup and live sending are a later checkpoint; no password is requested here.
