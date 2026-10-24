# Approved production header text and duplicate positions

The eight differences in the private row-one comparison dated 2026-10-24 were
explicitly approved. Local `config/sources.production.json` and
`config/production-header-init.approved.json` now use the exact reviewed labels:

| Sources | Field | Column | Approved label |
| --- | --- | --- | --- |
| All three | email | C | `Email Address` |
| All three | name | D | `Full Name (e.g Juan S. Dela Cruz)` |
| discount_10 | tier | M | `10% OFF Tickets (Pro Tickets are not available)` |

The exact reviewed discount_10 R1 question is retained in the approved snapshot.
It is not an application mapping. All other form/staff labels, source identities,
prices, tier aliases, eligibility rules and tracking positions remain unchanged.
No live headers or cells were modified or initialized.

The only duplicate-header exceptions are explicit per-source settings:

```json
{"approved_duplicate_headers": {"Email Address": ["C", "E"]}}
```

That value applies only to regular. Both discount sources require `["C", "F"]`.
The validator rejects other labels, positions, extra occurrences, missing
occurrences or a recipient mapping outside C. A source without this explicit
setting retains the original rule rejecting every duplicate header.

Mapped participant fields are read by verified column position and exact label.
Column C is always the recipient source. The second email column is not read as
an application field, merged, compared or substituted when C is blank. No email
value was selected from live participant rows during this update.

All four approval mappings remain exact and positional. Tracking headers remain
unique; sources using the reviewed exception require all 24 tracking names in
their approved sequence whenever tracking headers are present. Full snapshot
comparison still rejects changes to unmapped questions, unexpected shifts and
additional headers. Legacy sources and their initialization tests retain their
existing behavior.

Private byte-for-byte backups of the previous manifest and approved snapshot,
plus a change record, are under the ignored `runtime-production/schema-update-*`
directory recorded in `runtime-production/schema-update-current.json`. The
configuration files, backups, test logs and live validation report remain ignored
by Git. The historical comparison used for approval is preserved unchanged.

Verification includes nine focused tests and the full regression suite: 219 tests
passed. Focused checks cover exact duplicate positions; missing, moved, renamed
and extra duplicates; all mapped fields and approvals; all 24 tracking headers;
selection of C despite a different or populated secondary email; snapshot
strictness; and unchanged tier/price/eligibility behavior.

Live validation uses only row 1 and spreadsheet/tab metadata with a read-only
scope. No participant adapter, controlled rendering test, initialization or SMTP
operation is invoked. `EMAIL_ENABLED=false` and general production locks remain.
