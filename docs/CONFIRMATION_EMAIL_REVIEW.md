# Confirmation email — local visual review

Implemented with synthetic data only. No participant rows, Sheets, Drive,
Google credentials, SMTP connections or email sending were used. Production
locks and the disabled email setting remain unchanged. Composition and tests
do not require Google access.

The supplied HTML, badge archives, previous templates and existing previews are
preserved. Source SHA-256 values:

| Source | SHA-256 |
| --- | --- |
| `templates/email/registration-confirmation.html` | `fa527b092014f91520a9bb6f444c13964fa7ddab27fc2e4789e96b6c0b78cada` |
| `templates/event-badge-final.coat` | `7e3c7247d68dd490d2987ae616d40cf344805a38d6a36023a9176d97327830d4` |
| `templates/card.coat` | `c9e727bb7280f9effa5712ebe7d6e2e95e5741e74b1f6b71778edfb24fec8bc2` |
| `templates/event-badgev2.coat` | `fa89f6268ca40f29f8d2bbb2df19e489e15a07c8da84c3f81a048b4028a74c2a` |

## Message construction

`event_checkin.confirmation_email.build_confirmation_message` returns an
`EmailMessage` without SMTP settings, credentials, an enabled-email flag or
network calls:

```python
message = build_confirmation_message(
    frozen_recipient, issued_name, participant_id, badge_path, durable_message_id,
    ticket_tier=issued_tier, sender=None,
)
```

`first_name` is the first word of the complete issued name. `full_name` preserves
that name; `email` is the passed frozen recipient; `pass_type` accepts only Lite,
Regular or Pro. Participant text is HTML-escaped, including image alt text.
Substitution happens once. Missing, unknown or unresolved placeholders fail
before transport. Local composition errors are known-unsent failures.

Only the commented badge example and active dashed badge table are replaced in
the rendered copy. Event copy, poster URL, layout, Outlook conditional markup
and font fallback stacks are preserved. The source file is never rewritten.

```text
multipart/mixed
├── multipart/alternative
│   ├── text/plain
│   └── multipart/related
│       ├── text/html           (src="cid:badge.…@event-checkin.invalid")
│       └── image/png; inline   (matching Content-ID)
└── image/png; attachment       (event-badge.png)
```

Both PNG parts contain the exact supplied full-resolution bytes, without
resizing or public badge hosting. Browser previews are separate HTML copies
using a sibling PNG filename; local paths never enter the email HTML.

`SMTPMailer.build_message` also works offline. Production messages require the
issued tier. The batch caller passes its already-frozen name, recipient, tier,
participant ID and rendered card path; the mailer never retrieves Sheet values.
The caller's Message-ID is unchanged, and the badge CID derives from it.
Existing retry and uncertain-delivery handling is preserved. Legacy calls using
five positional arguments retain the original plain-text message workflow.

## Review files and verification

Open `generated-production/confirmation-email-review-2026-10-24/index.html`.
Each tier folder contains `preview.html`, `confirmation.eml` and
`INVALID-SYNTHETIC-BADGE.png`:

| Folder | Fictional name | Frozen recipient |
| --- | --- | --- |
| `INVALID-EMAIL-LITE` | Avery O'Connell & Lane | avery.lite@example.invalid |
| `INVALID-EMAIL-REGULAR` | Élodie Marín O'Connell | elodie.regular@example.invalid |
| `INVALID-EMAIL-PRO` | Rowan Harper Everleigh | rowan.pro@example.invalid |

Every name has 22 characters. Synthetic PNGs preserve the full 1200×900 badge
and append a red invalid-test banner below it, producing 1200×974 images.
Their `INVALID_TEST_` QR namespace is permanently rejected by check-in.
Name/tier pixel verification and QR decoding pass for all three examples.
`report.json` records fields, source and image hashes, and per-badge verification.

**210 regression tests passed**, including 13 new checks covering all tiers,
escaping, placeholder rejection, frozen recipient/name/tier/card consistency,
serialized MIME, exact attachments, stable IDs, retry/uncertain-delivery behavior,
legacy compatibility, locks and actual synthetic rendering. `tests.log` contains
the results. Transport tests use mocks; no SMTP connection is made.

Reproduce locally with a **new** output directory (existing folders are refused):

```sh
.venv/bin/python -m event_checkin.confirmation_previews \
  --out generated-production/confirmation-email-review-another-run
.venv/bin/python -m unittest discover -s tests -v
```

The preview command constructs fixed synthetic settings directly, without
loading private profiles or credentials or creating Google/SMTP clients.

## Settings and review limits

No settings need to be added for composition or local previews. Keep
`EMAIL_ENABLED=false`. Sender credentials were not configured. If a separate
sending phase is later authorized, the existing private-profile fields
`SMTP_HOST`, `SMTP_PORT`, `SMTP_SECURITY`, `SMTP_USERNAME`, `SMTP_PASSWORD` and
`SMTP_FROM` need manual review and entry. The contact email in the HTML does not
configure or authorize a sender.

The original public poster URL at `lh3.googleusercontent.com` and Playfair
Display stylesheet URL at `fonts.googleapis.com` remain unchanged. Their
availability is **unverified**. They are not fetched by the builder or tests and
are not prerequisites for completion. Browser visual review is pending; browser
previews do not establish inbox compatibility, remote-image loading or web-font
support. Work stops here for visual review, with live processing locked.
