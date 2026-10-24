# Organization-sender inbox test

`production-inbox-test` prepares or submits exactly one synthetic confirmation
email from `awscc.upmindanao@gmail.com`. It never reads registrations, opens
Sheets, reserves a production identity, or uses the personal-sender selection.
General production batches, writable adapters and workers remain locked.

The fictional name is `Avery O'Connell & Lane`. A fresh local UUID binds the
prepared message and invalid badge. The QR uses the permanently rejected
`INVALID_TEST_` namespace; the PNG retains its red invalid-ticket banner. Subject,
HTML and plain text prominently identify the message as a test. The final HTML
template, event copy, poster URL and font fallbacks are preserved in the message
copy. The final badge uses the pinned Google Sans 400/700 files, final name/tier
bindings and QR rectangle `(378, 228, 444)`.

The message contains HTML, plain text, one CID-inline PNG and the same full
resolution PNG as a downloadable attachment. Browser preview paths are substituted
only in `preview.html`; `confirmation.eml` retains its CID reference.

## Prepare locally with email disabled

Run from the repository root. Choose one recipient and a new folder. Replace the
example address below; preparation does not contact that address or any server.

```sh
RECIPIENT='your-chosen-recipient@example.com'
INBOX_TEST_DIR='runtime-production/organization-inbox-tests/manual-inbox-001'
.venv/bin/python -m event_checkin --profile production production-inbox-test \
  --prepare --to "$RECIPIENT" --out "$INBOX_TEST_DIR" --tier Regular
open "$INBOX_TEST_DIR/preview.html"
```

Preparation requires `EMAIL_ENABLED=false`. It verifies the final badge's text
pixels, QR and pinned fonts, then archives the invalid PNG, HTML, complete EML,
manifest and preparation binding. It needs no SMTP password to construct a message.
Both configured sender fields must already exactly match the organization sender.
Existing output folders are refused. Only direct children of the configured
production runtime's `organization-inbox-tests` directory are allowed; files are
private (0600), directories are private (0700), and the default runtime is Git-ignored.

Remote poster and web-font availability remains unverified. Opening a browser
preview may request those public URLs; construction and tests never fetch them.
A browser preview does not establish inbox compatibility.

## Manual activation, single send, and deactivation

Keep the organization app password only in `.env.production`. This command does
not print, copy, configure or verify the credential against Gmail during
preparation. Before a deliberate send, privately confirm the provider's
`SMTP_HOST`, `SMTP_PORT`, `SMTP_SECURITY`, and password are configured.
`SMTP_USERNAME` and `SMTP_FROM` must both be exactly
`awscc.upmindanao@gmail.com`. Leave the old personal-sender approval unchanged.

Activation (only when you intend to send the reviewed test):

```sh
.venv/bin/python -c "from dotenv import set_key; set_key('.env.production', 'EMAIL_ENABLED', 'true', quote_mode='never')"
```

Submit the prepared message once, using the same explicit recipient and folder:

```sh
.venv/bin/python -m event_checkin --profile production production-inbox-test \
  --send --to "$RECIPIENT" --out "$INBOX_TEST_DIR" --authorize-send
```

Deactivate immediately afterward, including after a failure:

```sh
.venv/bin/python -c "from dotenv import set_key; set_key('.env.production', 'EMAIL_ENABLED', 'false', quote_mode='never')"
```

For a shell block that also deactivates on normal exit or interruption:

```sh
(
  trap '.venv/bin/python -c "from dotenv import set_key; set_key(\".env.production\", \"EMAIL_ENABLED\", \"false\", quote_mode=\"never\")"' EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  .venv/bin/python -c "from dotenv import set_key; set_key('.env.production', 'EMAIL_ENABLED', 'true', quote_mode='never')" || exit
  .venv/bin/python -m event_checkin --profile production production-inbox-test \
    --send --to "$RECIPIENT" --out "$INBOX_TEST_DIR" --authorize-send
)
```

These are operator instructions; implementation and tests do not run them.
An enabled flag is accepted only by explicit scoped commands; it does not unlock
general production. `EMAIL_ENABLED=false` is checked before SMTP construction and
again after authentication, immediately before submission. The scoped adapter
reloads the private settings and checks the exact organization sender, unchanged
SMTP configuration, frozen recipient, template/font hashes, MIME/PNG contents and
invalid QR. There are no recipient-list, batch, resend or participant-data options.

## Outcomes and delivery evidence

`attempt.json` is written exclusively before SMTP. A per-test lock and that
durable marker prevent a second submission from the same folder, including after
a crash or error. The Message-ID, recipient and card are never replaced on send.

`result.json` reports `SMTP_ACCEPTED`, `NOT_ACCEPTED`, or `UNCERTAIN`:

- Local gating, connection/authentication failure or explicit SMTP rejection is
  known not accepted. No automatic retry occurs.
- A disconnect during submission is uncertain. A started attempt without a
  result is also uncertain unless retained acceptance evidence establishes the
  outcome. Missing completion alone is never proof of non-submission.
- A successful SMTP response records a separate acceptance checkpoint. Acceptance
  is not proof of delivery to Inbox; check Inbox/Spam, bounces and provider logs.

The same folder cannot be retried even after a known failure. Review the private
result and safe stage/class diagnostics before deciding on another deliberate
test. For uncertainty, check provider submission logs and recipient Inbox/Spam
using the retained Message-ID and attempt time before creating another test.
Absence from Sent alone does not establish rejection. Never delete or reset the
attempt marker to bypass this guard.

Offline tests use fictional data, temporary files and mocked SMTP. They cover
disabled-email enforcement, exact organization sender, recipient binding, final
rendering/QR/fonts, MIME/CID/attachment bytes, safe failure diagnostics, settings
revocation, interrupted attempts, and legacy behavior.
