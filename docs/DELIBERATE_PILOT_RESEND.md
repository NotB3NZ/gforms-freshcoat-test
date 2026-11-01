# Deliberate resend of the unresolved discount_15 row 2 pilot delivery

The user explicitly authorized preparing this single deliberate resend and
acknowledged that the recipient may receive duplicate email. Implementation and
offline verification do not send it. The original pilot authorization, separate
resend authorization and `EMAIL_ENABLED` remain disabled.

The original plan stays byte-for-byte unchanged. The resend is pinned to exactly
`discount_15:row:2`, its existing unresolved parent action/receipt, frozen identity,
exact retained badge hash and original sender/schema/template authorization.
No other recipient, identity, QR, badge generation or arbitrary resend is allowed.

The resend never updates the participant Sheet. Original `actions`, `claims`,
`pilot_delivery` and `pilot_delivery_events` records are preserved, including the
original unknown outcome and original Message-ID. A separate `pilot_resends`
record links that original record ID/Message-ID/receipt to one deterministic
resend ID and its own stable Message-ID. `pilot_resend_events` is append-only.
The attempt record is created only by deliberate future execution, not by local
authorization preparation. It preserves a hash of the original Sheet tracking.

HTML, plain text, CID image and attachment use the existing frozen ticket and the
same PNG bytes. The existing QR and name/tier verifier runs before SMTP submission;
the original badge is never redrawn or replaced. Every send/retry still requires
all four current approvals, all-source schema/duplicate checks, active uncancelled
ticket status, unchanged frozen details, the pinned organization sender, both
authorization scopes, enabled email and explicit duplicate-risk acknowledgement.
The only eligibility exception is the acknowledged original uncertain delivery.

SMTP intent and acceptance are recorded durably with readback using the existing
FULL-synchronous SQLite journal infrastructure. There is no Google request between
SMTP acceptance and its acceptance receipt. Acceptance is not inbox confirmation.
An uncertain resend never automatically repeats. Repeating an accepted operation
returns without resubmission. Retry reuses its own Message-ID; it never mints a
third delivery attempt under this authorization.

## Future operator commands — none run during implementation

From the project directory, define the unchanged plan and command helper:

```sh
cd /Users/benzdecastro/Documents/Projects/test_gforms/event-checkin
PLAN='runtime-production/batch-reviews/official-pilot-20261101-134414.json'
pilot() { .venv/bin/python -m event_checkin --profile production batch-run --pilot --plan "$PLAN" "$@"; }
```

The ignored private file `config/pilot-resend.approved.json` is prepared locally
with `enabled=false`. To create it when missing, with both original sending gates
disabled, this local-only command reads the existing journal/badge and writes only
the new disabled policy. It preserves an existing policy rather than replacing it:

```sh
pilot --prepare-resend 'discount_15:row:2' --acknowledge-duplicate --authorize-pilot
```

Read-only live preflight rechecks current source data and original tracking. It
does not reserve an attempt, render a card, connect to SMTP or update Sheets:

```sh
pilot --resend-preflight 'discount_15:row:2'
```

Stop on a mismatch. Do not edit hashes, erase metadata or substitute another badge.
Activate the original pilot and the additional row-2 resend scope while email is
still disabled:

```sh
pilot --activate --authorize-pilot
pilot --activate-resend 'discount_15:row:2' --acknowledge-duplicate --authorize-pilot
```

Privately confirm the organization SMTP settings: `SMTP_USERNAME` and the address
in `SMTP_FROM` must both be `awscc.upmindanao@gmail.com`. Edit the existing private
profile to set `EMAIL_ENABLED=true`; credentials are never printed/copied by these
commands:

```sh
nano .env.production
```

Perform only the one deliberate resend:

```sh
pilot --resend 'discount_15:row:2' --acknowledge-duplicate --authorize-pilot
```

Review its result. `ACCEPTED` means SMTP acceptance, not inbox delivery. An
uncertain result requires provider review and does not authorize another send.
Disable the additional resend scope locally, then independently resume only the
original first sends for rows 3 and 4:

```sh
pilot --deactivate-resend 'discount_15:row:2'
pilot --remaining-first-sends --authorize-pilot
```

The remaining-first-sends operation never processes row 2 and never marks its
original attempt complete. Rows 3 and 4 use their existing reserved action IDs and
Message-IDs, and normal eligibility/recovery gates. Already completed entries are
skipped. Blocked entries are never replaced by other registrations. It is available
even if the deliberate resend itself needs provider review.

When stopping, disable the original scope and set `EMAIL_ENABLED=false` again:

```sh
pilot --deactivate
nano .env.production
```

## Read-only diagnosis and explicit resend recovery

Review the separate resend without changing either delivery:

```sh
pilot --resend-diagnose 'discount_15:row:2'
```

`NOT_STARTED`/`RESERVED` means the separate resend has not submitted; `INTENT` or
`UNCERTAIN` blocks all automatic resend attempts; `UNSENT` requires explicit retry;
`ACCEPTED` permanently prevents another submission. The original pilot diagnosis
continues to show the original unresolved delivery independently. Its original
`--resolve` command must not be used to label the earlier attempt not sent merely
because this later resend succeeded.

Use the stable new `pilot_resends.message_id` privately with provider logs. After
actual provider review, record only the resend outcome, with email disabled:

```sh
pilot --resend-resolve 'discount_15:row:2' --outcome accepted \
  --evidence 'REPLACE_WITH_REVIEWED_RESEND_ACCEPTANCE_REFERENCE' --authorize-pilot
```

Or, only when provider evidence confirms the resend was not accepted:

```sh
pilot --resend-resolve 'discount_15:row:2' --outcome not-sent \
  --evidence 'REPLACE_WITH_REVIEWED_RESEND_NON_ACCEPTANCE_REFERENCE' --authorize-pilot
```

Recording acceptance is local fact recording; it does not clear a later
cancellation or require re-enabling email. Releasing a retry rechecks current
eligibility and unchanged original tracking. Neither command changes the original
unknown outcome or connects to SMTP. Acceptance cannot be downgraded to not sent.

For a known-unsent resend or a provider-reviewed retry release, reactivate the
required scopes while email is disabled, privately enable email, then explicitly
retry the same linked resend with its same Message-ID:

```sh
pilot --resend-retry 'discount_15:row:2' --acknowledge-duplicate --authorize-pilot
```

Deactivate both scopes and disable email afterwards. Do not delete journals,
reset fields, retire tickets, or prepare another identity to bypass an uncertain
outcome. Another deliberate duplicate-risk delivery needs separate review and
authorization; this scope allows only this one linked resend operation.
