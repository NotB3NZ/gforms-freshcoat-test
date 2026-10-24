# Pinned Google Sans for the final badge

These are Google Sans **14.000**, genuine static 400 and 700 faces derived
from the [official Google Fonts source at the pinned revision](https://github.com/google/fonts/tree/b7c8331cf620346575487ef8db0e2c729f014675/ofl/googlesans).
The included `OFL.txt` is the upstream SIL Open Font License. `manifest.json`
records the upstream URL, revision, SHA-256, derivation axes and resulting
asset hashes. FontTools confirmed `OS/2.usWeightClass` 400/700 and no remaining
`fvar` table. Static style names and bold flags are normalized accordingly.

Runtime rendering needs no font download, system font installation or FontTools.
The renderer checks both asset hashes and embeds the faces in its **temporary**
template copy as data URLs. The installed `.coat` archive remains untouched.
Only Google Sans is registered by the final badge renderer; a missing glyph
cannot silently resolve through another registered family.

Optional rebuild, using the upstream TTF URL recorded in the manifest:

```sh
python -m pip install fonttools==4.60.2
python tools/derive_google_sans.py /path/to/GoogleSans-variable.ttf
```

The rebuild refuses an unexpected upstream SHA-256, pins `opsz=17`, `GRAD=0`
and each weight, and preserves upstream font timestamps. The checked-in assets
and manifest are the runtime source of truth.
