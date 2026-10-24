# Personal-sender delivery for regular row 2

`EMAIL_ENABLED=false` always prohibits SMTP sending. There is no disabled-email
send exception. Offline composition and read-only preparation remain available.
The SMTP adapter checks the flag before connecting and again after authentication,
immediately before submission. The scoped path also reloads the production
configuration and private authorization at those gates.

This implementation adds only `production-single-send`, fixed to source
`regular`, physical row **2**, and the private sender address already used in
the separate test profile. It accepts no source, row, recipient override or
resend option. Production credentials were not copied or enabled.

## Preparation and scope

```sh
.venv/bin/python -m event_checkin --profile production production-single-send \
  --prepare --selection runtime-production/single-entry-regular-row2/selection.json
```

Preparation requires email and the private scope to remain disabled. It validates
all three approved schemas; the four approval flags; canonical tier and price
mapping; duplicates and identity integrity; unissued status and processing
history; and existing local reservations. It captures exact typed original cells,
then rechecks values and headers for changes. It never creates an issuance journal,
reserves a ticket identity, updates Sheet cells, renders a real ticket or uses SMTP.

The selected registration is bound privately by fingerprint, original snapshot,
source/tab configuration, approved header snapshot, final badge hash, sender and
selection-file hash. A changed/moved registration cannot be followed to another row.
Selections are not overwritten; failed preflight reports retain safe blocker reasons.

Private `config/single-entry-send.approved.json` contains the exact sender and
scope, including `enabled: false`. The manifest, header plan, scoped configuration,
selection, personal instructions and logs are Git-ignored. Keep private files at
0600 and their runtime directory at 0700.

## Later manual activation

No settings need enabling during implementation or preflight. Exact account-specific
steps are in the private
`runtime-production/single-entry-regular-row2/activation-steps.md` file. They include
the pinned personal sender and the provider host/port/security already used in the
test profile, without copying or displaying the password.

After reviewing a successful preflight, the operator must manually configure
`.env.production` with that personal account's `SMTP_HOST`, `SMTP_PORT`,
`SMTP_SECURITY`, `SMTP_USERNAME`, `SMTP_FROM` and provider SMTP/app password.
Both username and From must match the private sender pin. Google Sheets
service-account credentials are not SMTP credentials.

Only when deliberately choosing to perform this one send:

1. Set `.env.production` → `EMAIL_ENABLED=true`.
2. Set `config/single-entry-send.approved.json` → `"enabled": true`, preserving
   all sender, target, source, snapshot and selection bindings.
3. Run the exact one-entry command:

```sh
.venv/bin/python -m event_checkin --profile production production-single-send \
  --apply --selection runtime-production/single-entry-regular-row2/selection.json \
  --authorize-send
```

Disable both flags again after the attempt. Other production commands retain their
default configuration and operational locks. Loading enabled settings for this
one command does not unlock `Settings.require_operational`, generic writable
Sheets adapters, the production worker or general live batches.

## Sending and delivery safeguards

The scoped adapter can update only approved app-owned tracking fields on the
bound physical row 2. It checks current typed cells before writing exact
`userEnteredValue` fields. Form answers, approval checkboxes, attendance columns
and other registrations cannot be written through it.

The existing production processor and durable batch journal provide identity
reservation, frozen name/recipient/tier, stable Message-ID, readback, retries and
uncertain-delivery rules. Existing tickets or claims from another workflow cannot
be adopted. All-source checks run again throughout processing. After SMTP login,
the final gate verifies current approvals, duplicates, frozen fields, the durable
send intent, ticket reservation and the badge's name/tier pixels and QR. Inline
and attached PNG bytes must match the verified badge. The integrated confirmation
builder provides escaped HTML, plain text, CID image and full-resolution attachment.

A completed selection is idempotent and cannot resend. A known-not-accepted
failure can be retried explicitly with the same selection, identity, card and
Message-ID:

```sh
.venv/bin/python -m event_checkin --profile production production-single-send \
  --retry --selection runtime-production/single-entry-regular-row2/selection.json \
  --authorize-send
```

Both enable flags are still required. `SENT`, `SENDING`, `AMBIGUOUS` and
`EMAIL_REVIEW` are not automatic retry permissions. Interrupted sends, a lost
SENT write, disabling email in flight or concurrent changes can leave a
conservative review state. Review provider logs before resolving; SMTP cannot
guarantee exactly-once delivery after an uncertain result.

The only resolution surface is the same bound selection:

```sh
.venv/bin/python -m event_checkin --profile production production-single-send \
  --resolve sent --selection runtime-production/single-entry-regular-row2/selection.json
# Or --resolve retry only after staff review determines that retry is appropriate.
```

Resolution records a human outcome and never connects to SMTP; it may run while
email is disabled. It cannot be used as a sending authorization.

Offline tests use fake Sheets, mocked SMTP and temporary synthetic journals. They
cover disabled-email enforcement, wrong senders/scopes, changed rows and protected
columns, approvals and duplicates, stable retry identities/IDs, uncertain outcomes,
lost identity/SENT writes, CID attachments, idempotence and legacy compatibility.

## Diagnose an existing incomplete attempt

Leave both enable flags false and use the existing selection:

```sh
.venv/bin/python -m event_checkin --profile production production-single-send \
  --diagnose --selection runtime-production/single-entry-regular-row2/selection.json
```

This operation validates all three row-one schemas, then reads only the mapped
name/email/tier and 24 tracking cells on regular row 2. It opens the existing
journal read-only. It never constructs a processor, reserves an identity, renders,
updates Sheets or connects to SMTP. It saves a new private diagnosis report and
leaves the selection, journal and card unchanged. Participant values, credentials,
Message-IDs and QR tokens are withheld from its output.

An incomplete action is not evidence of non-submission. Diagnosis checks the bound
reservation and plan, original delivery history, current send intent, attempt
count, Message-ID presence and sent timestamp. A matching pre-send snapshot can
establish `NOT_SUBMITTED` because this path requires persisted SENDING intent and
confirmed readback before SMTP. SENDING/AMBIGUOUS without acceptance evidence stays
`UNCERTAIN`. A retained acceptance checkpoint or the post-acceptance SENT record
can establish `CONFIRMED_ACCEPTED`, even if action completion is missing.

Errors retain only a fixed stage, approved source ID, exception class, HTTP status,
allowlisted API reason and local code location. Raw responses and exception messages
are not logged. New SMTP attempts also retain a private acceptance checkpoint after
successful submission. Failure to save diagnostics never authorizes a retry or
changes the SMTP outcome. These checkpoints cannot reconstruct an older exception
or prove an older submission occurred.

For an interrupted pre-send action, recovery resumes the original `--apply` only
after a separate deliberate authorization. It reuses the reserved identity, card
and journal Message-ID and repeats every live safety check. Do not prepare another
selection or clear tracking. A known rejected delivery uses explicit `--retry`.
An uncertain delivery requires provider submission logs or recipient evidence
before `--resolve sent` or `--resolve retry`; absence from Sent alone is insufficient.
No diagnostic command performs any recovery write or send.
