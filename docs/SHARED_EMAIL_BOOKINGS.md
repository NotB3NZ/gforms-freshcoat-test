# Shared-email bookings

The production batch workflow can issue separate tickets to distinct, staff-approved
attendees who share one delivery email. Each attendee keeps an independent record ID,
participant ID, QR token, action, and Message-ID. The common email is only the
delivery destination. Existing payment approval and eligibility checks still apply
to every attendee.

## Review and explicit approval

Review the exact physical rows first. The review command checks that all rows exist
on the named configured source, share one normalized delivery email, have valid
registration details, and belong to the configured event. It prints row numbers,
approval facts, and SHA-256 binding digests; it never prints attendee names or email
addresses. Compare those exact rows in the staff-controlled Sheet view and confirm
that they are distinct attendees with payment coverage.

For the two current Regular-source candidates, the commands are:

```sh
.venv/bin/python -m event_checkin --profile production shared-email-review \
  --source regular --rows 15 33

.venv/bin/python -m event_checkin --profile production shared-email-approve \
  --source regular --rows 15 33 \
  --review-sha256 <DIGEST_FROM_THE_REVIEW_OUTPUT> \
  --confirm-distinct-attendees --confirm-payment-coverage

.venv/bin/python -m event_checkin --profile production shared-email-review \
  --source regular --rows 46 47

.venv/bin/python -m event_checkin --profile production shared-email-approve \
  --source regular --rows 46 47 \
  --review-sha256 <DIGEST_FROM_THE_REVIEW_OUTPUT> \
  --confirm-distinct-attendees --confirm-payment-coverage
```

Approval is saved privately at `runtime-production/shared-email-bookings.approved.json`
with mode 0600. The record binds the event, source, physical rows, response timestamp,
email, name, raw tier, and price category by digest. If any binding detail changes or
a row moves, the approval stops applying and staff must review and approve the new
digest. The approval does not set or bypass any of the four payment checkboxes.

Preview only those candidate rows after staff has approved them:

```sh
.venv/bin/python -m event_checkin --profile production batch-preview --live \
  --id regular:row:15 --id regular:row:33 \
  --id regular:row:46 --id regular:row:47 --limit all
```

The usual `batch-preview` and `batch-run` commands remain the selection and execution
interface. Preview is read-only. A later live run still needs the existing operational
checkpoint and separate disabled email/authorization controls.

## Ticket claim database schema

The legacy journal claims table uniquely keyed tickets by `(event_id, email)`. It
must be migrated before the batch runner can reserve a second ticket for an approved
shared email. The new key is `(event_id, record_id)`; email remains indexed for
collision checks. The reservation transaction permits an existing same-email claim
only when every same-email record is a member of the same currently valid approved
group. Unapproved later submissions still hit duplicate safeguards.

The migration is explicit and takes a SQLite online backup before changing only the
claims table. Run it only after an operationally approved maintenance window, with
the worker stopped and a new backup path:

```sh
mkdir -m 700 -p runtime-production/migration-records/shared-email
.venv/bin/python -m event_checkin --profile production shared-email-migrate-journal \
  --backup runtime-production/migration-records/shared-email/production-batches-before-shared-email.sqlite3 \
  --apply
```

The command takes the worker and Sheets locks, creates the mode-0600 SQLite backup,
verifies existing claims are byte-for-value preserved, then migrates the journal.
It refuses a missing journal, an existing backup path, or an unexpected schema. It
does not rewrite plans, action records, delivery receipts, identities, or artifacts.
The automated migration test first copies a synthetic legacy journal, migrates only
that copy, and verifies the original and backup remain on the legacy schema.

## Sending gates

Keep `EMAIL_ENABLED=false` and every sending authorization disabled until review,
preview, and the normal operational checkpoints are complete. This workflow neither
connects to SMTP nor issues a ticket during review or approval.
