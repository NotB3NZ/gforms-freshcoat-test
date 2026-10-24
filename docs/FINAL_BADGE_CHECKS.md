# Final badge: long names and fonts

Completed with synthetic data only on 2026-10-24. No participant rows, live
Sheets, mail clients, issuance journals or HTML-email integration were used.
`EMAIL_ENABLED=false`, production locks, profile files and existing previews
were preserved. The legacy `templates/card.coat` test workflow still passes.

The installed `templates/event-badge-final.coat` remains byte-for-byte unchanged:

```text
SHA-256 7e3c7247d68dd490d2987ae616d40cf344805a38d6a36023a9176d97327830d4
```

The frame is `front`; bindings are `displayName` / `ticketTier`. QR placement
remains x=378, y=228, size=444. The name box remains x=72, y=681.1,
width=1056, height=92.45. Other element positions and sizes are unchanged.

## Long names

The source's `displayName.maxLength=40` is widened to 160 only in the rendering
copy. The actual name and any required legacy `name` field receive the complete
string. Inputs over the application's existing 160-character limit are rejected.
The installed schema is preserved.

FreshCoat fits text against box height before applying `maxLines=1`; this can
discard wrapped lines. The temporary copy therefore also fits the complete
name to the original line width with the pinned 700 face, retaining shrink fit
and a 2% width allowance for FreeType/Skia rounding. The independent literal
verification path uses the same copy preparation before removing bindings.

CanvasKit command inspection verifies that the final rendered name equals the
entire synthetic input, has one line, has no missing glyphs, and stays within
the existing horizontal bounds. Literal name/tier pixels and QR decoding also
pass for all cases:

| Case | Length | Tier | Name size (design px) | Full text / pixels / QR |
| --- | ---: | --- | ---: | --- |
| ACCENTS-45 | 45 | Lite | 44.31 | Passed |
| LONG-80 | 80 | Regular | 24.83 | Passed |
| LONG-160 | 160 | Pro | 12.43 | Passed |
| WIDE-160 | 160 × W | Pro | 6.78 | Passed |

The names include `É`, `é`, `ó`, ASCII and curly apostrophes, and `&`.
Very long names necessarily become small in the preserved single-line box;
the wide-character stress case demonstrates that names are retained even
below FreshCoat's usual 8px shrink floor. Physical print legibility was not tested.
FreshCoat reports small-text print warnings for the 80- and 160-character cases
(the 160-character accented name is about 2.98pt; the W stress case is about
1.63pt). Existing 115-DPI footer-image and rich-black print warnings also remain
in the captured logs. These are print checks, not font-resolution or glyph fallback.

## Actual font results

The source declares a Google Fonts URL requesting only 700 even though the tier
requires 400. A zero process exit did not establish either correct font loading
or correct weight coverage. The rendering copy now declares local Google Sans
14.000 **400 and 700**, with pinned, hash-checked assets and the upstream OFL.
See [font provenance](../templates/fonts/google-sans/README.md).

For every verification case:

- Only Google Sans was loaded; both loaded byte hashes match the manifest.
- Remote font fetching was explicitly disabled: **0 requests**.
- Font-resolution missing/guessed lists and glyph issue lists are empty.
- CLI stdout and stderr were inspected: **no font or glyph fallback warnings**.
- CanvasKit's actual paragraph engine selected 400 for the tier and 700 for
  the name. At each weight, its shaped advances matched a renderer holding
  only the corresponding intended face. The accented probe's 48px advances
  were 778.03px (400) and 830.50px (700), proving distinct weight selection.
- Each intended face directly covers all codepoints used in its name/tier.

Real negative checks confirm that an absent font can produce exit code zero
with `no font data for Google Sans` / fallback warnings. These warnings are
now rejected in both card rendering and controlled literal verification.
An unsupported U+10FFFF glyph is rejected too. Missing or modified pinned
assets block rendering; another family is never substituted.

## Tests and artifacts

```sh
.venv/bin/python -m unittest discover -s tests -v
# 197 tests passed, including 8 focused final-badge tests and the legacy workflow.

# Use a NEW output directory: existing folders are refused.
.venv/bin/python -m event_checkin.final_badge_checks \
  --out generated-production/final-badge-checks-another-run
```

The offline command constructs fixed synthetic settings directly. It does not
load production profiles or connect to Google or SMTP. Previews preserve the
1200×900 card and append a red invalid-test banner outside the badge layout.

Artifacts from this run are under
`generated-production/final-badge-checks-2026-10-24/`:

- `tests.log`: complete passing test output.
- `review/report.json`: per-case proof, font matching and preview paths.
- `review/ACCENTS-45/INVALID-SYNTHETIC-PREVIEW.png`
- `review/LONG-80/INVALID-SYNTHETIC-PREVIEW.png`
- `review/LONG-160/INVALID-SYNTHETIC-PREVIEW.png`
- `review/WIDE-160/INVALID-SYNTHETIC-PREVIEW.png`

Each case folder also contains synthetic values, a derived rendering copy,
`font-resolution.json`, `cli-stdout.txt`, and `cli-stderr.txt`. These artifacts
are ignored by Git along with other generated production previews.
